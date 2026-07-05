from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import ExpenseCategory, ExpenseEntry, PayrollAuditLog, PayrollComponent, PayrollLine, PayrollPeriod, StaffLoan, StaffMember, User
from ..timezone import eat_now, eat_today

bp = Blueprint("payroll", __name__, url_prefix="/hr")

STAFF_ROLES = ["Nurse", "Receptionist", "Doctor", "Clinical officer", "Accountant", "Claims officer", "Pediatrician", "Admin/Managing Director", "Janitor", "Laboratory technician"]
DEPARTMENTS = ["Clinical", "Reception", "Finance", "Claims", "Administration", "Pharmacy", "Laboratory"]
EMPLOYMENT_TYPES = ["Full-time", "Part-time", "Locum", "Contractor"]
SALARY_TYPES = ["Fixed salary", "Hourly", "Daily"]
CONTRACT_STATUSES = ["Active", "Probation", "Terminated", "Resigned"]
PERIOD_STATUSES = ["Draft", "Under review", "Approved by admin", "Paid", "Locked"]


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _audit(period, action, summary):
    db.session.add(PayrollAuditLog(period_id=getattr(period, "id", None), action=action, actor_username=current_user.username, actor_role=current_user.role, change_summary=summary))


def _ensure_payroll_tables():
    try:
        insp = inspect(db.engine)
        needed = {"staff_member", "payroll_period", "payroll_component", "staff_loan", "payroll_line", "payroll_audit_log"}
        existing = set(insp.get_table_names())
        if not needed.issubset(existing):
            db.create_all()
        elif "staff_member" in existing:
            columns = {c.get("name") for c in insp.get_columns("staff_member")}
            if "first_name" not in columns:
                db.session.execute(text("ALTER TABLE staff_member ADD COLUMN first_name VARCHAR(80) NOT NULL DEFAULT ''"))
            if "last_name" not in columns:
                db.session.execute(text("ALTER TABLE staff_member ADD COLUMN last_name VARCHAR(80) NOT NULL DEFAULT ''"))
            if "user_id" not in columns:
                db.session.execute(text("ALTER TABLE staff_member ADD COLUMN user_id INTEGER"))
            db.session.commit()
    except Exception:
        db.session.rollback()


@bp.before_request
@login_required
def _guard():
    _ensure_payroll_tables()
    if (current_user.role or "").lower() not in {"admin", "accountant"}:
        from flask import abort
        abort(403)


@bp.route("/payroll")
def dashboard():
    period = PayrollPeriod.query.order_by(PayrollPeriod.period_month.desc()).first()
    lines = period.lines if period else []
    gross = sum(_money(l.gross_pay) for l in lines)
    net = sum(_money(l.net_pay) for l in lines)
    revenue = _money(period.revenue) if period else Decimal("0.00")
    pct = (gross / revenue * 100).quantize(Decimal("0.01")) if revenue else Decimal("0.00")
    dept_rows = db.session.query(StaffMember.department, func.sum(PayrollLine.net_pay)).join(PayrollLine, StaffMember.id == PayrollLine.staff_id).group_by(StaffMember.department).all()
    periods = PayrollPeriod.query.order_by(PayrollPeriod.period_month.desc()).limit(12).all()
    return render_template("payroll_dashboard.html", period=period, gross=gross, net=net, pct=pct, dept_rows=dept_rows, periods=periods)


