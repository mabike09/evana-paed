# app/models.py
from datetime import datetime
from decimal import Decimal
from enum import Enum
from sqlalchemy import Enum as SqlEnum, event
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from .extensions import db
from .timezone import eat_now, eat_today

# ---------------------------
# Patients & Visits
# ---------------------------
class Patient(db.Model):
    __tablename__ = "patient"

    id = db.Column(db.Integer, primary_key=True)
    patient_code = db.Column(db.String(20), unique=True, index=True)  # BD-YY###
    first_name = db.Column(db.String(80), nullable=False)
    last_name  = db.Column(db.String(80), nullable=False)
    sex = db.Column(db.String(10))
    date_of_birth = db.Column(db.String(10))
    phone = db.Column(db.String(30), nullable=False)
    email = db.Column(db.String(120))
    address = db.Column(db.String(200))
    next_of_kin = db.Column(db.String(120))
    insurance_provider = db.Column(db.String(120))
    policy_number = db.Column(db.String(120))
    allergies = db.Column(db.Text)
    medical_history = db.Column(db.Text)
    consent = db.Column(db.Boolean, default=False)

    visits   = db.relationship("Visit", backref="patient", lazy=True, cascade="all, delete-orphan")
    invoices = db.relationship("Invoice", backref="patient", lazy=True, cascade="all, delete-orphan")

    @property
    def billed_total(self):
        return sum((inv.amount or 0) for inv in self.invoices)

    @property
    def paid_total(self):
        return sum((p.amount or 0) for inv in self.invoices for p in inv.payments)

    @property
    def balance(self):
        return (self.billed_total or 0) - (self.paid_total or 0)


class Visit(db.Model):
    __tablename__ = "visit"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    visit_date = db.Column(db.String(10), nullable=False)     # "YYYY-MM-DD"
    reason = db.Column(db.String(200))

    # Workflow / routing
    status = db.Column(db.String(10), nullable=False, default="Open")  # Open / Closed
    current_station = db.Column(db.String(20), nullable=False, default="TRIAGE")  # TRIAGE/DOCTOR/LAB/PHARMACY/BILLING/CLOSED
    closed_at = db.Column(db.DateTime)

    # Clinician notes
    notes = db.Column(db.Text)
    diagnosis = db.Column(db.Text)

    # Summaries for history (auto-filled from invoice lines)
    procedures = db.Column(db.Text)
    prescriptions = db.Column(db.Text)

    # TRIAGE / VITALS
    weight_kg = db.Column(db.Numeric(6, 2))
    height_cm = db.Column(db.Numeric(6, 2))
    temp_c = db.Column(db.Numeric(4, 1))
    pulse_bpm = db.Column(db.Integer)
    bp_sys = db.Column(db.Integer)
    bp_dia = db.Column(db.Integer)
    spo2 = db.Column(db.Integer)

    amount_billed = db.Column(db.Numeric(12, 2), default=0)
    amount_paid   = db.Column(db.Numeric(12, 2), default=0)

    files = db.relationship("FileAsset", backref="visit_ref", lazy=True)
    invoice = db.relationship("Invoice", backref="visit", uselist=False)

    created_at = db.Column(db.DateTime, default=eat_now, nullable=False, index=True)

    @property
    def turnaround_minutes(self):
        end = self.closed_at or eat_now()
        start = self.created_at or end
        return max(0, int((end - start).total_seconds() // 60))


# ---------------------------
# Billing: Invoice, Payment
# ---------------------------
class Invoice(db.Model):
    __tablename__ = "invoice"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, index=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    visit_id = db.Column(db.Integer, db.ForeignKey("visit.id"), nullable=True)

    issue_date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    description = db.Column(db.String(255))
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    payer_type = db.Column(
        SqlEnum("Cash", "Insurance", name="payer_type_enum", native_enum=False),
        nullable=False,
        default="Cash"
    )

    lines = db.relationship("InvoiceLine", backref="invoice", lazy=True, cascade="all, delete-orphan")
    payments = db.relationship("Payment", backref="invoice", lazy=True, cascade="all, delete-orphan")

    created_at = db.Column(db.DateTime, default=eat_now, nullable=False, index=True)

    @property
    def paid_total(self):
        return sum((p.amount or 0) for p in self.payments)

    @property
    def balance(self):
        return (self.amount or 0) - self.paid_total

    @property
    def age_days(self):
        try:
            return (eat_today() - datetime.strptime(self.issue_date, "%Y-%m-%d").date()).days
        except Exception:
            return 0


class Payment(db.Model):
    __tablename__ = "payment"

    id = db.Column(db.Integer, primary_key=True)
    receipt_no = db.Column(db.String(32), unique=True, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    payment_date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    method = db.Column(db.String(30))  # Cash, Mobile Money, Card, Bank, Other
    reference = db.Column(db.String(60))
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)


# ---------------------------
# Auth: User
# ---------------------------
class User(db.Model, UserMixin):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(30), nullable=False)  # admin, pediatrician, doctor, nurse, reception, labtech

    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


# ---------------------------
# Billing master data
# ---------------------------
class Insurer(db.Model):
    __tablename__ = "insurer"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True)


