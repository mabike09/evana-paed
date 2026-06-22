# app/forms.py
from datetime import date
from flask_wtf import FlaskForm
from wtforms import StringField, DateField, SelectField, TextAreaField, BooleanField, SubmitField, DecimalField, PasswordField
from wtforms.validators import DataRequired, Length, Optional, Email, NumberRange, EqualTo, Regexp

ROLES = [
    ("admin","Admin"),
    ("pediatrician","Pediatrician"),
    ("doctor","Doctor"),
    ("nurse","Nurse"),
    ("accountant","Accountant"),
    ("claims_manager","Claims Manager"),
    ("claims_officer","Claims Officer"),
    ("branch_manager","Branch Manager"),
    ("reception","Receptionist"),
    ("labtech","Lab Technician"),
]

PAYMENT_METHODS = [
    ("Cash","Cash"), ("Mobile Money","Mobile Money"),
    ("Card","Card"), ("Bank","Bank"), ("Other","Other")
]

INSURANCE_CHOICES = [
    ("Cash", "Cash"),
    ("AAR", "AAR"),
    ("APA", "APA"),
    ("GA", "GA"),
    ("Medicard", "Medicard"),
    ("Prudential", "Prudential"),
    ("Sanlam", "Sanlam"),
    ("ICEA", "ICEA"),
    ("Case", "Case"),
]

class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=150)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=200)])
    submit = SubmitField("Sign In")

class UserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=150)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=150)])
    role = SelectField("Role", choices=ROLES, validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6, max=200)])
    confirm  = PasswordField("Confirm Password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Create User")


class UserEditForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=150)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=150)])
    role = SelectField("Role", choices=ROLES, validators=[DataRequired()])
    is_active = BooleanField("Active")
    password = PasswordField("New Password", validators=[Optional(), Length(min=6, max=200)])
    confirm = PasswordField("Confirm New Password", validators=[EqualTo("password")])
    submit = SubmitField("Save User")




INSURANCE_CHOICES = [
    ("Cash", "Cash"),
    ("AAR", "AAR"),
    ("APA", "APA"),
    ("GA", "GA"),
    ("Medicard", "Medicard"),
    ("Prudential", "Prudential"),
    ("Sanlam", "Sanlam"),
    ("ICEA", "ICEA"),
    ("Case", "Case"),
]

class PatientForm(FlaskForm):
    first_name = StringField("First Name", validators=[DataRequired(), Length(max=80)])
    last_name  = StringField("Last Name",  validators=[DataRequired(), Length(max=80)])
    sex = SelectField("Sex", choices=[("", "Select..."), ("Male", "Male"), ("Female", "Female")])
    date_of_birth = DateField("Date of Birth", format="%Y-%m-%d", validators=[Optional()])
    phone = StringField(
        "Phone",
        validators=[
            DataRequired(),
            Regexp(r"^256\d{9}$", message="Phone number must be in the format 256XXXXXXXXX."),
        ],
    )
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    address = StringField("Address", validators=[Optional(), Length(max=200)])
    next_of_kin = StringField("Next of Kin", validators=[Optional(), Length(max=120)])

    # change from StringField → SelectField
    insurance_provider = SelectField("Payer / Insurer", choices=INSURANCE_CHOICES, validators=[Optional()])

    policy_number = StringField("Policy Number", validators=[Optional(), Length(max=120)])
    allergies = TextAreaField("Allergies", validators=[Optional(), Length(max=5000)])
    medical_history = TextAreaField("Medical History", validators=[Optional(), Length(max=10000)])
    consent = BooleanField("I confirm consent for storing medical data for care.")
    submit = SubmitField("Save Patient")

