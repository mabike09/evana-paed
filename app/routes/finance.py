from __future__ import annotations

import csv
import io
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, inspect, text
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import (
    APAttachment,
    APAuditLog,
    APBill,
    APBillLine,
    APPayment,
    Payment,
    APRecurringTemplate,
    APSupplier,
    ExpenseCategory,
    ExpenseEntry,
    PettyCashAuditLog,
    PettyCashPeriodLock,
    PettyCashReconciliation,
    PettyCashTransaction,
)
from ..permissions import roles_required
from ..timezone import eat_now, eat_today

bp = Blueprint("finance", __name__)

ALLOWED_ATTACHMENT_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif", "doc", "docx", "xls", "xlsx"}
PETTY_CASH_CATEGORIES = [
    "transport",
    "office supplies",
    "cleaning materials",
    "airtime/data",
    "staff welfare",
    "minor repairs",
    "fuel",
    "miscellaneous",
    "dentist retainer",
    "dental consumables",
]
PETTY_CASH_TXN_TYPES = ["cash_in", "cash_out"]
ENTRY_ROLES = {"reception", "receptionist", "nurse", "branch_manager", "branch manager", "admin", "accountant"}
FINANCE_MANAGER_ROLES = {"admin", "accountant"}
LOW_BALANCE_THRESHOLD = Decimal("100000.00")
AP_SUPPLIER_CATEGORIES = [
    "drugs and medical supplies",
    "laboratory supplies",
    "equipment vendors",
    "utility providers",
    "rent/landlord",
    "marketing/service providers",
    "maintenance and repair providers",
    "transport and fuel providers",
    "casual service providers",
]
AP_PAYMENT_TERMS = ["7 days", "14 days", "30 days", "cash on delivery"]
AP_PAYMENT_METHODS = ["cash", "bank transfer", "mobile money", "cheque", "eft"]
AP_EXPENSE_CATEGORIES = [
    "drug supplies",
    "laboratory supplies",
    "staff welfare",
    "utilities",
    "rent",
    "repairs and maintenance",
    "fuel and transport",
    "cleaning supplies",
    "marketing",
    "professional fees",
    "equipment purchase",
    "equipment servicing",
    "petty cash",
    "referral commission",
    "stationary",
    "office supplies",
]
AP_ATTACHMENT_TYPES = [
    "supplier invoice",
    "delivery note",
    "signed goods received note",
    "requisition form",
    "local purchase order",
    "approval note",
    "contract or service agreement",
    "payment proof",
    "supplier statement",
]


def _money(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _user_role() -> str:
    return (getattr(current_user, "role", "") or "").strip().lower()


def _can_manage_finance() -> bool:
    return _user_role() in FINANCE_MANAGER_ROLES or _user_role() == "admin"


def _can_manage_petty_cash() -> bool:
    return _user_role() == "admin"


def _can_record_petty_cash_in() -> bool:
    return _user_role() in {"accountant", "admin"}


def _can_record_petty_cash_out() -> bool:
    return _user_role() in {"reception", "receptionist", "nurse", "branch_manager", "branch manager", "admin"}


def _can_enter_transactions() -> bool:
    return _can_record_petty_cash_in() or _can_record_petty_cash_out()


def _allowed_attachment(filename: str) -> bool:
    if "." not in (filename or ""):
        return False
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_ATTACHMENT_EXTENSIONS


def _attachment_folder() -> str:
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "petty_cash")
    os.makedirs(folder, exist_ok=True)
    return folder


def _log_action(action: str, record_type: str, record_id: int | None, changes: dict):
    db.session.add(
        PettyCashAuditLog(
            action=action,
            record_type=record_type,
            record_id=record_id,
            actor_username=getattr(current_user, "username", "system"),
            actor_role=getattr(current_user, "role", ""),
            changes_json=json.dumps(changes, default=str),
        )
    )


def _ap_attachment_folder() -> str:
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "accounts_payable")
    os.makedirs(folder, exist_ok=True)
    return folder


def _ap_log(action: str, entity: str, entity_id: int | None, summary: dict):
    db.session.add(
        APAuditLog(
            action=action,
            entity=entity,
            entity_id=entity_id,
            actor_username=getattr(current_user, "username", "system"),
            actor_role=getattr(current_user, "role", ""),
            change_summary=json.dumps(summary, default=str),
        )
    )


