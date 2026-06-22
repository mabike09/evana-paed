# app/utils.py
from datetime import datetime, timedelta, date
from decimal import Decimal
from flask import current_app
from sqlalchemy import func
from .extensions import db
from .models import Patient, Invoice, Payment
from .timezone import eat_now, eat_today

def within_24h(dt):
    return bool(dt) and (eat_now() - dt) <= timedelta(hours=24)

def _yy(d: date) -> str: return d.strftime("%y")
def _yymm(d: date) -> str: return d.strftime("%y%m")

def generate_patient_code(reg_date: date | None = None) -> str:
    d = reg_date or eat_today()
    like = f"BC-{_yy(d)}%"
    seq = (db.session.query(func.count(Patient.id))
           .filter(Patient.patient_code.like(like)).scalar() or 0) + 1
    return f"BC-{_yy(d)}{seq:03d}"

def generate_invoice_number(issue_date: date | None = None) -> str:
    """INV-YYMM-#### (no branch)."""
    d = issue_date or eat_today()
    yymm = _yymm(d)
    like = f"INV-{yymm}-%"
    seq = (db.session.query(func.count(Invoice.id))
           .filter(Invoice.number.like(like)).scalar() or 0) + 1
    return f"INV-{yymm}-{seq:04d}"

def generate_receipt_number(pay_date: date | None = None) -> str:
    """RC-YYMM-#### (no branch)."""
    d = pay_date or eat_today()
    yymm = _yymm(d)
    like = f"RC-{yymm}-%"
    seq = (db.session.query(func.count(Payment.id))
           .filter(Payment.receipt_no.like(like)).scalar() or 0) + 1
    return f"RC-{yymm}-{seq:04d}"
    
def invoice_editable_now(inv) -> bool:
    try:
        status = (getattr(inv, "status", None) or "").lower()
        if status in {"paid", "void", "cancelled", "canceled"}:
            return False
        dt = getattr(inv, "created_at", None) or getattr(inv, "issued_at", None) or getattr(inv, "date", None)
        if not dt: return False
        from datetime import datetime as _dt
        if not isinstance(dt, _dt):
            from dateutil import parser
            parsed = parser.parse(str(dt))
            dt = parsed if isinstance(parsed, _dt) else _dt(parsed.year, parsed.month, parsed.day)
        window = timedelta(hours=current_app.config.get("INVOICE_EDIT_WINDOW_HOURS", 24))
        return (eat_now() - dt) <= window
    except Exception:
        return False

# -------------------------------
# Role-aware landing endpoint
# -------------------------------
# Map role -> endpoint string
_ROLE_LANDING_MAP = {
    # Lab users go straight to Lab Queue
    "lab": "lab.lab_queue",
    "labtech": "lab.lab_queue",
    "laboratory": "lab.lab_queue",

    # Doctors & pediatricians to Doctors Queue
    "doctor": "patients.doctors_queue",
    "pediatrician": "patients.doctors_queue",
    "accountant": "finance.petty_cash_ledger",
    "branch_manager": "finance.petty_cash_ledger",

    # Claims team members go straight to the claims dashboard
    "claims_manager": "claims.dashboard",
    "claims manager": "claims.dashboard",
    "claims_officer": "claims.dashboard",
    "claims officer": "claims.dashboard",
}

_DEFAULT_LANDING = "patients.patients_list"


def landing_endpoint_for(user) -> str | None:
    """
    Return the landing endpoint (string) based on the user's role(s).
    Supports:
      - user.role as a single string
      - user.roles as a list of strings or role objects with .name
    """
    if not user:
        return None

    # Single role attribute (most common in this codebase)
    role = getattr(user, "role", None)
    if isinstance(role, str):
        role_lc = role.strip().lower()
        if role_lc in _ROLE_LANDING_MAP:
            return _ROLE_LANDING_MAP[role_lc]

    # Multiple roles (optional)
    roles = getattr(user, "roles", None)
    if roles:
        for r in roles:
            name = getattr(r, "name", r)
            if isinstance(name, str) and name.strip().lower() in _ROLE_LANDING_MAP:
                return _ROLE_LANDING_MAP[name.strip().lower()]

    # No match -> default
    return _DEFAULT_LANDING


def has_endpoint(endpoint: str) -> bool:
    try:
        return endpoint in current_app.view_functions
    except Exception:
        return False