class Procedure(db.Model):
    __tablename__ = "procedure"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    default_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    category = db.Column(
        SqlEnum("consult", "lab", "procedure", name="proc_cat_enum", native_enum=False),
        nullable=False,
        default="procedure"
    )

    prices = db.relationship("ProcedurePrice", backref="procedure", lazy="select", cascade="all, delete-orphan")


class ProcedurePrice(db.Model):
    __tablename__ = "procedure_price"

    id = db.Column(db.Integer, primary_key=True)
    procedure_id = db.Column(db.Integer, db.ForeignKey("procedure.id"), nullable=False)
    insurer_id = db.Column(db.Integer, db.ForeignKey("insurer.id"), nullable=False)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    __table_args__ = (db.UniqueConstraint("procedure_id", "insurer_id", name="uq_proc_price_per_insurer"),)


# ---------------------------
# Inventory
# ---------------------------
class Item(db.Model):
    __tablename__ = "item"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    sku = db.Column(db.String(64), unique=True)
    unit = db.Column(db.String(32), nullable=False, default="unit")
    min_level = db.Column(db.Integer, nullable=False, default=0)
    current_qty = db.Column(db.Integer, nullable=False, default=0)
    is_drug = db.Column(db.Boolean, nullable=False, default=False)
    sell_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    buying_price = db.Column(db.Numeric(12, 2))

    txns = db.relationship("ItemTxn", backref="item", lazy=True, cascade="all, delete-orphan")


class ItemPrice(db.Model):
    __tablename__ = "item_price"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    insurer_id = db.Column(db.Integer, db.ForeignKey("insurer.id"), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    __table_args__ = (db.UniqueConstraint("item_id", "insurer_id", name="uq_item_price_per_insurer"),)


@event.listens_for(Item, "before_insert")
def _item_qty_before_insert(mapper, connection, target):
    try:
        target.current_qty = int(target.current_qty or 0)
    except Exception:
        target.current_qty = 0


@event.listens_for(Item, "before_update")
def _item_qty_before_update(mapper, connection, target):
    try:
        target.current_qty = int(target.current_qty or 0)
    except Exception:
        target.current_qty = 0


class ItemTxn(db.Model):
    __tablename__ = "item_txn"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    when = db.Column(db.DateTime, nullable=False, default=eat_now, index=True)
    qty_change = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(30), nullable=False)
    visit_id = db.Column(db.Integer, db.ForeignKey("visit.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    note = db.Column(db.String(255))


class InvoiceLine(db.Model):
    __tablename__ = "invoice_line"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)

    kind = db.Column(
        SqlEnum("procedure", "drug", "other", name="line_kind_enum", native_enum=False),
        nullable=False
    )
    procedure_id = db.Column(db.Integer, db.ForeignKey("procedure.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"))

    description = db.Column(db.String(255), nullable=False)
    qty = db.Column(db.Numeric(12, 2), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    insurer_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    patient_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)


class DispenseTxn(db.Model):
    __tablename__ = "dispense_txn"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False, index=True)
    visit_id = db.Column(db.Integer, db.ForeignKey("visit.id"))
    invoice_line_id = db.Column(db.Integer, db.ForeignKey("invoice_line.id"))

    when = db.Column(db.DateTime, nullable=False, default=eat_now, index=True)
    qty = db.Column(db.Numeric(12, 2), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)