def _ap_save_attachment(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    if not _allowed_attachment(filename):
        raise ValueError("Unsupported attachment type.")
    stamped = f"{eat_now().strftime('%Y%m%d%H%M%S')}_{filename}"
    full_path = os.path.join(_ap_attachment_folder(), stamped)
    file_storage.save(full_path)
    return os.path.join("accounts_payable", stamped)


def _ap_bill_paid_total(bill: APBill) -> Decimal:
    return _money(sum((_money(p.amount) for p in bill.payments), Decimal("0.00")))


def _ap_bill_balance(bill: APBill) -> Decimal:
    return _money(_money(bill.invoice_total) - _ap_bill_paid_total(bill))


def _ap_bill_bucket(bill: APBill, today: date) -> str:
    balance = _ap_bill_balance(bill)
    if balance <= 0:
        return "paid"
    due = _parse_date(bill.due_date, today)
    if not due or due >= today:
        return "current"
    overdue_days = (today - due).days
    if overdue_days <= 30:
        return "1-30"
    if overdue_days <= 60:
        return "31-60"
    if overdue_days <= 90:
        return "61-90"
    return "90+"


def _ap_refresh_status(bill: APBill):
    balance = _ap_bill_balance(bill)
    if balance <= 0:
        bill.status = "paid"
    elif balance < _money(bill.invoice_total):
        bill.status = "partial"
    else:
        bill.status = "unpaid"


def _ap_generate_recurring_bills(today: date):
    templates = APRecurringTemplate.query.filter(APRecurringTemplate.is_active.is_(True)).all()
    for template in templates:
        if template.frequency != "monthly":
            continue
        due_day = min(max(1, template.due_day or 1), 28)
        due_date = date(today.year, today.month, due_day)
        due_date_raw = str(due_date)
        existing = APBill.query.filter(
            APBill.recurring_template_id == template.id,
            APBill.due_date == due_date_raw,
        ).first()
        if existing:
            continue
        invoice_number = f"RC-{template.id}-{today.strftime('%Y%m')}"
        bill = APBill(
            supplier_id=template.supplier_id,
            invoice_number=invoice_number,
            invoice_date=str(today),
            due_date=due_date_raw,
            expense_category=template.expense_category,
            description=f"Auto-generated recurring payable: {template.template_name}",
            invoice_total=_money(template.amount),
            tax_amount=_money(template.tax_amount),
            submitted_by="system",
            submitted_date=str(today),
            status="unpaid",
            is_recurring=True,
            recurring_template_id=template.id,
            created_by="system",
            updated_by="system",
        )
        db.session.add(bill)
        db.session.flush()
        db.session.add(
            APBillLine(
                bill_id=bill.id,
                line_description=template.template_name,
                amount=_money(template.amount),
                expense_category=template.expense_category,
            )
        )
        template.last_generated_on = str(today)
        _ap_log("auto_generate", "bill", bill.id, {"template": template.template_name, "due_date": due_date_raw})


def _parse_date(raw: str | None, fallback: date | None = None) -> date | None:
    if not raw:
        return fallback
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def _get_filters():
    today = eat_today()
    start_date = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end_date = _parse_date(request.args.get("end_date"), today)
    if start_date and end_date and end_date < start_date:
        start_date, end_date = end_date, start_date
    return {
        "start_date": start_date,
        "end_date": end_date,
        "category": (request.args.get("category") or "").strip(),
        "transaction_type": (request.args.get("transaction_type") or "").strip(),
        "voucher_number": (request.args.get("voucher_number") or "").strip(),
        "payee": (request.args.get("payee") or "").strip(),
        "status": (request.args.get("status") or "").strip(),
        "entered_by": (request.args.get("entered_by") or "").strip(),
        "approved_by": (request.args.get("approved_by") or "").strip(),
    }


def _apply_filters(query, filters):
    if filters["start_date"]:
        query = query.filter(PettyCashTransaction.date >= str(filters["start_date"]))
    if filters["end_date"]:
        query = query.filter(PettyCashTransaction.date <= str(filters["end_date"]))
    if filters["category"]:
        query = query.filter(PettyCashTransaction.expense_category == filters["category"])
    if filters["transaction_type"]:
        query = query.filter(PettyCashTransaction.transaction_type == filters["transaction_type"])
    if filters["voucher_number"]:
        query = query.filter(PettyCashTransaction.voucher_number.ilike(f"%{filters['voucher_number']}%"))
    if filters["payee"]:
        query = query.filter(PettyCashTransaction.payee.ilike(f"%{filters['payee']}%"))
    if filters["entered_by"]:
        query = query.filter(PettyCashTransaction.entered_by == filters["entered_by"])
    if filters["approved_by"]:
        query = query.filter(PettyCashTransaction.approved_by == filters["approved_by"])
    if filters["status"] == "unreconciled":
        query = query.filter(PettyCashTransaction.reconciliation_id.is_(None))
    elif filters["status"] == "reconciled":
        query = query.filter(PettyCashTransaction.reconciliation_id.isnot(None))
    elif filters["status"] == "missing_attachment":
        query = query.filter((PettyCashTransaction.attachment_path.is_(None)) | (PettyCashTransaction.attachment_path == ""))
    elif filters["status"] == "unaccounted_advance":
        query = query.filter(PettyCashTransaction.is_advance.is_(True), PettyCashTransaction.is_accounted.is_(False))
    return query


def _build_ledger_rows(transactions, opening_balance: Decimal = Decimal("0.00")):
    rows = []
    running_balance = _money(opening_balance)
    for txn in transactions:
        previous = running_balance
        amount_in = _money(txn.amount) if txn.transaction_type == "cash_in" else Decimal("0.00")
        amount_out = _money(txn.amount) if txn.transaction_type == "cash_out" else Decimal("0.00")
        running_balance = _money(previous + amount_in - amount_out)
        rows.append({
            "transaction": txn,
            "previous_balance": previous,
            "amount_in": amount_in,
            "amount_out": amount_out,
            "new_balance": running_balance,
        })
    return rows, running_balance


def _all_transactions():
    return PettyCashTransaction.query.order_by(PettyCashTransaction.date.asc(), PettyCashTransaction.id.asc())


def _opening_balance(start_date: date | None) -> Decimal:
    if not start_date:
        return Decimal("0.00")
    prior = PettyCashTransaction.query.filter(PettyCashTransaction.date < str(start_date)).order_by(PettyCashTransaction.date.asc(), PettyCashTransaction.id.asc()).all()
    _, balance = _build_ledger_rows(prior)
    return balance


def _filtered_transactions(filters):
    return _apply_filters(_all_transactions(), filters).all()


def _current_balance() -> Decimal:
    _, balance = _build_ledger_rows(_all_transactions().all())
    return balance


def _latest_lock():
    return PettyCashPeriodLock.query.order_by(PettyCashPeriodLock.locked_until.desc(), PettyCashPeriodLock.id.desc()).first()


def _ensure_open_period(txn_date: date) -> bool:
    lock = _latest_lock()
    if lock and txn_date <= lock.locked_until:
        flash(f"Transactions up to {lock.locked_until} are locked after month-end reconciliation.", "warning")
        return False
    return True


def _summary_metrics(ledger_rows, filtered_transactions):
    txns = filtered_transactions
    cash_in_total = _money(sum((_money(t.amount) for t in txns if t.transaction_type == "cash_in"), Decimal("0.00")))
    cash_out_total = _money(sum((_money(t.amount) for t in txns if t.transaction_type == "cash_out"), Decimal("0.00")))
    category_totals = defaultdict(Decimal)
    user_totals = defaultdict(Decimal)
    monthly = defaultdict(lambda: {"cash_in": Decimal("0.00"), "cash_out": Decimal("0.00")})
    for txn in txns:
        if txn.transaction_type == "cash_out":
            category_totals[txn.expense_category or "Uncategorized"] += _money(txn.amount)
            user_totals[txn.entered_by or "Unknown"] += _money(txn.amount)
        bucket = (txn.date or "")[:7] or "Unknown"
        monthly[bucket][txn.transaction_type] += _money(txn.amount)

    missing_attachment = [t for t in txns if not t.attachment_path]
    unreconciled = [t for t in txns if not t.reconciliation_id]
    unaccounted_advances = [t for t in txns if t.is_advance and not t.is_accounted]
    alerts = []
    current_balance = _current_balance()
    if current_balance < LOW_BALANCE_THRESHOLD:
        alerts.append(f"Petty cash balance is below UGX 100,000. Current balance: UGX {current_balance:,.2f}")
    if missing_attachment:
        alerts.append(f"{len(missing_attachment)} transaction(s) are missing receipt/voucher/accountability attachments.")

    return {
        "current_balance": current_balance,
        "cash_in_total": cash_in_total,
        "cash_out_total": cash_out_total,
        "category_totals": sorted(category_totals.items(), key=lambda item: item[0].lower()),
        "user_totals": sorted(user_totals.items(), key=lambda item: item[0].lower()),
        "monthly_rows": sorted(monthly.items()),
        "missing_attachment": missing_attachment,
        "unreconciled": unreconciled,
        "unaccounted_advances": unaccounted_advances,
        "alerts": alerts,
        "latest_row": ledger_rows[-1] if ledger_rows else None,
    }


def _save_attachment(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    if not _allowed_attachment(filename):
        raise ValueError("Unsupported attachment type.")
    stamped = f"{eat_now().strftime('%Y%m%d%H%M%S')}_{filename}"
    full_path = os.path.join(_attachment_folder(), stamped)
    file_storage.save(full_path)
    return os.path.join("petty_cash", stamped)


@bp.route("/finance")
@login_required
@roles_required("reception", "nurse", "accountant", "branch_manager", "admin")
def finance_home():
    return redirect(url_for("finance.petty_cash_ledger"))


@bp.route("/finance/expense-tracker", methods=["GET", "POST"])
@login_required
@roles_required("accountant", "admin")
def expense_tracker():
    today = str(eat_today())
    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        if action == "add_category":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Category name is required.", "warning")
            else:
                exists = ExpenseCategory.query.filter(func.lower(ExpenseCategory.name) == name.lower()).first()
                if exists:
                    flash("Category already exists.", "warning")
                else:
                    db.session.add(ExpenseCategory(name=name, created_by=current_user.username, is_active=True))
                    db.session.commit()
                    flash("Expense category added.", "success")
            return redirect(url_for("finance.expense_tracker"))
        if action == "add_expense":
            try:
                category_id = int(request.form.get("category_id") or 0)
            except ValueError:
                category_id = 0
            category = ExpenseCategory.query.get(category_id) if category_id else None
            if not category:
                flash("Select a valid expense category.", "warning")
                return redirect(url_for("finance.expense_tracker"))
            db.session.add(
                ExpenseEntry(
                    expense_date=(request.form.get("expense_date") or today).strip(),
                    category_id=category.id,
                    description=(request.form.get("description") or "").strip(),
                    vendor_payee=(request.form.get("vendor_payee") or "").strip(),
                    reference=(request.form.get("reference") or "").strip() or None,
                    amount=_money(request.form.get("amount")),
                    entered_by=current_user.username,
                )
            )
            db.session.commit()
            flash("Expense recorded.", "success")
            return redirect(url_for("finance.expense_tracker"))

    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    query = ExpenseEntry.query.join(ExpenseCategory).order_by(ExpenseEntry.expense_date.desc(), ExpenseEntry.id.desc())
    if start_date:
        query = query.filter(ExpenseEntry.expense_date >= start_date)
    if end_date:
        query = query.filter(ExpenseEntry.expense_date <= end_date)

    categories = ExpenseCategory.query.filter_by(is_active=True).order_by(ExpenseCategory.name.asc()).all()
    expenses = query.all()
    return render_template(
        "finance_expense_tracker.html",
        today=today,
        categories=categories,
        expenses=expenses,
        start_date=start_date,
        end_date=end_date,
    )


@bp.route("/finance/accounts-payable", methods=["GET", "POST"])
@login_required
@roles_required("accountant", "admin")
def accounts_payable():
    today = eat_today()
    _ap_generate_recurring_bills(today)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "create_supplier":
            supplier_name = (request.form.get("supplier_name") or "").strip()
            existing_supplier = APSupplier.query.filter(APSupplier.supplier_name.ilike(supplier_name)).first() if supplier_name else None
            if existing_supplier:
                flash("Supplier already exists. Use the existing supplier record instead of creating a duplicate.", "warning")
                return redirect(url_for("finance.accounts_payable"))
            supplier = APSupplier(
                supplier_name=supplier_name,
                contact_person=(request.form.get("contact_person") or "").strip(),
                phone_number=(request.form.get("phone_number") or "").strip(),
                email=(request.form.get("email") or "").strip(),
                physical_address=(request.form.get("physical_address") or "").strip(),
                category=(request.form.get("category") or "").strip(),
                payment_terms=(request.form.get("payment_terms") or "30 days").strip(),
                payment_details=(request.form.get("payment_details") or "").strip(),
                tax_details=(request.form.get("tax_details") or "").strip(),
                is_active=(request.form.get("is_active") == "on"),
                opening_balance=_money(request.form.get("opening_balance") or 0),
            )
            if not supplier.supplier_name:
                flash("Supplier name is required.", "warning")
                return redirect(url_for("finance.accounts_payable"))
            db.session.add(supplier)
            try:
                db.session.flush()
                _ap_log("create", "supplier", supplier.id, {"supplier_name": supplier.supplier_name})
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Supplier already exists. Duplicate supplier names are not allowed.", "warning")
                return redirect(url_for("finance.accounts_payable"))
            flash("Supplier saved.", "success")
            return redirect(url_for("finance.accounts_payable"))

        if action == "create_bill":
            supplier = APSupplier.query.get(int(request.form.get("supplier_id") or 0))
            if not supplier:
                flash("Select a valid supplier.", "warning")
                return redirect(url_for("finance.accounts_payable"))
            invoice_number = (request.form.get("invoice_number") or "").strip()
            existing = APBill.query.filter_by(supplier_id=supplier.id, invoice_number=invoice_number).first()
            if existing:
                flash("Duplicate invoice number warning for this supplier.", "danger")
                return redirect(url_for("finance.accounts_payable"))

            bill = APBill(
                supplier_id=supplier.id,
                invoice_number=invoice_number,
                invoice_date=str(_parse_date(request.form.get("invoice_date"), today)),
                due_date=str(_parse_date(request.form.get("due_date"), today)),
                expense_category=(request.form.get("expense_category") or "").strip(),
                description=(request.form.get("description") or "").strip(),
                invoice_total=_money(request.form.get("invoice_total") or 0),
                tax_amount=_money(request.form.get("tax_amount") or 0),
                submitted_by=(request.form.get("submitted_by") or getattr(current_user, "username", "")).strip(),
                submitted_date=str(_parse_date(request.form.get("submitted_date"), today)),
                status="unpaid",
                is_recurring=(request.form.get("is_recurring") == "on"),
                created_by=getattr(current_user, "username", ""),
                updated_by=getattr(current_user, "username", ""),
            )
            if APBill.query.filter_by(invoice_date=bill.invoice_date, invoice_total=bill.invoice_total).first():
                flash("Duplicate warning: another invoice has same amount and date.", "warning")

            db.session.add(bill)
            try:
                db.session.flush()
            except IntegrityError:
                db.session.rollback()
                flash("Duplicate invoice number for this supplier detected. Bill was not saved.", "warning")
                return redirect(url_for("finance.accounts_payable"))
            line_descriptions = request.form.getlist("line_description[]")
            line_amounts = request.form.getlist("line_amount[]")
            line_categories = request.form.getlist("line_category[]")
            for idx, line_desc in enumerate(line_descriptions):
                line_desc = (line_desc or "").strip()
                if not line_desc:
                    continue
                db.session.add(
                    APBillLine(
                        bill_id=bill.id,
                        line_description=line_desc,
                        amount=_money(line_amounts[idx] if idx < len(line_amounts) else 0),
                        expense_category=((line_categories[idx] if idx < len(line_categories) else bill.expense_category) or bill.expense_category).strip(),
                    )
                )

            doc_type = (request.form.get("document_type") or "supplier invoice").strip()
            for uploaded in request.files.getlist("attachments"):
                if not uploaded or not uploaded.filename:
                    continue
                path = _ap_save_attachment(uploaded)
                db.session.add(APAttachment(bill_id=bill.id, document_type=doc_type, file_path=path, uploaded_by=getattr(current_user, "username", "")))

            _ap_log("create", "bill", bill.id, {"supplier": supplier.supplier_name, "invoice_number": invoice_number})
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Bill could not be saved because it conflicts with an existing record.", "warning")
                return redirect(url_for("finance.accounts_payable"))
            flash("Bill captured successfully.", "success")
            return redirect(url_for("finance.accounts_payable"))

        if action == "record_payment":
            bill = APBill.query.get_or_404(int(request.form.get("bill_id") or 0))
            amount = _money(request.form.get("amount") or 0)
            if amount > _ap_bill_balance(bill):
                flash("Supplier overpayment warning: amount exceeds bill balance.", "danger")
                return redirect(url_for("finance.accounts_payable"))
            proof = request.files.get("payment_proof")
            proof_path = _ap_save_attachment(proof) if proof and proof.filename else None
            payment = APPayment(
                bill_id=bill.id,
                payment_date=str(_parse_date(request.form.get("payment_date"), today)),
                amount=amount,
                method=(request.form.get("method") or "cash").strip().lower(),
                reference_number=(request.form.get("reference_number") or "").strip(),
                paying_account=(request.form.get("paying_account") or "").strip(),
                processed_by=(request.form.get("processed_by") or getattr(current_user, "username", "")).strip(),
                proof_attachment_path=proof_path,
            )
            db.session.add(payment)
            _ap_refresh_status(bill)
            _ap_log("payment", "bill", bill.id, {"amount": str(amount), "method": payment.method})
            db.session.commit()
            flash("Payment recorded.", "success")
            return redirect(url_for("finance.accounts_payable"))

        if action == "create_recurring":
            template = APRecurringTemplate(
                supplier_id=int(request.form.get("supplier_id") or 0),
                template_name=(request.form.get("template_name") or "").strip(),
                expense_category=(request.form.get("expense_category") or "").strip(),
                amount=_money(request.form.get("amount") or 0),
                tax_amount=_money(request.form.get("tax_amount") or 0),
                frequency="monthly",
                due_day=int(request.form.get("due_day") or 1),
                reminder_days_before=int(request.form.get("reminder_days_before") or 3),
                is_active=(request.form.get("is_active") == "on"),
                created_by=getattr(current_user, "username", ""),
            )
            db.session.add(template)
            db.session.flush()
            _ap_log("create", "recurring_template", template.id, {"template_name": template.template_name})
            db.session.commit()
            flash("Recurring template saved.", "success")
            return redirect(url_for("finance.accounts_payable"))

    suppliers = APSupplier.query.order_by(APSupplier.supplier_name.asc()).all()
    bills = APBill.query.order_by(APBill.due_date.asc(), APBill.id.desc()).all()
    templates = APRecurringTemplate.query.order_by(APRecurringTemplate.template_name.asc()).all()
    audits = APAuditLog.query.order_by(APAuditLog.created_at.desc()).limit(20).all()

    aging = {"current": Decimal("0.00"), "1-30": Decimal("0.00"), "31-60": Decimal("0.00"), "61-90": Decimal("0.00"), "90+": Decimal("0.00")}
    supplier_totals = defaultdict(Decimal)
    category_totals = defaultdict(Decimal)
    due_this_week, overdue, partials = [], [], []
    duplicate_refs = []
    paid_refs = {(b.supplier_id, b.invoice_number) for b in bills if b.status == "paid"}
    monthly_payments = Decimal("0.00")
    for bill in bills:
        _ap_refresh_status(bill)
        balance = _ap_bill_balance(bill)
        bucket = _ap_bill_bucket(bill, today)
        if bucket in aging:
            aging[bucket] += balance
        if balance > 0:
            supplier_totals[bill.supplier.supplier_name] += balance
            category_totals[bill.expense_category] += balance
        due = _parse_date(bill.due_date, today)
        if due and 0 <= (due - today).days <= 7 and balance > 0:
            due_this_week.append(bill)
        if due and due < today and balance > 0:
            overdue.append(bill)
        if bill.status == "partial":
            partials.append(bill)
        if (bill.supplier_id, bill.invoice_number) in paid_refs and bill.status != "paid":
            duplicate_refs.append(bill)
        for payment in bill.payments:
            payment_dt = _parse_date(payment.payment_date, today)
            if payment_dt and payment_dt.year == today.year and payment_dt.month == today.month:
                monthly_payments += _money(payment.amount)

    reminders = []
    for template in templates:
        if not template.is_active:
            continue
        due_day = min(max(1, template.due_day or 1), 28)
        due_dt = date(today.year, today.month, due_day)
        reminder_date = due_dt - timedelta(days=max(0, template.reminder_days_before or 0))
        if reminder_date <= today <= due_dt:
            reminders.append(f"{template.template_name} due on {due_dt}")

    report = (request.args.get("report") or "").strip().lower()
    if report:
        out = io.StringIO()
        writer = csv.writer(out)
        if report == "aging":
            writer.writerow(["Bucket", "Amount"])
            for bucket, amount in aging.items():
                writer.writerow([bucket, f"{amount:.2f}"])
            name = "payables_aging_report.csv"
        elif report == "unpaid":
            writer.writerow(["Supplier", "Invoice Number", "Due Date", "Outstanding"])
            for bill in bills:
                bal = _ap_bill_balance(bill)
                if bal > 0:
                    writer.writerow([bill.supplier.supplier_name, bill.invoice_number, bill.due_date, f"{bal:.2f}"])
            name = "unpaid_bills_report.csv"
        elif report == "payment_history":
            writer.writerow(["Supplier", "Invoice Number", "Payment Date", "Amount", "Method", "Reference", "Processed By"])
            for bill in bills:
                for pay in bill.payments:
                    writer.writerow([bill.supplier.supplier_name, bill.invoice_number, pay.payment_date, f"{_money(pay.amount):.2f}", pay.method, pay.reference_number, pay.processed_by])
            name = "payment_history_report.csv"
        else:
            writer.writerow(["Supplier", "Category", "Invoice", "Total", "Paid", "Outstanding", "Status"])
            for bill in bills:
                writer.writerow([bill.supplier.supplier_name, bill.expense_category, bill.invoice_number, f"{_money(bill.invoice_total):.2f}", f"{_ap_bill_paid_total(bill):.2f}", f"{_ap_bill_balance(bill):.2f}", bill.status])
            name = "accounts_payable_summary.csv"
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})

    db.session.commit()
    return render_template(
        "finance_accounts_payable.html",
        suppliers=suppliers,
        bills=bills,
        templates=templates,
        audits=audits,
        today=today,
        supplier_categories=AP_SUPPLIER_CATEGORIES,
        payment_terms=AP_PAYMENT_TERMS,
        attachment_types=AP_ATTACHMENT_TYPES,
        payment_methods=AP_PAYMENT_METHODS,
        expense_categories=AP_EXPENSE_CATEGORIES,
        ap_bill_balance=_ap_bill_balance,
        ap_bill_paid_total=_ap_bill_paid_total,
        ap_bill_bucket=_ap_bill_bucket,
        aging=aging,
        supplier_totals=sorted(supplier_totals.items(), key=lambda x: x[0].lower()),
        category_totals=sorted(category_totals.items(), key=lambda x: x[0].lower()),
        due_this_week=due_this_week,
        overdue=overdue,
        partials=partials,
        reminders=reminders,
        duplicate_refs=duplicate_refs,
        monthly_payments=monthly_payments,
        total_outstanding=sum(aging.values(), Decimal("0.00")),
        is_admin=_user_role() == "admin",
    )


