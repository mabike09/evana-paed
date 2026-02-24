from decimal import Decimal
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import load_only

from ..extensions import db
from app.permissions import roles_required
from ..models import BillingQueue, Invoice, InvoiceLine, Payment, Item, ItemTxn, DispenseTxn


def _invoice_paid_total(invoice_id: int) -> Decimal:
    total = Decimal("0")
    for p in Payment.query.filter_by(invoice_id=invoice_id).all():
        total += Decimal(str(getattr(p, "amount", 0) or 0))
    return total


def _invoice_is_fully_paid(inv: Invoice | None) -> bool:
    if not inv:
        return False
    inv_total = Decimal(str(getattr(inv, "amount", 0) or 0))
    paid_total = _invoice_paid_total(inv.id)
    return inv_total > 0 and paid_total >= inv_total


def _invoice_payer_type(inv: Invoice | None, queue_entry: BillingQueue | None = None) -> str:
    payer_type = (getattr(inv, "payer_type", "") or "").strip() if inv else ""

    patient = getattr(queue_entry, "patient", None) if queue_entry else None
    insurance_provider = (getattr(patient, "insurance_provider", "") or "").strip().lower() if patient else ""
    queue_indicates_insurance = bool(insurance_provider and insurance_provider != "cash")

    payer_type_lc = payer_type.lower()
    if queue_indicates_insurance and payer_type_lc in {"", "cash"}:
        return "Insurance"
    if payer_type_lc == "insurance":
        return "Insurance"
    if payer_type_lc == "cash":
        return "Cash"
    return "Insurance" if queue_indicates_insurance else "Cash"



def _normalize_invoice_payer_types_for_context(patient_id: int, visit_id: int | None) -> None:
    q = _invoice_query_for_context(patient_id, visit_id)
    q.filter(func.lower(Invoice.payer_type) == "insurance").update(
        {Invoice.payer_type: "Insurance"},
        synchronize_session=False,
    )
    q.filter(func.lower(Invoice.payer_type) == "cash").update(
        {Invoice.payer_type: "Cash"},
        synchronize_session=False,
    )

def _invoice_is_dispense_eligible(inv: Invoice | None, queue_entry: BillingQueue | None = None) -> bool:
    if not inv:
        return False
    if _invoice_is_fully_paid(inv):
        return True
    # Insurance bills are verified/closed in Billing before dispensing.
    return _invoice_payer_type(inv, queue_entry).lower() == "insurance"


def _drug_rows_for_invoice(inv: Invoice):
    rows = []
    line_disp = {}
    for d in DispenseTxn.query.join(InvoiceLine, DispenseTxn.invoice_line_id == InvoiceLine.id).filter(InvoiceLine.invoice_id == inv.id).all():
        lid = getattr(d, "invoice_line_id", None)
        if lid:
            line_disp[lid] = line_disp.get(lid, Decimal("0")) + Decimal(str(getattr(d, "qty", 0) or 0))

    for line in InvoiceLine.query.filter_by(invoice_id=inv.id).all():
        if (getattr(line, "kind", "") or "").lower() != "drug":
            continue
        item = Item.query.get(getattr(line, "item_id", None)) if getattr(line, "item_id", None) else None
        prescribed = Decimal(str(getattr(line, "qty", 0) or 0))
        dispensed = line_disp.get(line.id, Decimal("0"))
        remaining = max(Decimal("0"), prescribed - dispensed)
        rows.append({
            "line": line,
            "item": item,
            "stock": int(getattr(item, "current_qty", 0) or 0) if item else 0,
            "prescribed": prescribed,
            "dispensed": dispensed,
            "remaining": remaining,
        })
    return rows


def _invoice_query_for_context(patient_id: int, visit_id: int | None):
    q = Invoice.query.filter_by(patient_id=patient_id)
    if visit_id is None:
        q = q.filter(Invoice.visit_id.is_(None))
    else:
        q = q.filter_by(visit_id=visit_id)
    return q

