# app/routes/reports.py
from flask import Blueprint, render_template, request
from flask_login import login_required
from ..permissions import roles_required
from ..models import Invoice
from datetime import datetime

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