@bp.route("/finance/accounts-payable/bills/<int:bill_id>/edit", methods=["POST"])
@login_required
@roles_required("admin")
def ap_bill_edit(bill_id: int):
    bill = APBill.query.get_or_404(bill_id)
    before = {"due_date": bill.due_date, "description": bill.description, "invoice_total": str(bill.invoice_total)}
    bill.due_date = str(_parse_date(request.form.get("due_date"), eat_today()))
    bill.description = (request.form.get("description") or bill.description or "").strip()
    bill.invoice_total = _money(request.form.get("invoice_total") or bill.invoice_total)
    bill.tax_amount = _money(request.form.get("tax_amount") or bill.tax_amount)
    bill.expense_category = (request.form.get("expense_category") or bill.expense_category).strip()
    bill.updated_by = getattr(current_user, "username", "")
    _ap_refresh_status(bill)
    _ap_log("edit", "bill", bill.id, {"before": before, "after": {"due_date": bill.due_date, "invoice_total": str(bill.invoice_total)}})
    db.session.commit()
    flash("Bill updated.", "success")
    return redirect(url_for("finance.accounts_payable"))


@bp.route("/finance/accounts-payable/bills/<int:bill_id>/delete", methods=["POST"])
@login_required
@roles_required("admin")
def ap_bill_delete(bill_id: int):
    bill = APBill.query.get_or_404(bill_id)
    _ap_log("delete", "bill", bill.id, {"invoice_number": bill.invoice_number})
    db.session.delete(bill)
    db.session.commit()
    flash("Bill deleted.", "success")
    return redirect(url_for("finance.accounts_payable"))