def _ensure_open_invoice(patient_id: int, visit_id: int | None) -> Invoice:
    _normalize_invoice_payer_types_for_context(patient_id, visit_id)
    inv = _invoice_query_for_context(patient_id, visit_id).order_by(Invoice.id.desc()).first()
    if inv:
        return inv

    inv = Invoice(
        patient_id=patient_id,
        visit_id=visit_id,
        issue_date=datetime.utcnow().strftime("%Y-%m-%d"),
        description="Created in pharmacy",
        amount=0,
    )
    db.session.add(inv)
    db.session.flush()
    return inv

bp = Blueprint("pharmacy", __name__, url_prefix="/pharmacy")


@bp.route("/", methods=["GET"])
@login_required
@roles_required("nurse", "admin")
def pharmacy_dashboard():
    active_q_id = request.args.get("queue_id", type=int)

    queue = (
        BillingQueue.query.options(
            load_only(
                BillingQueue.id,
                BillingQueue.patient_id,
                BillingQueue.visit_id,
                BillingQueue.status,
                BillingQueue.added_at,
                BillingQueue.added_by,
                BillingQueue.kind,
                BillingQueue.description,
            )
        )
        .filter_by(status="Open")
        .filter(BillingQueue.kind == "PHARMACY")
        .order_by(BillingQueue.added_at.asc())
        .all()
    )

    selected = None
    if active_q_id:
        selected = next((q for q in queue if q.id == active_q_id), None)
    if selected is None and queue:
        selected = queue[0]

    selected_invoice = None
    paid_drug_lines = []
    if selected:
        _normalize_invoice_payer_types_for_context(selected.patient_id, getattr(selected, "visit_id", None))
        selected_invoice = _invoice_query_for_context(
            selected.patient_id,
            getattr(selected, "visit_id", None),
        ).order_by(Invoice.id.desc()).first()

        if _invoice_is_dispense_eligible(selected_invoice, selected):
            paid_drug_lines = _drug_rows_for_invoice(selected_invoice)

    return render_template(
        "pharmacy.html",
        queue=queue,
        selected=selected,
        selected_invoice=selected_invoice,
        paid_drug_lines=paid_drug_lines,
        selected_is_insurance=(_invoice_payer_type(selected_invoice, selected) == "Insurance") if selected else False,
    )





@bp.route("/queue/<int:q_id>/clear", methods=["POST"], endpoint="pharmacy_queue_clear")
@login_required
@roles_required("nurse", "admin")
def pharmacy_queue_clear(q_id):
    q = BillingQueue.query.get_or_404(q_id)
    if (getattr(q, "kind", "") or "").upper() != "PHARMACY":
        abort(400)

    q.status = "Closed"
    if hasattr(q, "closed_at"):
        q.closed_at = datetime.utcnow()
    if hasattr(q, "description"):
        base = (q.description or "").strip()
        cleared_stamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        note = f"Cleared from pharmacy queue @{cleared_stamp}"
        q.description = f"{base} | {note}" if base else note

    db.session.commit()
    flash("Patient removed from pharmacy queue.", "success")
    return redirect(url_for("pharmacy.pharmacy_dashboard"))

