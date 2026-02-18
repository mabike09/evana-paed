# app/routes/reports.py
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

from flask import Blueprint, Response, render_template, request
from flask_login import login_required
from ..permissions import roles_required
from ..models import BillingQueue, DispenseTxn, Invoice, InvoiceLine, Item, Patient, Payment, Visit

bp = Blueprint("reports", __name__)

@bp.route("/reports/aging")
@login_required
@roles_required("admin")
def aging_report():
    as_of = request.args.get("as_of")
    as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date() if as_of else datetime.today().date()
    invoices = Invoice.query.all()
    buckets = {"Current (<30)": [], "30-59": [], "60-89": [], "90+": []}
    for inv in invoices:
        bal = inv.balance
        if bal <= 0: continue
        age = (as_of_date - datetime.strptime(inv.issue_date, "%Y-%m-%d").date()).days
        if age < 30: buckets["Current (<30)"].append(inv)
        elif age < 60: buckets["30-59"].append(inv)
        elif age < 90: buckets["60-89"].append(inv)
        else: buckets["90+"].append(inv)
    return render_template("report_aging.html", buckets=buckets, as_of=as_of_date)

@bp.route("/reports/payer_split")
@login_required
@roles_required("admin")
def payer_split():
    invoices = Invoice.query.all()
    totals = {"Cash": {"billed": 0, "paid": 0}, "Insurance": {"billed": 0, "paid": 0}}
    for inv in invoices:
        if not inv.payer_type: continue
        totals[inv.payer_type]["billed"] += float(inv.amount or 0)
        totals[inv.payer_type]["paid"] += float(inv.paid_total or 0)
    return render_template("report_payer_split.html", totals=totals)


def _parse_date_param(raw_date, fallback):
    if not raw_date:
        return fallback
    try:
        return datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def _as_money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _contains_any(text, keywords):
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