# ---------------------------
# Queues
# ---------------------------
class ClinicianQueue(db.Model):
    __tablename__ = "clinician_queue"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    status = db.Column(db.String(20), default="Waiting")
    queued_at = db.Column(db.DateTime, default=eat_now)
    seen_at = db.Column(db.DateTime, nullable=True)
    clinician_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    patient = db.relationship("Patient")


class BillingQueue(db.Model):
    """
    Generic workflow queue:
      kind: TRIAGE / DOCTOR / LAB / PHARMACY / BILLING
      status: Open / Closed
    """
    __tablename__ = "billing_queue"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False, index=True)
    visit_id = db.Column(db.Integer, db.ForeignKey("visit.id"))
    status = db.Column(db.String(20), nullable=False, default="Open")
    added_at = db.Column(db.DateTime, nullable=False, default=eat_now, index=True)
    closed_at = db.Column(db.DateTime, nullable=True, index=True)
    added_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    kind = db.Column(db.String(20), nullable=True, index=True)
    description = db.Column(db.String(255), nullable=True)

    patient = db.relationship("Patient")


# ---------------------------
# Lab
# ---------------------------
class LabOrder(db.Model):
    __tablename__ = "lab_order"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False, index=True)
    visit_id = db.Column(db.Integer, db.ForeignKey("visit.id"), nullable=True, index=True)
    status = db.Column(
        SqlEnum("PendingPayment", "Pending", "Completed", name="lab_status_enum", native_enum=False),
        default="PendingPayment"
    )
    created_at = db.Column(db.DateTime, default=eat_now, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    lines = db.relationship("LabOrderLine", backref="order", lazy=True, cascade="all, delete-orphan")


class LabOrderLine(db.Model):
    __tablename__ = "lab_order_line"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("lab_order.id"), nullable=False, index=True)
    procedure_id = db.Column(db.Integer, db.ForeignKey("procedure.id"), nullable=True)
    test_name = db.Column(db.String(200))

    result_value = db.Column(db.String(200))
    result_text = db.Column(db.Text)
    status = db.Column(
        SqlEnum("Ordered", "Done", name="lab_line_status_enum", native_enum=False),
        default="Ordered"
    )
    result_at = db.Column(db.DateTime)
    performed_by = db.Column(db.Integer, db.ForeignKey("user.id"))


# ---------------------------
# Files & SMS
# ---------------------------
class FileAsset(db.Model):
    __tablename__ = "file_asset"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    visit_id = db.Column(db.Integer, db.ForeignKey("visit.id"), nullable=True)
    kind = db.Column(db.String(30))
    filename = db.Column(db.String(255))
    mime = db.Column(db.String(100))
    size = db.Column(db.Integer)
    stored_path = db.Column(db.String(500))
    uploaded_at = db.Column(db.DateTime, default=eat_now)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"))


class SmsLog(db.Model):
    __tablename__ = "sms_log"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), index=True)
    phone = db.Column(db.String(30), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    provider_response_id = db.Column(db.String(64))
    status = db.Column(db.String(4))
    remarks = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=eat_now, index=True)


