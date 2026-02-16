# app/routes/billing.py
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from sqlalchemy import func

from ..permissions import roles_required
from ..extensions import db
from ..forms import InvoiceForm, PaymentForm, InvoiceEditForm
from ..models import (
    Invoice, Payment, InvoiceLine, Patient,
    Procedure, ProcedurePrice,
    Item, ItemPrice,
    BillingQueue, ClinicianQueue
)
from ..utils import generate_invoice_number, generate_receipt_number
from ..pdf import invoice_pdf_response, payment_pdf_response

bp = Blueprint("billing", __name__)

# ---- Optional models (guarded) ------------------------------------------------
try:
    from ..models import Insurer
except Exception:
    Insurer = None

try:
    from ..models import Visit
except Exception:
    Visit = None

try:
    from ..models import LabOrder
except Exception:
    LabOrder = None

try:
    from ..models import PriceBook, PriceItem, Payer
except Exception:
    PriceBook = PriceItem = Payer = None


# ------------------------------------------------------------------------------
# Create Invoice
# ------------------------------------------------------------------------------
@bp.route("/patients/<int:patient_id>/invoices/new", methods=["POST"])
@login_required
@roles_required("nurse", "reception", "admin")
def add_invoice(patient_id):
    Patient.query.get_or_404(patient_id)
    form = InvoiceForm()

    # Populate visit choices
    if Visit:
        patient_visits = (Visit.query
                          .filter_by(patient_id=patient_id)
                          .order_by(Visit.id.desc())
                          .all())
        form.visit_id.choices = [("", "— No Visit —")] + [
            (str(v.id), f"{getattr(v, 'visit_date', '')} • {getattr(v, 'reason', '') or 'Visit'}")
            for v in patient_visits
        ]

    if form.validate_on_submit():
        link_visit_id = int(form.visit_id.data) if getattr(form, "visit_id", None) and form.visit_id.data else None
        try:
            issue_date = form.issue_date.data.strftime("%Y-%m-%d")
        except Exception:
            issue_date = datetime.utcnow().strftime("%Y-%m-%d")

        inv = Invoice(
            patient_id=patient_id,
            visit_id=link_visit_id,
            issue_date=issue_date,
            description=(form.description.data or "").strip() if hasattr(form, "description") else "",
            amount=form.amount.data or 0
        )
        db.session.add(inv)
        db.session.flush()

        # Number from the provided date (or today)
        try:
            inv.number = generate_invoice_number(
                form.issue_date.data if hasattr(form, "issue_date") else datetime.utcnow()
            )
        except Exception:
            inv.number = generate_invoice_number(datetime.utcnow())

        db.session.commit()
        flash("Invoice created.", "success")
    else:
        problems = "; ".join([f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()])
        flash(f"Please correct invoice form errors. {problems}", "danger")

    return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="billing"))