@bp.route("/reports")
@login_required
@roles_required("reception", "nurse", "admin")
def reports_dashboard():
    today = datetime.utcnow().date()
    start_date = _parse_date_param(request.args.get("start_date"), today)
    end_date = _parse_date_param(request.args.get("end_date"), today)
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time())

    visits_in_range = Visit.query.filter(Visit.created_at >= start_dt, Visit.created_at < end_dt).all()
    patient_ids = list({v.patient_id for v in visits_in_range if v.patient_id})

    first_visits = {}
    if patient_ids:
        for pid in patient_ids:
            fv = (
                Visit.query.filter_by(patient_id=pid)
                .order_by(Visit.created_at.asc())
                .first()
            )
            if fv and fv.created_at:
                first_visits[pid] = fv.created_at

    new_patients = 0
    returning_patients = 0
    booked_visits = 0
    walk_in_visits = 0

    for visit in visits_in_range:
        first_seen_at = first_visits.get(visit.patient_id)
        if first_seen_at and start_dt <= first_seen_at < end_dt:
            new_patients += 1
        else:
            returning_patients += 1

        if _contains_any(visit.reason, ["book", "appointment", "follow up", "follow-up"]):
            booked_visits += 1
        else:
            walk_in_visits += 1

    # Queue / waiting-time report
    queue_entries = (
        BillingQueue.query
        .filter(BillingQueue.visit_id.isnot(None))
        .filter(BillingQueue.kind.in_(["TRIAGE", "PHARMACY"]))
        .filter(BillingQueue.added_at >= start_dt, BillingQueue.added_at < end_dt)
        .all()
    )

    queue_by_visit = defaultdict(list)
    for entry in queue_entries:
        queue_by_visit[entry.visit_id].append(entry)

    dispense_by_visit = defaultdict(list)
    for txn in DispenseTxn.query.filter(DispenseTxn.when >= start_dt, DispenseTxn.when < end_dt).all():
        if txn.visit_id:
            dispense_by_visit[txn.visit_id].append(txn)

    visit_ids = [v.id for v in visits_in_range]
    invoice_lines = InvoiceLine.query.join(Invoice, Invoice.id == InvoiceLine.invoice_id).filter(Invoice.visit_id.in_(visit_ids)).all() if visit_ids else []
    drug_visits = {line.invoice.visit_id for line in invoice_lines if (line.kind or "").lower() == "drug" and line.invoice and line.invoice.visit_id}

    turnaround_minutes = []
    for visit in visits_in_range:
        starts = [q.added_at for q in queue_by_visit.get(visit.id, []) if q.added_at]
        start_ts = min(starts) if starts else visit.created_at
        if not start_ts:
            continue

        if visit.id in drug_visits:
            clears = [d.when for d in dispense_by_visit.get(visit.id, []) if d.when]
            end_ts = max(clears) if clears else visit.closed_at
        else:
            end_ts = visit.closed_at

        if end_ts and end_ts >= start_ts:
            turnaround_minutes.append((end_ts - start_ts).total_seconds() / 60)

    avg_turnaround_mins = round(sum(turnaround_minutes) / len(turnaround_minutes), 1) if turnaround_minutes else 0

    # Revenue & collections report
    payments_in_range = Payment.query.filter(Payment.payment_date >= str(start_date), Payment.payment_date <= str(end_date)).all()
    invoices_in_range = Invoice.query.filter(Invoice.issue_date >= str(start_date), Invoice.issue_date <= str(end_date)).all()

    total_collected_cash = _as_money(sum(_as_money(p.amount) for p in payments_in_range if (p.method or "").lower() == "cash"))
    total_collected_mobile = _as_money(sum(_as_money(p.amount) for p in payments_in_range if "mobile" in (p.method or "").lower()))
    total_collected = _as_money(total_collected_cash + total_collected_mobile)

    total_sales_cash = _as_money(sum(_as_money(inv.amount) for inv in invoices_in_range if (inv.payer_type or "") == "Cash"))
    total_sales_insurance = _as_money(sum(_as_money(inv.amount) for inv in invoices_in_range if (inv.payer_type or "") == "Insurance"))
    total_sales_mobile = total_collected_mobile
    total_sales = _as_money(total_sales_cash + total_sales_mobile + total_sales_insurance)

    insurance_company_totals = defaultdict(Decimal)
    for inv in invoices_in_range:
        if (inv.payer_type or "") != "Insurance":
            continue
        patient = Patient.query.get(inv.patient_id) if inv.patient_id else None
        insurer_name = (patient.insurance_provider.strip() if patient and patient.insurance_provider else "Unspecified")
        insurance_company_totals[insurer_name] += _as_money(inv.amount)

    insurance_company_rows = sorted(insurance_company_totals.items(), key=lambda row: row[0].lower())

    # Pharmacy / inventory report
    items = Item.query.order_by(Item.name.asc()).all()
    low_stock = [item for item in items if item.current_qty > 0 and item.current_qty <= item.min_level]
    out_of_stock = [item for item in items if item.current_qty <= 0]

    context = {
        "start_date": start_date,
        "end_date": end_date,
        "patient_traffic": {
            "new": new_patients,
            "returning": returning_patients,
            "walk_in": walk_in_visits,
            "booked": booked_visits,
            "total": len(visits_in_range),
        },
        "queue_report": {
            "average_turnaround_minutes": avg_turnaround_mins,
            "visits_measured": len(turnaround_minutes),
        },
        "collections": {
            "collected_cash": total_collected_cash,
            "collected_mobile": total_collected_mobile,
            "collected_total": total_collected,
            "sales_cash": total_sales_cash,
            "sales_mobile": total_sales_mobile,
            "sales_insurance": total_sales_insurance,
            "sales_total": total_sales,
        },
        "insurance_company_rows": insurance_company_rows,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
    }

    if request.args.get("export") == "pdf":
        html = render_template("reports_dashboard_pdf.html", **context)
        try:
            from xhtml2pdf import pisa
        except Exception:
            return Response(html, mimetype="text/html")

        pdf_io = BytesIO()
        result = pisa.CreatePDF(src=html, dest=pdf_io, encoding="utf-8")
        if result.err:
            return Response(html, mimetype="text/html")
        filename = f"reports_{start_date}_{end_date}.pdf"
        return Response(
            pdf_io.getvalue(),
            mimetype="application/pdf",
            headers={"Content-Disposition": f"inline; filename={filename}"},
        )

    return render_template("reports_dashboard.html", **context)