@bp.route("/finance/accounts-payable/attachment/<int:attachment_id>")
@login_required
@roles_required("accountant", "admin")
def ap_attachment(attachment_id: int):
    attachment = APAttachment.query.get_or_404(attachment_id)
    return send_file(os.path.join(current_app.config["UPLOAD_FOLDER"], attachment.file_path), as_attachment=False)


@bp.route("/finance/accounts-payable/payment-proof/<int:payment_id>")
@login_required
@roles_required("accountant", "admin")
def ap_payment_proof(payment_id: int):
    payment = APPayment.query.get_or_404(payment_id)
    if not payment.proof_attachment_path:
        flash("No payment proof for this record.", "warning")
        return redirect(url_for("finance.accounts_payable"))
    return send_file(os.path.join(current_app.config["UPLOAD_FOLDER"], payment.proof_attachment_path), as_attachment=False)



@bp.route("/finance/income-statement", methods=["GET"])
@login_required
@roles_required("accountant", "admin")
def income_statement():
    today = eat_today()
    period = (request.args.get("period") or "this_month").strip().lower()
    start_date = _parse_date(request.args.get("start_date"))
    end_date = _parse_date(request.args.get("end_date"))

    if period == "this_month":
        start_date = today.replace(day=1)
        end_date = today
    elif period == "last_month":
        first_this_month = today.replace(day=1)
        end_date = first_this_month - timedelta(days=1)
        start_date = end_date.replace(day=1)
    elif period == "this_year":
        start_date = date(today.year, 1, 1)
        end_date = today
    elif period != "custom":
        period = "this_month"
        start_date = today.replace(day=1)
        end_date = today

    if period == "custom":
        if not start_date:
            start_date = today.replace(day=1)
        if not end_date:
            end_date = today

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    start_raw = str(start_date)
    end_raw = str(end_date)

    cash_revenue = _money(db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.payment_date >= start_raw,
        Payment.payment_date <= end_raw,
        Payment.method == "cash",
    ).scalar())

    insurance_revenue = _money(db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.payment_date >= start_raw,
        Payment.payment_date <= end_raw,
        Payment.method == "insurance",
    ).scalar())

    drug_cost = _money(db.session.query(func.coalesce(func.sum(APBillLine.amount), 0)).join(APBill).filter(
        APBill.invoice_date >= start_raw,
        APBill.invoice_date <= end_raw,
        func.lower(APBillLine.expense_category).in_(["drug supplies", "drugs and medical supplies", "drugs"]),
    ).scalar())

    lab_supplies_cost = _money(db.session.query(func.coalesce(func.sum(APBillLine.amount), 0)).join(APBill).filter(
        APBill.invoice_date >= start_raw,
        APBill.invoice_date <= end_raw,
        func.lower(APBillLine.expense_category).in_(["laboratory supplies", "lab supplies"]),
    ).scalar())

    expense_rows = db.session.query(
        ExpenseCategory.name,
        func.coalesce(func.sum(ExpenseEntry.amount), 0),
    ).join(ExpenseEntry, ExpenseEntry.category_id == ExpenseCategory.id).filter(
        ExpenseEntry.expense_date >= start_raw,
        ExpenseEntry.expense_date <= end_raw,
    ).group_by(ExpenseCategory.name).order_by(ExpenseCategory.name.asc()).all()

    operating_expenses = [{"name": name, "amount": _money(total)} for name, total in expense_rows]
    total_operating_expenses = _money(sum((row["amount"] for row in operating_expenses), Decimal("0.00")))

    total_revenue = _money(cash_revenue + insurance_revenue)
    cost_of_service = _money(drug_cost + lab_supplies_cost)
    gross_profit = _money(total_revenue - cost_of_service)
    net_profit = _money(gross_profit - total_operating_expenses)

    gross_profit_margin = Decimal("0.00")
    net_income_margin = Decimal("0.00")
    if total_revenue > 0:
        gross_profit_margin = (gross_profit / total_revenue * Decimal("100")).quantize(Decimal("0.01"))
        net_income_margin = (net_profit / total_revenue * Decimal("100")).quantize(Decimal("0.01"))

    return render_template(
        "finance_income_statement.html",
        period=period,
        start_date=start_date,
        end_date=end_date,
        cash_revenue=cash_revenue,
        insurance_revenue=insurance_revenue,
        total_revenue=total_revenue,
        drug_cost=drug_cost,
        lab_supplies_cost=lab_supplies_cost,
        cost_of_service=cost_of_service,
        gross_profit=gross_profit,
        operating_expenses=operating_expenses,
        total_operating_expenses=total_operating_expenses,
        net_profit=net_profit,
        gross_profit_margin=gross_profit_margin,
        net_income_margin=net_income_margin,
    )