# ------------------------------------------------------------------------------
# Record Payment
# ------------------------------------------------------------------------------
@bp.post("/patients/<int:patient_id>/invoices/<int:invoice_id>/payments/new")
@login_required
@roles_required("reception", "nurse", "admin")  # add "billing" too if you have that role
def add_payment(patient_id, invoice_id):
    """Record a payment. If the invoice becomes fully paid, release any lab orders waiting for payment."""
    inv = Invoice.query.filter_by(id=invoice_id, patient_id=patient_id).first_or_404()
    patient = Patient.query.get(patient_id)
    payer_type = (getattr(inv, "payer_type", None) or "").strip()
    if not payer_type:
        ip = (getattr(patient, "insurance_provider", "") or "").strip().lower() if patient else ""
        payer_type = "Insurance" if (ip and ip != "cash") else "Cash"

    raw_amount = (request.form.get("amount") or "").strip()
    method = (request.form.get("method") or "Cash").strip()
    reference = (request.form.get("reference") or "").strip()
    payment_date_str = (request.form.get("payment_date") or "").strip()

    # Amount → Decimal and must be > 0
    try:
        amt = Decimal(raw_amount.replace(",", "").replace(" ", ""))
        if amt <= 0:
            raise InvalidOperation()
    except Exception:
        flash("Payment not saved — invalid amount.", "danger")
        return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="billing"))

    # Date parse (YYYY-MM-DD), fallback = today
    try:
        pay_dt = datetime.strptime(payment_date_str, "%Y-%m-%d").date() if payment_date_str else datetime.utcnow().date()
    except Exception:
        pay_dt = datetime.utcnow().date()

    pay = Payment()
    # required fields (per models.py)
    if hasattr(pay, "invoice_id"):
        pay.invoice_id = inv.id
    if hasattr(pay, "payment_date"):
        # models.py defines payment_date as String(10), so keep it consistent
        pay.payment_date = pay_dt.strftime("%Y-%m-%d")
    if hasattr(pay, "amount"):
        pay.amount = amt
    if hasattr(pay, "method"):
        pay.method = method
    if hasattr(pay, "reference"):
        pay.reference = reference

    # Receipt number (safe)
    if hasattr(pay, "receipt_no"):
        try:
            pay.receipt_no = generate_receipt_number(datetime.utcnow())
        except Exception:
            try:
                pay.receipt_no = generate_receipt_number()
            except Exception:
                pay.receipt_no = None

    db.session.add(pay)

    try:
        db.session.commit()
        flash("Payment recorded.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Payment insert failed")
        flash("Payment not saved — database error.", "danger")
        return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="billing"))

    # -----------------------------
    # Auto-release LAB after full payment (non-blocking)
    # -----------------------------
    if LabOrder is not None:
        try:
            visit_id = getattr(inv, "visit_id", None)

            inv_total = Decimal(str(getattr(inv, "amount", 0) or 0))
            paid_total = Decimal("0")

            try:
                pays = Payment.query.filter_by(invoice_id=inv.id).all()
                for pp in pays:
                    paid_total += Decimal(str(getattr(pp, "amount", 0) or 0))
            except Exception:
                pass

            balance = inv_total - paid_total
            fully_paid = (balance <= Decimal("0"))

            if fully_paid:
                # 1) Release any lab orders waiting for payment
                if visit_id:
                    lab_orders = LabOrder.query.filter_by(
                        patient_id=patient_id,
                        visit_id=visit_id,
                        status="PendingPayment",
                    ).all()
                else:
                    lab_orders = LabOrder.query.filter_by(
                        patient_id=patient_id,
                        status="PendingPayment",
                    ).all()

                released = 0
                for lo in lab_orders:
                    lo.status = "Pending"
                    released += 1

                # 2) Ensure there is an OPEN LAB queue entry for this visit/patient
                try:
                    q = BillingQueue.query.filter_by(status="Open")
                    if hasattr(BillingQueue, "kind"):
                        q = q.filter(BillingQueue.kind == "LAB")
                    if visit_id:
                        q = q.filter(BillingQueue.visit_id == visit_id)
                    else:
                        q = q.filter(BillingQueue.patient_id == patient_id)

                    exists = q.first() is not None
                    if not exists:
                        bq = BillingQueue()
                        if hasattr(bq, "patient_id"): bq.patient_id = patient_id
                        if hasattr(bq, "visit_id"):   bq.visit_id = visit_id
                        if hasattr(bq, "status"):     bq.status = "Open"
                        if hasattr(bq, "added_at"):   bq.added_at = datetime.utcnow()
                        if hasattr(bq, "added_by"):   bq.added_by = getattr(current_user, "id", None)
                        if hasattr(bq, "kind"):       bq.kind = "LAB"
                        if hasattr(bq, "description"):bq.description = "Paid lab tests — sent to Lab"
                        db.session.add(bq)
                except Exception:
                    current_app.logger.exception("Failed ensuring LAB BillingQueue entry")

                if str(payer_type).lower() == "cash" and released:
                    try:
                        close_q = BillingQueue.query.filter_by(status="Open")
                        if hasattr(BillingQueue, "kind"):
                            close_q = close_q.filter(BillingQueue.kind == "BILLING")
                        if visit_id:
                            close_q = close_q.filter(BillingQueue.visit_id == visit_id)
                        else:
                            close_q = close_q.filter(BillingQueue.patient_id == patient_id)
                        for entry in close_q.all():
                            try:
                                db.session.delete(entry)
                            except Exception:
                                entry.status = "Closed"
                    except Exception:
                        current_app.logger.exception("Failed closing BillingQueue entry after cash lab payment")

                db.session.commit()

                if released:
                    flash("Lab payment complete — patient sent to Lab queue.", "success")

        except Exception:
            db.session.rollback()
            current_app.logger.exception("Lab order auto-release failed (non-blocking).")

    return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="billing"))

    # Date parse (YYYY-MM-DD), fallback = today
    try:
        pay_dt = datetime.strptime(payment_date_str, "%Y-%m-%d").date() if payment_date_str else datetime.utcnow().date()
    except Exception:
        pay_dt = datetime.utcnow().date()

    # 3) Build Payment object with all NOT NULL fields set BEFORE add/flush
    pay = Payment()

    # **CRITICAL**: set invoice_id first (fixes NOT NULL constraint)
    if hasattr(pay, "invoice_id"):
        pay.invoice_id = inv.id

    # Optional: store patient_id if model has it
    if hasattr(pay, "patient_id"):
        pay.patient_id = patient_id

    # payment_date supports date/datetime/str; coerce sensibly
    if hasattr(Payment, "payment_date"):
        pay.payment_date = pay_dt
    elif hasattr(pay, "payment_date"):
        pay.payment_date = pay_dt

    # Amount & Method (both should be present)
    if hasattr(pay, "amount"):
        pay.amount = amt

    if hasattr(pay, "method"):
        pay.method = method
    elif hasattr(pay, "channel"):  # if your schema uses 'channel' instead
        pay.channel = method

    # Reference / notes
    if hasattr(pay, "reference"):
        pay.reference = reference
    if reference and hasattr(pay, "notes"):
        pay.notes = reference

    # Receipt number (safe default)
    try:
        pay.receipt_no = generate_receipt_number(datetime.utcnow())
    except Exception:
        pay.receipt_no = generate_receipt_number()

    # 4) Insert & commit payment
    db.session.add(pay)

    try:
        db.session.commit()
        flash("Payment recorded.", "success")

                # -----------------------------
        # Auto-release LAB after full payment (robust)
        # -----------------------------
        if LabOrder is not None:
            try:
                visit_id = getattr(inv, "visit_id", None)

                # Compute balance robustly (don’t rely on inv.balance existing)
                inv_total = Decimal(str(getattr(inv, "amount", 0) or 0))

                paid_total = Decimal("0")
                try:
                    if hasattr(Payment, "invoice_id"):
                        pays = Payment.query.filter_by(invoice_id=inv.id).all()
                        for pp in pays:
                            paid_total += Decimal(str(getattr(pp, "amount", 0) or 0))
                except Exception:
                    pass

                balance = inv_total - paid_total
                fully_paid = (balance <= Decimal("0"))

                if fully_paid:
                    # 1) Release any lab orders waiting for payment
                    released = 0
                    if visit_id:
                        lab_orders = LabOrder.query.filter_by(
                            patient_id=patient_id,
                            visit_id=visit_id,
                            status="PendingPayment",
                        ).all()
                    else:
                        lab_orders = LabOrder.query.filter_by(
                            patient_id=patient_id,
                            status="PendingPayment",
                        ).all()

                    for lo in lab_orders:
                        lo.status = "Pending"
                        released += 1

                    # 2) Ensure there is an OPEN LAB queue entry for this visit/patient
                    try:
                        q = BillingQueue.query.filter_by(status="Open")
                        if hasattr(BillingQueue, "kind"):
                            q = q.filter(BillingQueue.kind == "LAB")
                        if visit_id:
                            q = q.filter(BillingQueue.visit_id == visit_id)
                        else:
                            q = q.filter(BillingQueue.patient_id == patient_id)

                        exists = q.first() is not None
                        if not exists:
                            bq = BillingQueue()
                            if hasattr(bq, "patient_id"): bq.patient_id = patient_id
                            if hasattr(bq, "visit_id"):   bq.visit_id = visit_id
                            if hasattr(bq, "status"):     bq.status = "Open"
                            if hasattr(bq, "added_at"):   bq.added_at = datetime.utcnow()
                            if hasattr(bq, "added_by"):   bq.added_by = getattr(current_user, "id", None)
                            if hasattr(bq, "kind"):       bq.kind = "LAB"
                            if hasattr(bq, "description"):bq.description = "Paid lab tests — sent to Lab"
                            db.session.add(bq)
                    except Exception:
                        current_app.logger.exception("Failed ensuring LAB BillingQueue entry")

                    db.session.commit()

                    if released:
                        flash("Lab payment complete — patient sent to Lab queue.", "success")

            except Exception:
                db.session.rollback()
                current_app.logger.exception("Lab order auto-release failed (non-blocking).")

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Payment insert failed")
        flash("Payment not saved — database error.", "danger")

    return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="billing"))


