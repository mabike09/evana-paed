from collections import Counter
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import CLAIM_STATUS_LABELS, CLAIM_STATUSES, InsuranceClaim, Invoice, Patient, Payment
from ..permissions import roles_required
from ..timezone import eat_now

bp = Blueprint("claims", __name__, url_prefix="/claims")


def _money(value):
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _is_insurance_invoice(inv):
    if not inv:
        return False
    if (getattr(inv, "payer_type", "") or "").strip().lower() == "insurance":
        return True
    insurer = (getattr(inv.patient, "insurance_provider", "") or "").strip().lower() if inv.patient else ""
    return bool(insurer and insurer != "cash")


def _claim_insurer_name(patient):
    insurer = (getattr(patient, "insurance_provider", None) or "").strip()
    if not insurer or insurer.lower() == "cash":
        return "Insurance"
    return insurer


def ensure_claim_for_invoice(inv):
    if not _is_insurance_invoice(inv):
        return None
    existing = getattr(inv, "insurance_claim", None)
    if existing:
        if _money(existing.expected_amount) != _money(inv.amount):
            existing.expected_amount = _money(inv.amount)
        return existing
    patient = inv.patient
    claim = InsuranceClaim(
        invoice_id=inv.id,
        patient_id=inv.patient_id,
        insurer_name=_claim_insurer_name(patient),
        policy_number=getattr(patient, "policy_number", None),
        status="draft",
        expected_amount=_money(inv.amount),
    )
    db.session.add(claim)
    return claim


def _sync_missing_claims():
    invoices = (
        Invoice.query.options(joinedload(Invoice.patient))
        .filter(func.lower(func.coalesce(Invoice.payer_type, "")) == "insurance")
        .all()
    )
    created = 0
    for inv in invoices:
        if not getattr(inv, "insurance_claim", None):
            ensure_claim_for_invoice(inv)
            created += 1
    if created:
        db.session.commit()
    return created


def _paid_total(invoice_id):
    total = Decimal("0.00")
    for payment in Payment.query.filter_by(invoice_id=invoice_id).all():
        total += _money(payment.amount)
    return total


def _payment_date_from_form(default):
    paid_date = (request.form.get("paid_date") or "").strip()
    if not paid_date:
        return default
    try:
        return datetime.strptime(paid_date, "%Y-%m-%d")
    except ValueError:
        flash("Invalid payment date. Using today instead.", "warning")
        return default


def _advance_claim(claim, new_status):
    now = eat_now()
    claim.status = new_status
    if new_status == "submitted_to_claims_officer":
        claim.verified_by_id = getattr(current_user, "id", None)
        claim.verified_at = claim.verified_at or now
        claim.submitted_to_officer_at = now
    elif new_status == "submitted_to_insurance":
        claim.officer_id = getattr(current_user, "id", None)
        claim.submitted_to_insurance_at = now
    elif new_status == "paid":
        claim.paid_at = _payment_date_from_form(now)
        claim.paid_amount = _money(request.form.get("paid_amount") or _paid_total(claim.invoice_id) or claim.expected_amount)
        claim.insurer_reference = (request.form.get("insurer_reference") or claim.insurer_reference or "").strip()
    elif new_status == "reconciled":
        claim.reconciled_at = now
        claim.reconciliation_notes = (request.form.get("reconciliation_notes") or claim.reconciliation_notes or "").strip()
    elif new_status == "rejected":
        claim.rejected_at = now
        claim.rejection_reason = (request.form.get("rejection_reason") or claim.rejection_reason or "").strip()
    elif new_status == "closed":
        claim.closed_at = now
    notes = (request.form.get("follow_up_notes") or "").strip()
    if notes:
        claim.follow_up_notes = notes


@bp.get("/")
@login_required
@roles_required("claims_officer", "claims_manager", "reception", "admin")
def dashboard():
    _sync_missing_claims()
    status_filter = (request.args.get("status") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    insurer_filter = (request.args.get("insurer") or "").strip()

    insurer_choices = [
        row[0]
        for row in db.session.query(InsuranceClaim.insurer_name)
        .filter(
            InsuranceClaim.insurer_name.isnot(None),
            InsuranceClaim.insurer_name != "",
            func.lower(InsuranceClaim.insurer_name) != "cash",
        )
        .distinct()
        .order_by(InsuranceClaim.insurer_name.asc())
        .all()
    ]

    q = InsuranceClaim.query.options(joinedload(InsuranceClaim.invoice), joinedload(InsuranceClaim.patient)).join(Invoice)
    counts_q = InsuranceClaim.query.join(Invoice)
    if start_date:
        q = q.filter(Invoice.issue_date >= start_date)
        counts_q = counts_q.filter(Invoice.issue_date >= start_date)
    if end_date:
        q = q.filter(Invoice.issue_date <= end_date)
        counts_q = counts_q.filter(Invoice.issue_date <= end_date)
    if insurer_filter:
        q = q.filter(InsuranceClaim.insurer_name == insurer_filter)
        counts_q = counts_q.filter(InsuranceClaim.insurer_name == insurer_filter)
    if status_filter in CLAIM_STATUSES:
        q = q.filter(InsuranceClaim.status == status_filter)
    claims = q.order_by(InsuranceClaim.updated_at.desc(), InsuranceClaim.id.desc()).all()
    counts = Counter(claim.status for claim in counts_q.all())
    filters = {
        "status": status_filter,
        "start_date": start_date,
        "end_date": end_date,
        "insurer": insurer_filter,
    }
    totals = {
        "expected": sum((_money(c.expected_amount) for c in claims), Decimal("0.00")),
        "paid": sum((_money(c.paid_amount) for c in claims), Decimal("0.00")),
    }
    totals["outstanding"] = totals["expected"] - totals["paid"]
    return render_template(
        "claims_dashboard.html",
        claims=claims,
        statuses=CLAIM_STATUSES,
        status_labels=CLAIM_STATUS_LABELS,
        status_filter=status_filter,
        counts=counts,
        totals=totals,
        filters=filters,
        insurer_choices=insurer_choices,
    )


@bp.post("/invoices/<int:invoice_id>/verify")
@login_required
@roles_required("reception", "admin")
def verify_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    claim = ensure_claim_for_invoice(inv)
    if not claim:
        flash("Only insurance invoices can be submitted to the claims officer.", "warning")
        return redirect(url_for("billing.patient_billing", patient_id=inv.patient_id))
    _advance_claim(claim, "submitted_to_claims_officer")
    db.session.commit()
    flash("Insurance verified and submitted to claims officer.", "success")
    return redirect(url_for("billing.patient_billing", patient_id=inv.patient_id))


@bp.post("/<int:claim_id>/status")
@login_required
@roles_required("claims_officer", "claims_manager", "admin")
def update_status(claim_id):
    claim = InsuranceClaim.query.get_or_404(claim_id)
    new_status = (request.form.get("status") or "").strip()
    if new_status not in CLAIM_STATUSES:
        flash("Invalid claim status.", "danger")
        return redirect(url_for("claims.dashboard"))
    _advance_claim(claim, new_status)
    db.session.commit()
    flash(f"Claim updated to {CLAIM_STATUS_LABELS[new_status]}.", "success")
    return redirect(
        url_for(
            "claims.dashboard",
            status=request.args.get("status", ""),
            start_date=request.args.get("start_date", ""),
            end_date=request.args.get("end_date", ""),
            insurer=request.args.get("insurer", ""),
        )
    )