@bp.route("/staff", methods=["GET", "POST"])
def staff_list():
    if request.method == "POST":
        if current_user.role != "admin":
            flash("Only admin can add staff.", "warning")
            return redirect(url_for("payroll.staff_list"))
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        if not first_name or not last_name:
            flash("Employee first and last name are required.", "warning")
            return redirect(url_for("payroll.staff_list"))
        linked_user_id = request.form.get("user_id") or None
        create_user = request.form.get("create_user") == "1"
        new_user = None
        if create_user:
            username = (request.form.get("new_username") or "").strip()
            email = (request.form.get("new_email") or "").strip()
            password = request.form.get("new_password") or ""
            if not username or not email or len(password) < 6:
                flash("Username, email, and a password of at least 6 characters are required to create a user.", "warning")
                return redirect(url_for("payroll.staff_list"))
            if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first():
                flash("The user account username or email already exists.", "danger")
                return redirect(url_for("payroll.staff_list"))
            new_user = User(username=username, email=email, role=request.form.get("new_user_role") or "reception")
            new_user.set_password(password)
            db.session.add(new_user); db.session.flush()
            linked_user_id = new_user.id
        staff = StaffMember(
            first_name=first_name,
            last_name=last_name,
            user_id=int(linked_user_id) if linked_user_id else None,
            role=request.form.get("role_other") or request.form.get("role"),
            employment_type=request.form.get("employment_type"),
            salary_type=request.form.get("salary_type"),
            basic_salary=_money(request.form.get("basic_salary")),
            bank_details=request.form.get("bank_details"),
            mobile_money_details=request.form.get("mobile_money_details"),
            nssf_number=request.form.get("nssf_number"),
            tin=request.form.get("tin"),
            start_date=request.form.get("start_date") or str(eat_today()),
            contract_status=request.form.get("contract_status"),
            department=request.form.get("department"),
        )
        db.session.add(staff); db.session.flush()
        staff.staff_id = f"EVP-{staff.id:04d}"
        try:
            db.session.commit(); flash("Staff member added." + (" User account created and linked." if new_user else ""), "success")
        except IntegrityError:
            db.session.rollback(); flash("Could not save staff because the selected user is already linked or another constraint failed.", "danger")
        return redirect(url_for("payroll.staff_list"))
    staff = StaffMember.query.order_by(StaffMember.first_name.asc(), StaffMember.last_name.asc()).all()
    linked_ids = [row[0] for row in db.session.query(StaffMember.user_id).filter(StaffMember.user_id.isnot(None)).all()]
    users_query = User.query.order_by(User.username.asc())
    if linked_ids:
        users_query = users_query.filter(~User.id.in_(linked_ids))
    users = users_query.all()
    return render_template("payroll_staff.html", staff=staff, users=users, roles=STAFF_ROLES, departments=DEPARTMENTS, employment_types=EMPLOYMENT_TYPES, salary_types=SALARY_TYPES, statuses=CONTRACT_STATUSES)