@bp.route("/finance/petty-cash", methods=["GET", "POST"])
@login_required
@roles_required("reception", "nurse", "accountant", "branch_manager", "admin")
def petty_cash_ledger():
    if request.method == "POST":
        if not _can_enter_transactions():
            flash("You do not have permission to enter petty cash transactions.", "danger")
            return redirect(url_for("finance.petty_cash_ledger"))

        txn_date = _parse_date(request.form.get("date"), eat_today())
        if not txn_date or not _ensure_open_period(txn_date):
            return redirect(url_for("finance.petty_cash_ledger"))

        transaction_type = (request.form.get("transaction_type") or "").strip()
        amount = _money(request.form.get("amount") or 0)
        purpose = (request.form.get("purpose") or "").strip()
        category = (request.form.get("expense_category") or "miscellaneous").strip()
        payee = (request.form.get("payee") or "").strip()
        action_mode = (request.form.get("action_mode") or "transaction").strip()
        attachment = request.files.get("attachment")

        if transaction_type not in PETTY_CASH_TXN_TYPES:
            flash("Transaction type must be cash in or cash out.", "warning")
            return redirect(url_for("finance.petty_cash_ledger"))
        if transaction_type == "cash_in" and not _can_record_petty_cash_in():
            flash("Only accountant/admin can record cash in transactions.", "danger")
            return redirect(url_for("finance.petty_cash_ledger"))
        if transaction_type == "cash_out" and not _can_record_petty_cash_out():
            flash("Accountants can only record cash in petty cash transactions.", "danger")
            return redirect(url_for("finance.petty_cash_ledger"))
        if amount <= 0:
            flash("Amount must be greater than zero.", "warning")
            return redirect(url_for("finance.petty_cash_ledger"))
        if transaction_type == "cash_out" and _current_balance() - amount < 0:
            flash("Negative petty cash balances are not allowed.", "danger")
            return redirect(url_for("finance.petty_cash_ledger"))

        try:
            attachment_path = _save_attachment(attachment)
        except ValueError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("finance.petty_cash_ledger"))

        if action_mode in {"top_up", "initial_float", "cash_returned"} and not _can_manage_petty_cash():
            flash("Only admin can set float, top up cash, or record cash returned.", "danger")
            return redirect(url_for("finance.petty_cash_ledger"))

        voucher_number = (request.form.get("voucher_number") or "").strip()
        receipt_number = (request.form.get("receipt_number") or "").strip()

        if action_mode == "initial_float":
            purpose = purpose or "Initial petty cash float"
            category = "miscellaneous"
            voucher_number = ""
            receipt_number = ""
            transaction_type = "cash_in"
        elif action_mode == "top_up":
            purpose = purpose or "Petty cash top-up"
            category = "miscellaneous"
            voucher_number = ""
            receipt_number = ""
            transaction_type = "cash_in"
        elif action_mode == "cash_returned":
            purpose = purpose or "Cash returned to petty cash"
            category = "miscellaneous"
            voucher_number = ""
            receipt_number = ""
            transaction_type = "cash_in"

        txn = PettyCashTransaction(
            date=str(txn_date),
            voucher_number=voucher_number,
            receipt_number=receipt_number,
            transaction_type=transaction_type,
            amount=amount,
            purpose=purpose,
            description=purpose,
            expense_category=category,
            payee=payee,
            entered_by=getattr(current_user, "username", ""),
            approved_by=(request.form.get("approved_by") or "").strip(),
            attachment_path=attachment_path,
            notes=(request.form.get("notes") or "").strip(),
            action_mode=action_mode,
            is_advance=(request.form.get("is_advance") == "on"),
            is_accounted=(request.form.get("is_accounted") == "on"),
            approval_timestamp=eat_now() if (request.form.get("approved_by") or "").strip() else None,
        )
        db.session.add(txn)
        db.session.flush()
        _log_action(
            "create",
            "transaction",
            txn.id,
            {
                "date": txn.date,
                "voucher_number": txn.voucher_number,
                "receipt_number": txn.receipt_number,
                "transaction_type": txn.transaction_type,
                "amount": str(txn.amount),
                "purpose": txn.purpose,
                "expense_category": txn.expense_category,
                "payee": txn.payee,
                "approved_by": txn.approved_by,
                "action_mode": txn.action_mode,
            },
        )
        db.session.commit()
        flash("Petty cash transaction recorded.", "success")
        return redirect(url_for("finance.petty_cash_ledger"))

    filters = _get_filters()
    transactions = _filtered_transactions(filters)
    opening_balance = _opening_balance(filters["start_date"])
    ledger_rows, filtered_balance = _build_ledger_rows(transactions, opening_balance=opening_balance)
    metrics = _summary_metrics(ledger_rows, transactions)
    locks = PettyCashPeriodLock.query.order_by(PettyCashPeriodLock.locked_until.desc()).all()
    audits = PettyCashAuditLog.query.order_by(PettyCashAuditLog.created_at.desc()).limit(15).all()

    export = request.args.get("export")
    if export in {"excel", "csv"}:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "Date", "Voucher Number", "Receipt Number", "Transaction Type", "Amount", "Purpose",
            "Category", "Payee", "Entered By", "Approved By", "Prev Balance", "Amount In", "Amount Out", "New Balance"
        ])
        for row in ledger_rows:
            txn = row["transaction"]
            writer.writerow([
                txn.date, txn.voucher_number, txn.receipt_number, txn.transaction_type, txn.amount, txn.purpose,
                txn.expense_category, txn.payee, txn.entered_by, txn.approved_by,
                row["previous_balance"], row["amount_in"], row["amount_out"], row["new_balance"],
            ])
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=petty_cash_ledger.csv"},
        )
    if export == "pdf":
        html = render_template(
            "finance_petty_cash_pdf.html",
            ledger_rows=ledger_rows,
            filters=filters,
            metrics=metrics,
        )
        try:
            from xhtml2pdf import pisa
        except Exception:
            return Response(html, mimetype="text/html")
        pdf_io = io.BytesIO()
        result = pisa.CreatePDF(src=html, dest=pdf_io, encoding="utf-8")
        if getattr(result, "err", False):
            return Response(html, mimetype="text/html")
        return Response(
            pdf_io.getvalue(),
            mimetype="application/pdf",
            headers={"Content-Disposition": "attachment; filename=petty_cash_ledger.pdf"},
        )

    return render_template(
        "finance_petty_cash.html",
        filters=filters,
        ledger_rows=ledger_rows,
        filtered_balance=filtered_balance,
        opening_balance=opening_balance,
        metrics=metrics,
        categories=PETTY_CASH_CATEGORIES,
        transaction_types=PETTY_CASH_TXN_TYPES,
        can_manage_finance=_can_manage_finance(),
        can_manage_petty_cash=_can_manage_petty_cash(),
        can_record_petty_cash_in=_can_record_petty_cash_in(),
        can_record_petty_cash_out=_can_record_petty_cash_out(),
        locks=locks,
        audits=audits,
        low_balance_threshold=LOW_BALANCE_THRESHOLD,
    )