# ---------------------------
# Finance: Petty Cash
# ---------------------------
class PettyCashReconciliation(db.Model):
    __tablename__ = "petty_cash_reconciliation"

    id = db.Column(db.Integer, primary_key=True)
    reconciliation_date = db.Column(db.String(10), nullable=False, index=True)
    reconciled_by = db.Column(db.String(150), nullable=False)
    approved_by = db.Column(db.String(150))
    comments = db.Column(db.Text)
    system_balance = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    physical_cash_counted = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    receipts_accounted_expenses = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    shortage_overage = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    approval_timestamp = db.Column(db.DateTime)
    reconciliation_timestamp = db.Column(db.DateTime, default=eat_now, nullable=False)


class PettyCashTransaction(db.Model):
    __tablename__ = "petty_cash_transaction"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False, index=True)
    voucher_number = db.Column(db.String(64), index=True)
    receipt_number = db.Column(db.String(64), index=True)
    transaction_type = db.Column(db.String(20), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    purpose = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255))
    expense_category = db.Column(db.String(80), index=True)
    payee = db.Column(db.String(150), index=True)
    entered_by = db.Column(db.String(150), nullable=False, index=True)
    approved_by = db.Column(db.String(150), index=True)
    attachment_path = db.Column(db.String(255))
    notes = db.Column(db.Text)
    action_mode = db.Column(db.String(40), nullable=False, default="transaction")
    is_advance = db.Column(db.Boolean, nullable=False, default=False)
    is_accounted = db.Column(db.Boolean, nullable=False, default=False)
    approval_timestamp = db.Column(db.DateTime)
    reconciled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=eat_now, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=eat_now, onupdate=eat_now, nullable=False)
    reconciliation_id = db.Column(db.Integer, db.ForeignKey("petty_cash_reconciliation.id"), index=True)

    reconciliation = db.relationship("PettyCashReconciliation", backref=db.backref("transactions", lazy=True))


class PettyCashAuditLog(db.Model):
    __tablename__ = "petty_cash_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(30), nullable=False, index=True)
    record_type = db.Column(db.String(30), nullable=False, index=True)
    record_id = db.Column(db.Integer, index=True)
    actor_username = db.Column(db.String(150), nullable=False)
    actor_role = db.Column(db.String(30))
    changes_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=eat_now, nullable=False, index=True)


class PettyCashPeriodLock(db.Model):
    __tablename__ = "petty_cash_period_lock"

    id = db.Column(db.Integer, primary_key=True)
    locked_until = db.Column(db.Date, nullable=False, index=True)
    locked_by = db.Column(db.String(150), nullable=False)
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=eat_now, nullable=False)


# ===========================
# NEW: Price Books
# ===========================
class ItemTypeEnum(str, Enum):
    DRUG = "drug"
    LAB = "lab"
    PROCEDURE = "procedure"
    UNKNOWN = "unknown"


class Payer(db.Model):
    __tablename__ = "payer"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    payer_type = db.Column(db.String(20), nullable=False, default="cash")


class PriceBook(db.Model):
    __tablename__ = "price_book"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    payer_id = db.Column(db.Integer, db.ForeignKey("payer.id"), nullable=False)
    effective_date = db.Column(db.Date, nullable=False)
    currency = db.Column(db.String(8), default="UGX")
    created_at = db.Column(db.DateTime, default=eat_now)

    payer = db.relationship("Payer")
    items = db.relationship("PriceItem", backref="pricebook", lazy=True, cascade="all, delete-orphan")


class PriceItem(db.Model):
    __tablename__ = "price_item"

    id = db.Column(db.Integer, primary_key=True)
    pricebook_id = db.Column(db.Integer, db.ForeignKey("price_book.id"), nullable=False, index=True)
    item_type = db.Column(
        SqlEnum("drug", "lab", "procedure", "unknown", name="price_item_type_enum", native_enum=False),
        nullable=False,
        default="unknown"
    )
    item_code = db.Column(db.String(64))
    item_name = db.Column(db.String(255), nullable=False)
    unit = db.Column(db.String(64))
    category = db.Column(db.String(128))
    sell_price = db.Column(db.Numeric(12, 2), nullable=False)
    buy_price = db.Column(db.Numeric(12, 2))
