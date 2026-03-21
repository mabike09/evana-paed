from __future__ import annotations

import csv
import io
import json
import os
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import inspect, text
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import PettyCashAuditLog, PettyCashReconciliation, PettyCashTransaction, PettyCashPeriodLock
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


def _money(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _user_role() -> str:
    return (getattr(current_user, "role", "") or "").strip().lower()


def _can_manage_finance() -> bool:
    return _user_role() in FINANCE_MANAGER_ROLES or _user_role() == "admin"


def _can_enter_transactions() -> bool:
    role = _user_role()
    return role in ENTRY_ROLES or role in FINANCE_MANAGER_ROLES or role == "admin"


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

        if action_mode in {"top_up", "initial_float", "cash_returned"} and not _can_manage_finance():
            flash("Only accountant/admin can set float, top up cash, or record cash returned.", "danger")
            return redirect(url_for("finance.petty_cash_ledger"))

        if action_mode == "initial_float":
            purpose = purpose or "Initial petty cash float"
            category = "miscellaneous"
        elif action_mode == "top_up":
            purpose = purpose or "Petty cash top-up"
            category = "miscellaneous"
            transaction_type = "cash_in"
        elif action_mode == "cash_returned":
            purpose = purpose or "Cash returned to petty cash"
            category = "miscellaneous"
            transaction_type = "cash_in"

        txn = PettyCashTransaction(
            date=str(txn_date),
            voucher_number=(request.form.get("voucher_number") or "").strip(),
            receipt_number=(request.form.get("receipt_number") or "").strip(),
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
    if getattr(app, "_petty_cash_tables_checked", False):
        return
    try:
        inspector = inspect(db.engine)
        existing = set(inspector.get_table_names())
        needed = {"petty_cash_transaction", "petty_cash_reconciliation", "petty_cash_audit_log", "petty_cash_period_lock"}
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
        app._petty_cash_tables_checked = True  # type: ignore[attr-defined]