@bp.route("/queue/<int:q_id>/dispense", methods=["POST"], endpoint="pharmacy_dispense")
@login_required
@roles_required("nurse", "admin")
def pharmacy_dispense(q_id):
    q = BillingQueue.query.get_or_404(q_id)
    if (getattr(q, "kind", "") or "").upper() != "PHARMACY" or (getattr(q, "status", "") or "") != "Open":
        abort(400)

    _normalize_invoice_payer_types_for_context(q.patient_id, getattr(q, "visit_id", None))
    inv = _invoice_query_for_context(q.patient_id, getattr(q, "visit_id", None)).order_by(Invoice.id.desc()).first()
    if not _invoice_is_dispense_eligible(inv, q):
        flash("Invoice must be paid or insurance-verified before dispensing.", "warning")
        return redirect(url_for("pharmacy.pharmacy_dashboard", queue_id=q.id))

    rows = {str(r["line"].id): r for r in _drug_rows_for_invoice(inv)}

    line_ids = request.form.getlist("line_id[]")
    qtys = request.form.getlist("qty[]")
    any_dispensed = False

    for lid, qraw in zip(line_ids, qtys):
        row = rows.get(str(lid))
        if not row:
            continue
        try:
            qty = Decimal((qraw or "0").strip())
        except Exception:
            qty = Decimal("0")
        if qty <= 0:
            continue

        item = row["item"]
        if not item:
            flash(f"{row['line'].description}: no linked inventory item.", "warning")
            continue

        remaining = row["remaining"]
        stock = int(getattr(item, "current_qty", 0) or 0)
        if qty > remaining:
            flash(f"{item.name}: qty {qty} exceeds remaining prescription {remaining}.", "warning")
            continue
        if qty > stock:
            flash(f"{item.name}: only {stock} in stock.", "warning")
            continue

        item.current_qty = stock - int(qty)
        db.session.add(
            DispenseTxn(
                item_id=item.id,
                patient_id=q.patient_id,
                visit_id=q.visit_id,
                invoice_line_id=row["line"].id,
                qty=qty,
                unit_price=Decimal(str(getattr(row["line"], "unit_price", 0) or 0)),
                line_total=Decimal(str(getattr(row["line"], "unit_price", 0) or 0)) * qty,
            )
        )
        db.session.add(
            ItemTxn(
                item_id=item.id,
                qty_change=-int(qty),
                reason="Consume-Visit",
                visit_id=q.visit_id,
                user_id=getattr(current_user, "id", None),
                note=f"Dispensed from pharmacy queue #{q.id}",
            )
        )
        any_dispensed = True

    if any_dispensed:
        db.session.commit()
        flash("Dispense recorded and inventory deducted.", "success")
    else:
        db.session.rollback()
        flash("No valid dispense quantities were submitted.", "warning")

    return redirect(url_for("pharmacy.pharmacy_dashboard", queue_id=q.id))

@bp.route("/queue/<int:q_id>/send-to-billing", methods=["POST"], endpoint="pharmacy_send_to_billing")
@login_required
@roles_required("nurse", "admin")
def pharmacy_send_to_billing(q_id):
    q = BillingQueue.query.get_or_404(q_id)
    q.status = "Closed"
    if hasattr(q, "description"):
        base = (q.description or "").strip()
        moved_stamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        note = f"Closed in pharmacy (sent to billing) @{moved_stamp}"
        q.description = f"{base} | {note}" if base else note

    exists = (
        BillingQueue.query.filter_by(visit_id=q.visit_id, status="Open")
        .filter(BillingQueue.kind == "BILLING")
        .first()
    )
    if not exists:
        bq = BillingQueue(
            patient_id=q.patient_id,
            visit_id=q.visit_id,
            status="Open",
            kind="BILLING",
            description="Returned from pharmacy for billing",
            added_by=getattr(current_user, "id", None),
        )
        db.session.add(bq)

    db.session.commit()
    flash("Sent back to billing queue.", "success")
    return redirect(url_for("pharmacy.pharmacy_dashboard"))


@bp.route("/queue/<int:q_id>/prepare-invoice", methods=["POST"], endpoint="pharmacy_prepare_invoice")
@login_required
@roles_required("nurse", "admin")
def pharmacy_prepare_invoice(q_id):
    q = BillingQueue.query.get_or_404(q_id)
    inv = _ensure_open_invoice(q.patient_id, q.visit_id)

    inferred_payer_type = _invoice_payer_type(inv, q)
    if (getattr(inv, "payer_type", "") or "").strip() != inferred_payer_type:
        inv.payer_type = inferred_payer_type

    db.session.commit()

    return redirect(
        url_for(
            "billing.invoice_edit",
            invoice_id=inv.id,
            next=url_for("pharmacy.pharmacy_dashboard", queue_id=q.id),
        )
    )