@bp.route("/finance/petty-cash/reconciliation", methods=["GET", "POST"])
@login_required
@roles_required("accountant", "admin")
def petty_cash_reconciliation():
    if request.method == "POST":
        reconciliation_date = _parse_date(request.form.get("reconciliation_date"), eat_today())
        if not reconciliation_date or not _ensure_open_period(reconciliation_date):
            return redirect(url_for("finance.petty_cash_reconciliation"))

        system_balance = _current_balance()
        physical_cash = _money(request.form.get("physical_cash_counted") or 0)
        accounted_expenses = _money(request.form.get("receipts_accounted_expenses") or 0)
        variance = _money(physical_cash + accounted_expenses - system_balance)
        reconciliation = PettyCashReconciliation(
            reconciliation_date=str(reconciliation_date),
            reconciled_by=(request.form.get("reconciled_by") or getattr(current_user, "username", "")).strip(),
            approved_by=(request.form.get("approved_by") or "").strip(),
            comments=(request.form.get("comments") or "").strip(),
            system_balance=system_balance,
            physical_cash_counted=physical_cash,
            receipts_accounted_expenses=accounted_expenses,
            shortage_overage=variance,
            approval_timestamp=eat_now() if (request.form.get("approved_by") or "").strip() else None,
            reconciliation_timestamp=eat_now(),
        )
        db.session.add(reconciliation)
        db.session.flush()

        unreconciled = PettyCashTransaction.query.filter(PettyCashTransaction.reconciliation_id.is_(None)).all()
        for txn in unreconciled:
            txn.reconciliation_id = reconciliation.id
            txn.reconciled_at = eat_now()

        if request.form.get("lock_period") == "on":
            month_end = date(reconciliation_date.year, reconciliation_date.month, 1)
            if reconciliation_date.month == 12:
                next_month = date(reconciliation_date.year + 1, 1, 1)
            else:
                next_month = date(reconciliation_date.year, reconciliation_date.month + 1, 1)
            month_end = next_month.fromordinal(next_month.toordinal() - 1)
            lock = PettyCashPeriodLock(
                locked_until=month_end,
                locked_by=getattr(current_user, "username", ""),
                notes=f"Locked after reconciliation #{reconciliation.id}",
            )
            db.session.add(lock)

        _log_action(
            "reconcile",
            "reconciliation",
            reconciliation.id,
            {
                "reconciliation_date": reconciliation.reconciliation_date,
                "system_balance": str(system_balance),
                "physical_cash_counted": str(physical_cash),
                "receipts_accounted_expenses": str(accounted_expenses),
                "shortage_overage": str(variance),
                "approved_by": reconciliation.approved_by,
            },
        )
        db.session.commit()
        flash("Petty cash reconciliation saved.", "success")
        return redirect(url_for("finance.petty_cash_reconciliation"))

    reconciliations = PettyCashReconciliation.query.order_by(PettyCashReconciliation.reconciliation_date.desc(), PettyCashReconciliation.id.desc()).all()
    latest = reconciliations[0] if reconciliations else None
    unreconciled = PettyCashTransaction.query.filter(PettyCashTransaction.reconciliation_id.is_(None)).order_by(PettyCashTransaction.date.asc()).all()
    return render_template(
        "finance_reconciliation.html",
        reconciliations=reconciliations,
        latest=latest,
        system_balance=_current_balance(),
        unreconciled=unreconciled,
    )