class VisitForm(FlaskForm):
    visit_date = DateField("Visit Date", format="%Y-%m-%d", default=date.today, validators=[DataRequired()])
    reason = StringField("Reason", validators=[Optional(), Length(max=200)])
    weight_kg = DecimalField("Weight (kg)", places=2, validators=[Optional(), NumberRange(min=0)])
    height_cm = DecimalField("Height (cm)", places=2, validators=[Optional(), NumberRange(min=0)])
    temp_c = DecimalField("Temperature (°C)", places=1, validators=[Optional()])
    pulse_bpm = DecimalField("Pulse (bpm)", places=0, validators=[Optional(), NumberRange(min=0)])
    bp_sys = DecimalField("BP Systolic", places=0, validators=[Optional(), NumberRange(min=0)])
    bp_dia = DecimalField("BP Diastolic", places=0, validators=[Optional(), NumberRange(min=0)])
    spo2 = DecimalField("SpO₂ (%)", places=0, validators=[Optional(), NumberRange(min=0, max=100)])
    notes = TextAreaField("Clinical Notes", validators=[Optional()])
    diagnosis = TextAreaField("Diagnosis", validators=[Optional()])
    procedures = TextAreaField("Procedures (summary)", validators=[Optional()])
    prescriptions = TextAreaField("Prescriptions (summary)", validators=[Optional()])
    amount_billed = DecimalField("Amount Billed", places=2, validators=[Optional(), NumberRange(min=0)])
    submit = SubmitField("Save Visit")

class InvoiceForm(FlaskForm):
    issue_date = DateField("Issue Date", format="%Y-%m-%d", default=date.today, validators=[DataRequired()])
    visit_id = SelectField("Link to Visit (optional)", choices=[], validators=[Optional()])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    amount = DecimalField("Amount", places=2, validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField("Create Invoice")

class PaymentForm(FlaskForm):
    payment_date = DateField("Payment Date", format="%Y-%m-%d", default=date.today, validators=[DataRequired()])
    method = SelectField("Method", choices=PAYMENT_METHODS, default="Cash", validators=[DataRequired()])
    reference = StringField("Reference", validators=[Optional(), Length(max=60)])
    amount = DecimalField("Amount", places=2, validators=[DataRequired(), NumberRange(min=0.01)])
    submit = SubmitField("Add Payment")

class ItemPriceForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=128)])
    sku = StringField("SKU", validators=[Optional(), Length(max=64)])
    unit = StringField("Unit", default="unit", validators=[DataRequired(), Length(max=32)])
    sell_price = DecimalField("Cash Price", places=2, validators=[DataRequired(), NumberRange(min=0)])
    is_drug = BooleanField("Is Drug?")
    submit = SubmitField("Save")

class StockTxnForm(FlaskForm):
    item_id = SelectField("Item", choices=[], coerce=int, validators=[DataRequired()])
    qty = DecimalField("Quantity (positive to add, negative to consume)", places=2, validators=[DataRequired()])
    reason = SelectField("Reason", choices=[
        ("Restock", "Restock"),
        ("Adjustment", "Adjustment"),
        ("Consume-Visit", "Consume (Visit)"),
        ("Consume-Other", "Consume (Other)"),
        ("Return", "Return"),
        ("WriteOff", "Write Off")
    ])
    note = StringField("Note", validators=[Optional(), Length(max=200)])
    submit = SubmitField("Record")


class BulkStockTxnForm(FlaskForm):
    reason = SelectField("Reason", choices=[
        ("Restock", "Restock"),
        ("Adjustment", "Adjustment"),
        ("Consume-Visit", "Consume (Visit)"),
        ("Consume-Other", "Consume (Other)"),
        ("Return", "Return"),
        ("WriteOff", "Write Off")
    ], validators=[DataRequired()])
    note = StringField("Note", validators=[Optional(), Length(max=200)])
    submit = SubmitField("Record Bulk Txn")

class VisitConsumeForm(FlaskForm):
    item_id = SelectField("Item", choices=[], coerce=int, validators=[DataRequired()])
    qty = DecimalField("Quantity to dispense", places=2, validators=[DataRequired(), NumberRange(min=0.01)])
    note = StringField("Note", validators=[Optional(), Length(max=200)])
    add_invoice_line = BooleanField("Add to visit invoice (if any)")
    submit = SubmitField("Dispense")

class InvoiceEditForm(FlaskForm):
    issue_date = DateField("Issue Date", format="%Y-%m-%d", validators=[Optional()])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    amount = DecimalField("Amount", places=2, validators=[Optional(), NumberRange(min=0)])
    payer_type = SelectField("Payer", choices=[("Cash","Cash"),("Insurance","Insurance")])
    submit = SubmitField("Save Changes")