def _normalized_kind(proc_id=None, item_id=None):
    if item_id:
        return "drug"
    if proc_id:
        return "procedure"
    return "other"


# ------------------------------------------------------------------------------
# Insurer-aware catalog search (AJAX)
# ------------------------------------------------------------------------------
def _get_patient_insurer(patient_id: int):
    if not Insurer:
        return None
    pat = Patient.query.get(patient_id)
    if pat and pat.insurance_provider and (pat.insurance_provider or "").strip().lower() != "cash":
        try:
            return Insurer.query.filter(Insurer.name == pat.insurance_provider).first()
        except Exception:
            return None
    return None


def _resolve_price_book_for_patient(patient: Patient):
    if not (PriceBook and patient):
        return None

    def _normalize_payer_name(name: str) -> str:
        if not name:
            return ""
        return name.strip().lower().replace("insurance", "").strip()

    ip_raw = (getattr(patient, "insurance_provider", "") or "").strip()
    ip_norm = _normalize_payer_name(ip_raw)
    if ip_norm:
        try:
            if Payer is not None:
                book = (
                    db.session.query(PriceBook)
                    .join(Payer, PriceBook.payer_id == Payer.id)
                    .filter(func.lower(func.trim(Payer.name)) == ip_norm)
                    .order_by(PriceBook.effective_date.desc().nullslast(), PriceBook.id.desc())
                    .first()
                )
                if book:
                    return book
        except Exception:
            pass
        try:
            if hasattr(PriceBook, "insurer_id") and Insurer:
                ins = Insurer.query.filter(Insurer.name.ilike(ip_raw)).first()
                if ins:
                    book = PriceBook.query.filter_by(insurer_id=ins.id).order_by(PriceBook.id.desc()).first()
                    if book:
                        return book
        except Exception:
            pass
        try:
            book = (
                PriceBook.query
                .filter(PriceBook.name.ilike(f"%{ip_raw}%"))
                .order_by(PriceBook.id.desc())
                .first()
            )
            if book:
                return book
        except Exception:
            pass

    candidates = []
    try:
        q = PriceBook.query
        if hasattr(PriceBook, "active"):
            q = q.filter_by(active=True)
        candidates = q.order_by(PriceBook.id.desc()).all()
    except Exception:
        candidates = []

    for book in candidates:
        name = (getattr(book, "name", "") or "").lower()
        book_type = (getattr(book, "type", "") or "").lower()
        if "cash" in name or book_type == "cash":
            return book

    return candidates[0] if candidates else None