@bp.route("/finance/petty-cash/attachment/<int:transaction_id>")
@login_required
@roles_required("reception", "nurse", "accountant", "branch_manager", "admin")
def petty_cash_attachment(transaction_id: int):
    txn = PettyCashTransaction.query.get_or_404(transaction_id)
    if not txn.attachment_path:
        flash("No attachment found for this transaction.", "warning")
        return redirect(url_for("finance.petty_cash_ledger"))
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], txn.attachment_path)
    return send_file(full_path, as_attachment=False)


@bp.before_app_request
def ensure_petty_cash_tables():
    app = current_app
    if getattr(app, "_finance_tables_checked", False):
        return
    try:
        inspector = inspect(db.engine)
        existing = set(inspector.get_table_names())
        needed = {
            "petty_cash_transaction",
            "petty_cash_reconciliation",
            "petty_cash_audit_log",
            "petty_cash_period_lock",
            "ap_supplier",
            "ap_bill",
            "ap_bill_line",
            "ap_attachment",
            "ap_payment",
            "ap_recurring_template",
            "ap_audit_log",
        }
        if not needed.issubset(existing):
            db.create_all()
        columns = {c.get("name") for c in inspector.get_columns("petty_cash_transaction")} if "petty_cash_transaction" in existing else set()
        if "reconciliation_id" in columns:
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_petty_cash_transaction_reconciliation_id ON petty_cash_transaction (reconciliation_id)"))
            db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.warning(f"Petty cash table check skipped: {exc}")
    finally:
        app._finance_tables_checked = True  # type: ignore[attr-defined]
