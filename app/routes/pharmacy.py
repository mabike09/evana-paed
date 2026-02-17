from decimal import Decimal
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from ..extensions import db
from app.permissions import roles_required
from ..models import BillingQueue, Invoice, InvoiceLine, Payment


def _invoice_paid_total(invoice_id: int) -> Decimal:
    total = Decimal("0")
    for p in Payment.query.filter_by(invoice_id=invoice_id).all():
        total += Decimal(str(getattr(p, "amount", 0) or 0))
    return total


def _ensure_open_invoice(patient_id: int, visit_id: int | None) -> Invoice:
    q = Invoice.query.filter_by(patient_id=patient_id)
    if visit_id:
        q = q.filter_by(visit_id=visit_id)
    inv = q.order_by(Invoice.id.desc()).first()
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
        BillingQueue.query.filter_by(status="Open")
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
        q = Invoice.query.filter_by(patient_id=selected.patient_id)
        if getattr(selected, "visit_id", None):
            q = q.filter_by(visit_id=selected.visit_id)
        selected_invoice = q.order_by(Invoice.id.desc()).first()

        if selected_invoice:
            paid_total = _invoice_paid_total(selected_invoice.id)
            inv_total = Decimal(str(getattr(selected_invoice, "amount", 0) or 0))
            fully_paid = paid_total >= inv_total and inv_total > 0

            if fully_paid:
                for line in InvoiceLine.query.filter_by(invoice_id=selected_invoice.id).all():
                    if (getattr(line, "kind", "") or "").lower() == "drug":
                        paid_drug_lines.append(line)

    return render_template(
        "pharmacy.html",
        queue=queue,
        selected=selected,
        selected_invoice=selected_invoice,
        paid_drug_lines=paid_drug_lines,
    )


@bp.post("/queue/<int:q_id>/send-to-billing")
@login_required
@roles_required("nurse", "admin")
def pharmacy_send_to_billing(q_id):
    q = BillingQueue.query.get_or_404(q_id)
    q.status = "Closed"

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


@bp.post("/queue/<int:q_id>/prepare-invoice")
@login_required
@roles_required("nurse", "admin")
def pharmacy_prepare_invoice(q_id):
    q = BillingQueue.query.get_or_404(q_id)
    inv = _ensure_open_invoice(q.patient_id, q.visit_id)
    db.session.commit()
    flash("Invoice is ready. Add items then send to billing.", "success")
    return redirect(url_for("billing.invoice_edit", invoice_id=inv.id))