def _lab_items_from_book(book, query: str):
    if not (PriceItem and book):
        return []

    items = []
    try:
        q = PriceItem.query.filter_by(pricebook_id=book.id)
        if hasattr(PriceItem, "item_type"):
            q = q.filter(func.lower(func.trim(func.coalesce(PriceItem.item_type, ""))) == "lab")
        if query:
            if hasattr(PriceItem, "item_name"):
                q = q.filter(PriceItem.item_name.ilike(f"%{query}%"))
            elif hasattr(PriceItem, "item_code"):
                q = q.filter(PriceItem.item_code.ilike(f"%{query}%"))
        order_col = PriceItem.item_name if hasattr(PriceItem, "item_name") else PriceItem.id
        rows = q.order_by(order_col.asc()).limit(10).all()
        for r in rows:
            name = getattr(r, "item_name", None) or getattr(r, "item_code", None) or "Lab Test"
            price = (
                getattr(r, "price", None)
                or getattr(r, "sell_price", None)
                or getattr(r, "amount", None)
                or 0
            )
            items.append({"id": r.id, "name": name, "price": float(price or 0)})
    except Exception:
        return []

    return items


@bp.route("/api/search/catalog")
@login_required
def api_search_catalog():
    q = (request.args.get("q") or "").strip()
    patient_id = request.args.get("patient_id", type=int)
    insurer = _get_patient_insurer(patient_id) if patient_id else None
    patient = Patient.query.get(patient_id) if patient_id else None
    book = _resolve_price_book_for_patient(patient) if patient else None
    results = []
    seen = set()
    if not q:
        return jsonify(results)

    # Procedures
    procs = Procedure.query.filter(Procedure.name.ilike(f"%{q}%")).limit(10).all()
    for p_ in procs:
        seen.add((p_.name or "").strip().lower())
        price = Decimal(getattr(p_, "default_price", 0) or 0)
        pricebook_hit = None
        if book and PriceItem:
            try:
                pq = PriceItem.query.filter_by(pricebook_id=book.id)
                if hasattr(PriceItem, "item_type"):
                    pq = pq.filter(func.lower(func.trim(func.coalesce(PriceItem.item_type, ""))) == "procedure")
                if getattr(p_, "code", None):
                    pricebook_hit = pq.filter(PriceItem.item_code == p_.code).first()
                if not pricebook_hit:
                    pricebook_hit = pq.filter(PriceItem.item_name.ilike(p_.name)).first()
                if pricebook_hit and getattr(pricebook_hit, "sell_price", None) is not None:
                    price = Decimal(pricebook_hit.sell_price or 0)
            except Exception:
                pricebook_hit = None
        if not pricebook_hit and insurer and ProcedurePrice:
            try:
                pp = ProcedurePrice.query.filter_by(procedure_id=p_.id, insurer_id=insurer.id).first()
                if pp:
                    price = Decimal(pp.price or 0)
            except Exception:
                pass
        results.append({
            "id": f"proc-{p_.id}",
            "kind": "procedure",
            "category": getattr(p_, "category", None),
            "ref_id": p_.id,
            "text": f"{p_.name} (UGX {price:,.0f})",
            "price": float(price)
        })

    # Items / Drugs
    items = Item.query.filter(Item.name.ilike(f"%{q}%")).limit(10).all()
    for d in items:
        price = Decimal(getattr(d, "sell_price", 0) or 0)
        pricebook_hit = None
        if book and PriceItem:
            try:
                pq = PriceItem.query.filter_by(pricebook_id=book.id)
                if hasattr(PriceItem, "item_type"):
                    pq = pq.filter(func.lower(func.trim(func.coalesce(PriceItem.item_type, ""))) == "drug")
                if getattr(d, "sku", None):
                    pricebook_hit = pq.filter(PriceItem.item_code == d.sku).first()
                if not pricebook_hit:
                    pricebook_hit = pq.filter(PriceItem.item_name.ilike(d.name)).first()
                if pricebook_hit and getattr(pricebook_hit, "sell_price", None) is not None:
                    price = Decimal(pricebook_hit.sell_price or 0)
            except Exception:
                pricebook_hit = None
        if not pricebook_hit and insurer and ItemPrice:
            try:
                ip = ItemPrice.query.filter_by(item_id=d.id, insurer_id=insurer.id).first()
                if ip:
                    price = Decimal(ip.price or 0)
            except Exception:
                pass
        results.append({
            "id": f"drug-{d.id}",
            "kind": "drug" if getattr(d, "is_drug", False) else "other",
            "ref_id": d.id,
            "text": f"{d.name} (UGX {price:,.0f})",
            "price": float(price)
        })

    # Lab tests from price book (if available)
    if patient_id and PriceBook and PriceItem:
        patient = Patient.query.get(patient_id)
        book = _resolve_price_book_for_patient(patient)
        for lab in _lab_items_from_book(book, q):
            name = lab.get("name", "Lab Test")
            if name.strip().lower() in seen:
                continue
            proc_id = None
            if Procedure:
                try:
                    proc_q = Procedure.query.filter(Procedure.name.ilike(name))
                    if hasattr(Procedure, "category"):
                        proc_q = proc_q.filter(Procedure.category.ilike("lab"))
                    proc = proc_q.first()
                    proc_id = getattr(proc, "id", None) if proc else None
                except Exception:
                    proc_id = None
            results.append({
                "id": f"lab-{lab.get('id')}",
                "kind": "procedure" if proc_id else "other",
                "category": "lab",
                "ref_id": proc_id or "",
                "text": f"{name} (UGX {lab.get('price', 0):,.0f})",
                "price": float(lab.get("price", 0) or 0)
            })

    return jsonify(results)