@bp.route("/staff/<int:staff_id>", methods=["GET", "POST"])
def staff_detail(staff_id):
    staff = StaffMember.query.get_or_404(staff_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "component":
            db.session.add(PayrollComponent(staff_id=staff.id, name=request.form.get("name"), component_type=request.form.get("component_type"), amount=_money(request.form.get("amount"))))
            flash("Salary component added.", "success")
        elif action == "loan":
            amount = _money(request.form.get("principal_amount"))
            db.session.add(StaffLoan(staff_id=staff.id, loan_type=request.form.get("loan_type"), principal_amount=amount, monthly_deduction=_money(request.form.get("monthly_deduction")), outstanding_balance=amount, approved_by=request.form.get("approved_by") or current_user.username, approval_date=request.form.get("approval_date") or str(eat_today()), notes=request.form.get("notes")))
            flash("Advance/loan recorded.", "success")
        db.session.commit(); return redirect(url_for("payroll.staff_detail", staff_id=staff.id))
    return render_template("payroll_staff_detail.html", staff=staff)


@bp.route("/periods", methods=["GET", "POST"])
def periods():
    if request.method == "POST":
        month = request.form.get("period_month")
        period = PayrollPeriod(name=request.form.get("name") or f"{month} payroll", period_month=month, revenue=_money(request.form.get("revenue")), created_by=current_user.username)
        db.session.add(period); db.session.flush(); _audit(period, "create", "Payroll period created")
        db.session.commit(); flash("Payroll period created.", "success")
        return redirect(url_for("payroll.period_detail", period_id=period.id))
    return render_template("payroll_periods.html", periods=PayrollPeriod.query.order_by(PayrollPeriod.period_month.desc()).all())


def _calculate_line(period, staff, form):
    basic = _money(staff.basic_salary)
    if staff.salary_type == "Hourly": basic = _money(form.get("attendance_hours")) * basic
    if staff.salary_type == "Daily": basic = _money(form.get("attendance_days")) * basic
    comps = [c for c in staff.components if c.is_active]
    allowances = sum(_money(c.amount) for c in comps if c.component_type == "earning" and c.name not in {"Overtime pay", "Locum pay"})
    deductions = sum(_money(c.amount) for c in comps if c.component_type == "deduction") + _money(form.get("deductions"))
    overtime = _money(form.get("overtime_pay")); locum = _money(form.get("locum_pay"))
    loan_deductions = sum(min(_money(l.monthly_deduction), _money(l.outstanding_balance)) for l in staff.loans if l.is_active and _money(l.outstanding_balance) > 0)
    nssf = _money(form.get("nssf")); paye = _money(form.get("paye"))
    gross = basic + allowances + overtime + locum
    net = gross - deductions - loan_deductions - nssf - paye
    return PayrollLine(period_id=period.id, staff_id=staff.id, attendance_days=_money(form.get("attendance_days")), attendance_hours=_money(form.get("attendance_hours")), basic_pay=basic, allowances=allowances, overtime_pay=overtime, locum_pay=locum, deductions=deductions, loan_deductions=loan_deductions, nssf=nssf, paye=paye, gross_pay=gross, net_pay=net)


@bp.route("/periods/<int:period_id>", methods=["GET", "POST"])
def period_detail(period_id):
    period = PayrollPeriod.query.get_or_404(period_id)
    if request.method == "POST":
        action = request.form.get("action")
        if period.status == "Locked" and current_user.role != "admin":
            flash("Locked payroll changes require admin authorization.", "warning"); return redirect(url_for("payroll.period_detail", period_id=period.id))
        if action == "calculate":
            period.lines.clear(); db.session.flush()
            for staff in StaffMember.query.filter(StaffMember.contract_status.in_(["Active", "Probation"])).all():
                db.session.add(_calculate_line(period, staff, request.form))
            _audit(period, "calculate", "Calculated payroll from active staff profiles and attendance inputs")
        elif action == "status":
            new_status = request.form.get("status")
            if new_status in ["Approved by admin", "Locked"] and current_user.role != "admin":
                flash("Only admin can approve or lock payroll.", "warning"); return redirect(url_for("payroll.period_detail", period_id=period.id))
            period.status = new_status
            if new_status == "Approved by admin": period.approved_by = current_user.username; period.approved_at = eat_now()
            if new_status == "Paid": period.paid_by = current_user.username; period.paid_at = eat_now(); _post_payroll_expense(period)
            if new_status == "Locked": period.locked_by = current_user.username; period.locked_at = eat_now()
            _audit(period, "status", f"Status changed to {new_status}")
        db.session.commit(); flash("Payroll updated.", "success")
        return redirect(url_for("payroll.period_detail", period_id=period.id))
    return render_template("payroll_period_detail.html", period=period, statuses=PERIOD_STATUSES, total_gross=sum(_money(l.gross_pay) for l in period.lines), total_net=sum(_money(l.net_pay) for l in period.lines))


def _post_payroll_expense(period):
    category = ExpenseCategory.query.filter(func.lower(ExpenseCategory.name) == "salary expense").first()
    if not category:
        category = ExpenseCategory(name="Salary expense", created_by="payroll"); db.session.add(category); db.session.flush()
    amount = sum(_money(l.gross_pay) for l in period.lines)
    exists = ExpenseEntry.query.filter_by(reference=f"PAYROLL-{period.id}").first()
    if not exists and amount:
        db.session.add(ExpenseEntry(expense_date=str(eat_today()), category_id=category.id, description=f"Salaries and overtime for {period.name}", vendor_payee="Staff payroll", reference=f"PAYROLL-{period.id}", amount=amount, entered_by=current_user.username))