# ------------------------------------------------------------------------------
# Invoice Edit (existing)
# ------------------------------------------------------------------------------
@bp.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required("admin", "reception", "nurse", "doctor", "pediatrician")
def invoice_edit(invoice_id):
    inv = Invoice.query.options(joinedload(Invoice.lines)).get_or_404(invoice_id)

    if request.method == "GET":
        return render_template("invoice_edit.html", inv=inv, p=inv.patient)

    def _dec(x, default="0.00"):
        try:
            return Decimal(str(x)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal(default)

    line_ids       = request.form.getlist("line_id[]")
    procedure_ids  = request.form.getlist("procedure_id[]")
    item_ids       = request.form.getlist("item_id[]")
    descs          = request.form.getlist("desc[]")
    qtys           = request.form.getlist("qty[]")
    prices         = request.form.getlist("price[]")
    deletes        = set(request.form.getlist("delete[]"))

    existing = {str(l.id): l for l in inv.lines}

    for lid, pid, iid, desc, qraw, praw in zip_longest(
        line_ids, procedure_ids, item_ids, descs, qtys, prices, fillvalue=""
    ):
        # delete
        if lid and lid in deletes and lid in existing:
            db.session.delete(existing[lid])
            continue

        proc_id = int(pid) if (pid or "").isdigit() else None
        itm_id  = int(iid) if (iid or "").isdigit() else None
        kind = "drug" if itm_id else ("procedure" if proc_id else "other")

        desc = (desc or "").strip()
        q = _dec(qraw or "0")
        if q <= 0:
            q = Decimal("0.00")

        p = _dec(praw or "0")
        total = (q * p).quantize(Decimal("0.01"))
        is_blank_line = (not desc and proc_id is None and itm_id is None and q <= 0 and p <= 0)

        if lid and lid in existing:
            ln = existing[lid]
            if is_blank_line:
                db.session.delete(ln)
                continue
            ln.kind = kind
            ln.procedure_id = proc_id if kind == "procedure" else None
            ln.item_id = itm_id if kind == "drug" else None
            ln.description = desc or (ln.description or "")
            ln.qty = q
            ln.unit_price = p
            ln.line_total = total
            ln.insurer_amount = Decimal("0.00")
            ln.patient_amount = total
        else:
            if is_blank_line:
                continue
            db.session.add(InvoiceLine(
                invoice_id=inv.id,
                kind=kind,
                procedure_id=proc_id if kind == "procedure" else None,
                item_id=itm_id if kind == "drug" else None,
                description=desc or "",
                qty=q,
                unit_price=p,
                line_total=total,
                insurer_amount=Decimal("0.00"),
                patient_amount=total
            ))

    try:
        # Flush so deletes/adds/updates are reflected in DB for SUM()
        db.session.flush()

        # ✅ Recompute invoice total from lines
        new_total = db.session.query(
            func.coalesce(func.sum(InvoiceLine.line_total), 0)
        ).filter(InvoiceLine.invoice_id == inv.id).scalar()

        inv.amount = Decimal(str(new_total or 0)).quantize(Decimal("0.01"))

        # keep existing behavior of syncing to visit if you have it
        try:
            from ..routes.patients import _sync_invoice_to_visit  # optional helper
            _sync_invoice_to_visit(inv)
        except Exception:
            pass

        db.session.commit()
        flash("Invoice updated (total recalculated).", "success")
    except Exception:
        current_app.logger.exception("Invoice edit failed")
        db.session.rollback()
        flash("Could not save invoice changes.", "danger")

    return redirect(url_for("patients.patient_chart", patient_id=inv.patient_id, tab="billing"))


# ------------------------------------------------------------------------------
# Billing Queue Close (existing)
# ------------------------------------------------------------------------------
@bp.post("/billing-queue/<int:bq_id>/close")
@login_required
@roles_required("reception", "nurse", "admin")
def billing_queue_close(bq_id):
    """
    Billing queue behavior:

    - CASH patients: DO NOT allow closing/removal if the visit invoice is not fully paid.
    - INSURANCE / MEDICARD patients: treat 'close' as VERIFICATION.
        If there are lab orders waiting for payment, release them and send the patient to the LAB queue.
        Otherwise, just close/remove from billing queues.

    This keeps your UI simple (one button), but enforces the rules you requested.
    """
    bq = BillingQueue.query.get_or_404(bq_id)

    # Determine invoice + payer
    inv = None
    if getattr(bq, "visit_id", None):
        inv = (Invoice.query
               .filter_by(visit_id=bq.visit_id)
               .order_by(Invoice.id.desc())
               .first())
    if inv is None:
        inv = (Invoice.query
               .filter_by(patient_id=bq.patient_id)
               .order_by(Invoice.id.desc())
               .first())

    patient = Patient.query.get(bq.patient_id)
    payer_type = None
    if inv is not None and hasattr(inv, "payer_type"):
        payer_type = (inv.payer_type or "").strip()
    if not payer_type:
        # Fallback from patient.insurance_provider
        ip = (getattr(patient, "insurance_provider", "") or "").strip().lower() if patient else ""
        payer_type = "Insurance" if (ip and ip != "cash") else "Cash"

    # Compute balance
    balance = Decimal("0")
    if inv is not None:
        try:
            inv_total = Decimal(str(getattr(inv, "amount", 0) or 0))
            paid_total = Decimal("0")
            pays = Payment.query.filter_by(invoice_id=inv.id).all()
            for pp in pays:
                paid_total += Decimal(str(getattr(pp, "amount", 0) or 0))
            balance = inv_total - paid_total
        except Exception:
            balance = Decimal("0")

    # ---------------------------
    # CASH: block closure if not paid
    # ---------------------------
    if str(payer_type).lower() == "cash" and (balance > Decimal("0")):
        flash(f"Cannot close Billing for CASH patient — outstanding balance UGX {balance:,.0f}. Please receive full payment first.", "danger")
        # stay around billing context
        return redirect(url_for("billing.patient_billing", patient_id=bq.patient_id))

    # ---------------------------
    # INSURANCE/MEDICARD: verification path
    # ---------------------------
    pending_lab = 0
    if LabOrder is not None:
        try:
            q = LabOrder.query.filter_by(patient_id=bq.patient_id, status="PendingPayment")
            if getattr(bq, "visit_id", None):
                q = q.filter(LabOrder.visit_id == bq.visit_id)
            pending_lab = q.count()
        except Exception:
            pending_lab = 0

    if str(payer_type).lower() != "cash" and pending_lab:
        try:
            # 1) Release lab orders
            q = LabOrder.query.filter_by(patient_id=bq.patient_id, status="PendingPayment")
            if getattr(bq, "visit_id", None):
                q = q.filter(LabOrder.visit_id == bq.visit_id)
            for lo in q.all():
                lo.status = "Pending"

            # 2) Close/remove the BILLING queue entry
            try:
                bq.status = "Closed"
            except Exception:
                pass
            try:
                db.session.delete(bq)
            except Exception:
                pass

            # 3) Ensure an OPEN LAB queue entry exists
            lab_exists = False
            try:
                q2 = BillingQueue.query.filter_by(status="Open")
                if hasattr(BillingQueue, "kind"):
                    q2 = q2.filter(BillingQueue.kind == "LAB")
                if getattr(bq, "visit_id", None):
                    q2 = q2.filter(BillingQueue.visit_id == bq.visit_id)
                else:
                    q2 = q2.filter(BillingQueue.patient_id == bq.patient_id)
                lab_exists = (q2.first() is not None)
            except Exception:
                lab_exists = False

            if not lab_exists:
                labq = BillingQueue()
                if hasattr(labq, "patient_id"): labq.patient_id = bq.patient_id
                if hasattr(labq, "visit_id"):   labq.visit_id = getattr(bq, "visit_id", None)
                if hasattr(labq, "status"):     labq.status = "Open"
                if hasattr(labq, "added_at"):   labq.added_at = datetime.utcnow()
                if hasattr(labq, "added_by"):   labq.added_by = getattr(current_user, "id", None)
                if hasattr(labq, "kind"):       labq.kind = "LAB"
                if hasattr(labq, "description"):labq.description = "Insurance verified — sent to Lab"
                db.session.add(labq)

            # 4) (Optional) update visit station
            if Visit is not None and getattr(bq, "visit_id", None):
                try:
                    v = Visit.query.get(bq.visit_id)
                    if v and hasattr(v, "current_station"):
                        v.current_station = "LAB"
                except Exception:
                    pass

            # 5) Clear clinician queue entries (existing behavior)
            try:
                ClinicianQueue.query.filter_by(patient_id=bq.patient_id).delete()
            except Exception:
                pass

            db.session.commit()
            flash("Insurance verified — patient sent to Lab queue.", "success")
            return redirect(url_for("lab.lab_queue"))

        except Exception:
            db.session.rollback()
            current_app.logger.exception("Insurance verification to Lab failed")
            flash("Could not verify insurance / send to lab. Please try again.", "danger")
            return redirect(url_for("billing.patient_billing", patient_id=bq.patient_id))

    # ---------------------------
    # Default closure (paid cash OR insurance without pending labs)
    # ---------------------------
    try:
        ClinicianQueue.query.filter_by(patient_id=bq.patient_id).delete()
    except Exception:
        pass

    try:
        db.session.delete(bq)
    except Exception:
        # fallback if delete is blocked
        try:
            bq.status = "Closed"
        except Exception:
            pass

    db.session.commit()
    flash("Removed from Billing queue.", "success")
    return redirect(url_for("patients.patients_list"))


# ------------------------------------------------------------------------------
# NEW: Patient Billing page — invoices, payments, totals
# Route used by "Go to billing" buttons
# ------------------------------------------------------------------------------
def _sum_payments(payments):
    total = Decimal("0")
    for p in payments or []:
        try:
            total += Decimal(str(getattr(p, "amount", 0) or 0))
        except Exception:
            pass
    return total


@bp.get("/billing/patient/<int:patient_id>", endpoint="patient_billing")
@login_required
@roles_required("reception", "nurse", "doctor", "pediatrician", "admin")
def patient_billing(patient_id):
    p = Patient.query.get_or_404(patient_id)

    # Invoices for this patient (newest first)
    invoices = (Invoice.query
                .filter_by(patient_id=p.id)
                .order_by(Invoice.id.desc())
                .all())

    # Payments associated with those invoices (or fallback by patient)
    inv_ids = [inv.id for inv in invoices]
    payments = []
    try:
        if hasattr(Payment, "invoice_id") and inv_ids:
            payments = (Payment.query
                        .filter(Payment.invoice_id.in_(inv_ids))
                        .order_by(Payment.id.desc())
                        .all())
        elif hasattr(Payment, "patient_id"):
            payments = (Payment.query
                        .filter_by(patient_id=p.id)
                        .order_by(Payment.id.desc())
                        .all())
    except Exception:
        payments = []

    # Group payments by invoice_id for quick lookup
    by_inv = {}
    if payments and hasattr(Payment, "invoice_id"):
        for pay in payments:
            by_inv.setdefault(getattr(pay, "invoice_id", None), []).append(pay)

    # Build view rows
    rows = []
    for inv in invoices:
        inv_total = Decimal(str(getattr(inv, "amount", 0) or 0))
        inv_pays = by_inv.get(inv.id, payments if not by_inv else [])
        paid = _sum_payments(inv_pays)
        balance = inv_total - paid

        visit_label = None
        if Visit and hasattr(inv, "visit_id") and inv.visit_id:
            try:
                v = Visit.query.get(inv.visit_id)
                if v:
                    vd = getattr(v, "visit_date", None) or ""
                    visit_label = f"Visit #{v.id} {vd}".strip()
            except Exception:
                visit_label = None

        rows.append({
            "obj": inv,
            "number": getattr(inv, "number", None) or f"INV-{inv.id}",
            "issue_date": getattr(inv, "issue_date", None),
            "status": getattr(inv, "status", None) or getattr(inv, "payer_type", None) or "",
            "total": inv_total,
            "paid": paid,
            "balance": balance,
            "visit_label": visit_label,
        })

    # Totals
    grand_total = sum((r["total"] for r in rows), Decimal("0"))
    grand_paid = sum((r["paid"] for r in rows), Decimal("0"))
    grand_balance = grand_total - grand_paid

    return render_template(
        "billing_patient.html",
        p=p,
        invoice_rows=rows,
        payments=payments,
        grand_total=grand_total,
        grand_paid=grand_paid,
        grand_balance=grand_balance,
        now=datetime.utcnow(),
    )


# ========= Print Views =========
@bp.get("/billing/invoice/<int:invoice_id>/print", endpoint="invoice_print")
@login_required
@roles_required("reception", "nurse", "doctor", "pediatrician", "admin")
def invoice_print(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)

    # Compute total paid (so the template can show Paid & Balance neatly)
    pays = getattr(inv, "payments", []) or []
    try:
        paid_total = sum((p.amount or 0) for p in pays)
    except Exception:
        paid_total = 0

    # Attach paid_total so the template can do: invoice.paid_total
    try:
        setattr(inv, "paid_total", paid_total)
    except Exception:
        pass

    branch = None  # optional

    return render_template(
        "invoice_print.html",
        invoice=inv,
        patient=inv.patient,
        branch=branch
    )


@bp.get("/billing/receipt/<int:payment_id>/print", endpoint="receipt_print")
@login_required
@roles_required("reception", "nurse", "admin")
def receipt_print(payment_id):
    pay = Payment.query.get_or_404(payment_id)

    inv = Invoice.query.get(pay.invoice_id) if getattr(pay, "invoice_id", None) else None
    p = inv.patient if inv and hasattr(inv, "patient") else None

    # Build safe line items for the template
    safe_lines = []
    subtotal = (inv.amount if inv else (pay.amount or 0)) or 0

    if inv and getattr(inv, "lines", None):
        for ln in inv.lines:
            safe_lines.append({
                "description": ln.description,
                "qty": ln.qty,
                "unit_price": ln.unit_price,
                "line_total": ln.line_total,
            })

    paid_to_date = 0
    if inv and getattr(inv, "payments", None):
        paid_to_date = sum((pp.amount or 0) for pp in inv.payments)

    balance = (subtotal or 0) - (paid_to_date or 0)

    return render_template(
        "receipt_print.html",
        pay=pay,
        inv=inv,
        p=p,
        safe_lines=safe_lines,
        subtotal=subtotal,
        paid_to_date=paid_to_date,
        balance=balance,
    )
