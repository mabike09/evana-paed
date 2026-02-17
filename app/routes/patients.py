# app/routes/patients.py
from decimal import Decimal
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from flask_login import login_required, current_user
from sqlalchemy import or_, cast
from sqlalchemy.types import String

from ..extensions import db
from ..permissions import roles_required
from ..forms import PatientForm, VisitForm, InvoiceForm, PaymentForm
from ..models import Patient, Visit, Invoice, Payment, BillingQueue, FileAsset, Insurer
from ..utils import generate_patient_code, generate_invoice_number, generate_receipt_number

# --- Model aliases used by patient_chart() ---
# IMPORTANT: these must exist at module scope, otherwise you'll get NameError at runtime.
try:
    from .. import models as _models
except Exception:
    _models = None

def _pick_model(*names):
    """Return the first model that exists in app.models, else None."""
    if not _models:
        return None
    for n in names:
        m = getattr(_models, n, None)
        if m is not None:
            return m
    return None

# Catalog / billing related models (use whatever exists in your models.py)
_DrugModel      = _pick_model("Item", "Drug", "Medication")
_ProcModel      = _pick_model("Procedure", "Service", "Treatment", "ProcedureItem")
_PriceItemModel = _pick_model("PriceItem")
_PriceBookModel = _pick_model("PriceBook")

# --- Lab models (must exist even if not installed in this edition) ---
_LabOrderModel  = _pick_model("LabOrder", "LabRequest", "LabOrderHeader")
_LabLineModel   = _pick_model("LabOrderLine", "LabLine", "LabRequestLine", "LabOrderItem")
_LabResultModel = _pick_model("LabResult", "Result", "LabTestResult")


# --- Model aliases (must exist at module scope) ---
# These prevent NameError in patient_chart() and visit_send_to_lab()

try:
    from ..models import Procedure as _ProcModel
except Exception:
    _ProcModel = None

# Some routes reference Procedure directly (not _ProcModel)
Procedure = _ProcModel


bp = Blueprint("patients", __name__)

# --- Model aliases used by patient_chart() ---
try:
    from ..models import Item as _DrugModel
except Exception:
    _DrugModel = None

try:
    from ..models import PriceItem as _PriceItemModel
except Exception:
    _PriceItemModel = None

try:
    from ..models import PriceBook as _PriceBookModel
except Exception:
    _PriceBookModel = None

try:
    from ..models import Payer as _PayerModel
except Exception:
    _PayerModel = None


# ---------------------------
# API: lookup / autocomplete
# ---------------------------
from sqlalchemy import func

# Ensure required models exist in this module scope
try:
    from ..models import Item, PriceBook, PriceItem, Payer
except Exception:
    Item = PriceBook = PriceItem = Payer = None


@bp.get("/api/lookup/drugs")
def api_lookup_drugs():
    # Used by patient_chart "Drug Prescription" autocomplete.
    # Returns JSON: [{id, name, price, qty}]
    if not current_user.is_authenticated:
        return jsonify([])

    if PriceItem is None or PriceBook is None or Payer is None or Item is None:
        current_app.logger.warning("Lookup models not available (Item/PriceItem/PriceBook/Payer).")
        return jsonify([])

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    patient_id = request.args.get("patient_id", type=int)
    patient = Patient.query.get(patient_id) if patient_id else None
    book = _resolve_price_book_for_patient(patient) if patient else None
    cash_pb = _cash_pricebook_id()
    book_id = book.id if book else cash_pb

    def _fallback_inventory():
        rows = (
            Item.query
            .filter(Item.name.ilike(f"%{q}%"))
            .order_by(Item.name.asc())
            .limit(20)
            .all()
        )
        # Prefer items marked as drugs; but if your items are not flagged yet,
        # we still return matches so autocomplete works.
        out = []
        for r in rows:
            if getattr(r, "is_drug", False) is False:
                # keep non-drug matches but push them down by name ordering already
                pass
            out.append({
                "id": r.id,
                "name": r.name,
                "price": float(getattr(r, "sell_price", 0) or 0),
                "qty": int(getattr(r, "current_qty", 0) or 0),
            })
        return jsonify(out)

    # Prefer matched price book entries for drugs, BUT return Inventory Item.id
    if book_id:
        try:
            rows = (
                PriceItem.query
                .filter(PriceItem.pricebook_id == book_id)
                # be tolerant: some DBs store 'Drug', 'DRUG', etc.
                .filter(func.lower(PriceItem.item_type) == "drug")
                .filter(PriceItem.item_name.ilike(f"%{q}%"))
                .order_by(PriceItem.item_name.asc())
                .limit(20)
                .all()
            )
        except Exception:
            rows = []

        # If the price book exists but has no drug rows, fall back to inventory.
        if not rows:
            return _fallback_inventory()

        out = []
        for r in rows:
            name = r.item_name
            price = float(getattr(r, "sell_price", 0) or 0)

            # map pricebook row -> inventory Item (so prescriptions link to stock)
            item = None
            try:
                if getattr(r, "item_code", None):
                    item = Item.query.filter(Item.sku == r.item_code).first()
                if not item:
                    item = Item.query.filter(Item.name.ilike(name)).first()
            except Exception:
                item = None

            out.append({
                "id": item.id if item else "",
                "name": name,
                "price": price,
                "qty": int(getattr(item, "current_qty", 0) or 0) if item else 0,
            })

        return jsonify(out)

    return _fallback_inventory()



@bp.get("/api/lookup/procedures")
def api_lookup_procedures():
    if not current_user.is_authenticated:
        return jsonify([])

    if PriceItem is None or PriceBook is None or Payer is None:
        current_app.logger.warning("Lookup models not available (PriceItem/PriceBook/Payer).")
        return jsonify([])

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    patient_id = request.args.get("patient_id", type=int)
    patient = Patient.query.get(patient_id) if patient_id else None
    book = _resolve_price_book_for_patient(patient) if patient else None
    book_id = book.id if book else _cash_pricebook_id()

    insurer = None
    if patient and (patient.insurance_provider or "").strip().lower() not in ("", "cash"):
        insurer = Insurer.query.filter(Insurer.name.ilike(patient.insurance_provider)).first()

    price_items = []
    if book_id:
        price_items = (
            PriceItem.query
            .filter(PriceItem.pricebook_id == book_id)
            .filter(func.lower(func.trim(func.coalesce(PriceItem.item_type, ""))) == "procedure")
            .filter(
                or_(
                    PriceItem.item_name.ilike(f"%{q}%"),
                    PriceItem.item_code.ilike(f"%{q}%")
                )
            )
            .order_by(PriceItem.item_name.asc())
            .limit(50)
            .all()
        )

    price_by_code = {(pi.item_code or "").strip(): pi for pi in price_items if pi.item_code}
    price_by_name = {(pi.item_name or "").strip().lower(): pi for pi in price_items if pi.item_name}

    results = []
    seen_proc_ids = set()

    if Procedure:
        procs = (
            Procedure.query
            .filter(or_(Procedure.name.ilike(f"%{q}%"), Procedure.code.ilike(f"%{q}%")))
            .limit(30)
            .all()
        )
        for proc in procs:
            unit_price = proc.default_price or 0
            hit = None
            if proc.code:
                hit = price_by_code.get(proc.code.strip())
            if not hit:
                hit = price_by_name.get((proc.name or "").strip().lower())
            if hit and getattr(hit, "sell_price", None) is not None:
                unit_price = hit.sell_price
            elif insurer and ProcedurePrice:
                try:
                    pp = ProcedurePrice.query.filter_by(procedure_id=proc.id, insurer_id=insurer.id).first()
                    if pp and pp.price is not None:
                        unit_price = pp.price
                except Exception:
                    pass
            seen_proc_ids.add(proc.id)
            results.append({"id": proc.id, "name": proc.name, "price": float(unit_price or 0)})

    # Fallback for setups where pricebook has procedure rows not present in Procedure catalog.
    for pi in price_items:
        proc = None
        if Procedure:
            try:
                code = (getattr(pi, "item_code", None) or "").strip()
                name = (getattr(pi, "item_name", None) or "").strip()
                if code:
                    proc = Procedure.query.filter(func.lower(func.trim(func.coalesce(Procedure.code, ""))) == code.lower()).first()
                if not proc and name:
                    proc = Procedure.query.filter(func.lower(func.trim(func.coalesce(Procedure.name, ""))) == name.lower()).first()
            except Exception:
                proc = None

        if proc and proc.id not in seen_proc_ids:
            seen_proc_ids.add(proc.id)
            results.append({
                "id": proc.id,
                "name": proc.name,
                "price": float(getattr(pi, "sell_price", 0) or 0),
            })
        elif not proc:
            # keep suggestion visible even if catalog link is missing; backend can still bill by name
            results.append({
                "id": "",
                "name": (getattr(pi, "item_name", None) or getattr(pi, "item_code", None) or "Procedure"),
                "price": float(getattr(pi, "sell_price", 0) or 0),
            })

    return jsonify(results)


@bp.get("/api/lookup/labs")
def api_lookup_labs():
    if not current_user.is_authenticated:
        return jsonify([])

    if PriceItem is None or PriceBook is None or Payer is None:
        current_app.logger.warning("Lookup models not available (PriceItem/PriceBook/Payer).")
        return jsonify([])

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    patient_id = request.args.get("patient_id", type=int)
    patient = Patient.query.get(patient_id) if patient_id else None
    book = _resolve_price_book_for_patient(patient) if patient else None
    book_id = book.id if book else _cash_pricebook_id()
    if not book_id:
        return jsonify([])

    rows = (
        PriceItem.query
        .filter(PriceItem.pricebook_id == book_id)
        # tolerate legacy/csv values like "Lab" or "LAB"
        .filter(func.lower(func.trim(func.coalesce(PriceItem.item_type, ""))) == "lab")
        .filter(PriceItem.item_name.ilike(f"%{q}%"))
        .order_by(PriceItem.item_name.asc())
        .limit(20)
        .all()
    )

    out = []
    for r in rows:
        proc_id = ""
        if Procedure:
            try:
                proc = (
                    Procedure.query
                    .filter(Procedure.name.ilike(r.item_name))
                    .filter(Procedure.category.ilike("lab"))
                    .first()
                )
                proc_id = proc.id if proc else ""
            except Exception:
                proc_id = ""
        out.append({
            "id": proc_id,
            "name": r.item_name,
            "price": float(getattr(r, "sell_price", 0) or 0),
        })

    return jsonify(out)


def _cash_pricebook_id():
    """
    Select a 'cash/self' price book if your Payer name includes cash/self.
    Otherwise fall back to the most recent price book.
    """
    if PriceBook is None or Payer is None:
        return None

    # 1) Try payer name matching cash/self
    try:
        pb = (
            db.session.query(PriceBook)
            .join(Payer, PriceBook.payer_id == Payer.id)
            .filter(
                or_(
                    func.lower(Payer.name).like("%cash%"),
                    func.lower(Payer.name).like("%self%"),
                    func.lower(Payer.name).like("%private%"),
                    func.lower(Payer.name).like("%walk%")
                )
            )
            .order_by(PriceBook.id.desc())
            .first()
        )
        if pb:
            return pb.id
    except Exception:
        current_app.logger.exception("Failed to find cash/self payer pricebook; falling back.")

    # 2) Fallback: latest pricebook
    pb = PriceBook.query.order_by(PriceBook.id.desc()).first()
    return pb.id if pb else None


# ---------- Optional models (guarded) ----------
try:
    from ..models import InvoiceLine
except Exception:
    InvoiceLine = None

try:
    from ..models import InvoiceItem
except Exception:
    InvoiceItem = None

try:
    from ..models import LabOrder, LabOrderLine
except Exception:
    LabOrder = LabOrderLine = None

try:
    from ..models import ProcedurePrice
except Exception:
    ProcedurePrice = None



# ---------- Helpers ----------
def _normalized_kind(proc_id=None, item_id=None):
    """
    Map the 'kind' to allowed enum values:
      - 'drug'      if linked to an inventory item
      - 'procedure' if linked to a procedure (incl. lab procedures)
      - 'other'     otherwise
    """
    if item_id:
        return "drug"
    if proc_id:
        return "procedure"
    return "other"


def lookup_test_price(name: str):
    """
    Returns (procedure, price) for a given test name.
    Tries Procedure first (category 'lab' if available), else fallback map.
    """
    clean = (name or "").strip()
    if not clean:
        return (None, 0)

    proc = None
    price = 0

    try:
        if Procedure:
            q = Procedure.query.filter(Procedure.name.ilike(clean))
            if hasattr(Procedure, "category"):
                q = q.filter(Procedure.category.ilike("lab"))
            proc = q.first()
            if proc:
                for f in ("cash_price", "price", "unit_price", "amount"):
                    if hasattr(proc, f) and getattr(proc, f) is not None:
                        try:
                            price = int(float(getattr(proc, f)))
                            break
                        except Exception:
                            pass
    except Exception:
        proc = None

    if price <= 0:
        fallback = {
            "Full Blood Count (FBC)": 25000,
            "Malaria Rapid Test": 10000,
            "Thick/Thin Film (Malaria Microscopy)": 15000,
            "CRP": 25000,
            "Urinalysis": 10000,
            "Stool Analysis": 15000,
            "H. pylori": 30000,
            "Blood Glucose (RBS/FBS)": 10000,
            "LFTs": 60000,
            "RFTs": 60000,
            "Widal Test": 20000,
        }
        price = int(fallback.get(clean, 0))

    return (proc, price)


def _get_or_create_open_invoice(patient_id: int, visit_id: int | None):
    """
    Find an open/draft invoice for the patient (and visit if provided),
    otherwise create one. Ensures issue_date and number are set via utils.py.
    """
    inv = None
    try:
        q = Invoice.query.filter(Invoice.patient_id == patient_id)
        q = Invoice.query.filter(Invoice.patient_id == patient_id)

        if visit_id and hasattr(Invoice, "visit_id"):
            q = q.filter(Invoice.visit_id == visit_id)

        open_statuses = {"open", "unpaid", "pending", "draft"}
        inv = (
            (q.filter(Invoice.status.in_(list(open_statuses))) if hasattr(Invoice, "status") else q)
            .order_by(Invoice.created_at.desc())
            .first()
        )
    except Exception:
        inv = None

    today = datetime.utcnow()
    today_str = today.strftime("%Y-%m-%d")

    if inv:
        changed = False
        if hasattr(inv, "issue_date") and not inv.issue_date:
            inv.issue_date = today_str
            changed = True
        if not getattr(inv, "number", None):
            inv.number = generate_invoice_number(today.date())
            changed = True
        if changed:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        return inv

    # Create a new invoice
    inv_kwargs = dict(patient_id=patient_id)
    if hasattr(Invoice, "visit_id") and visit_id:
        inv_kwargs["visit_id"] = visit_id
    if hasattr(Invoice, "status"):
        inv_kwargs["status"] = "open"
    if hasattr(Invoice, "created_at"):
        inv_kwargs["created_at"] = today
    if hasattr(Invoice, "issue_date"):
        inv_kwargs["issue_date"] = today_str
    if hasattr(Invoice, "description"):
        inv_kwargs["description"] = ""
    if hasattr(Invoice, "payer_type"):
        inv_kwargs["payer_type"] = "Cash"
    if hasattr(Invoice, "amount"):
        inv_kwargs["amount"] = Decimal("0")

    inv = Invoice(**inv_kwargs)
    db.session.add(inv)
    db.session.flush()  # get inv.id
    inv.number = generate_invoice_number(today.date())
    db.session.commit()
    return inv


def _safe_setattr(obj, field, value):
    if hasattr(obj, field):
        setattr(obj, field, value)
        return True
    return False


def _append_tagged_notes(v: Visit, tag: str, text: str):
    base = (v.notes or "").strip()
    section = f"\n\n[{tag}]\n{text.strip()}" if text and text.strip() else ""
    v.notes = (base + section).strip() if section else base


def _resolve_payer_kind(p: Patient) -> str:
    ip = (p.insurance_provider or "").strip().lower()
    return "Insurance" if (ip and ip != "cash") else "Cash"


def _get_or_create_invoice(p: Patient, v: Visit, issue_date: datetime | None = None) -> Invoice:
    inv = Invoice.query.filter_by(visit_id=v.id).order_by(Invoice.id.desc()).first()
    if inv:
        return inv
    inv = Invoice(
        patient_id=p.id,
        visit_id=v.id,
        issue_date=(issue_date or datetime.utcnow()).strftime("%Y-%m-%d"),
        description="",
        amount=Decimal("0"),
        payer_type=_resolve_payer_kind(p),
    )
    db.session.add(inv)
    db.session.flush()
    inv.number = generate_invoice_number(issue_date or datetime.utcnow())
    db.session.commit()
    return inv


def _get_catalog(model_cls, name_attr="name"):
    try:
        rows = model_cls.query.order_by(getattr(model_cls, name_attr).asc()).all()
        return [(r.id, getattr(r, name_attr, str(r.id))) for r in rows]
    except Exception:
        return []


def _resolve_price_book_for_patient(p: Patient):
    """Pick insurer-matched PriceBook if any; otherwise Cash/default."""
    if not PriceBook:
        return None

    def _normalize_payer_name(name: str) -> str:
        if not name:
            return ""
        return name.strip().lower().replace("insurance", "").strip()

    ip_raw = (p.insurance_provider or "").strip()
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
            if hasattr(PriceBook, "insurer_id"):
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

    for b in candidates:
        nm = (getattr(b, "name", "") or "").lower()
        typ = (getattr(b, "type", "") or "").lower()
        if "cash" in nm or typ == "cash":
            return b

    return candidates[0] if candidates else None


def _lab_items_from_book(book) -> list[dict]:
    """Return lab tests from PriceBook as dicts: {id, name, code, price}."""
    if not (PriceItem and book):
        return []

    items: list[dict] = []
    try:
        q = PriceItem.query.filter_by(pricebook_id=book.id)
        if hasattr(PriceItem, "item_type"):
            q = q.filter(PriceItem.item_type == "lab")

        order_col = PriceItem.item_name if hasattr(PriceItem, "item_name") else PriceItem.id
        rows = q.order_by(order_col.asc()).all()

        for r in rows:
            name = getattr(r, "item_name", None) or "Lab Test"
            code = getattr(r, "item_code", None)
            price = getattr(r, "sell_price", 0) or 0
            items.append({"id": r.id, "name": name, "code": code, "price": float(price)})
    except Exception:
        return []

    return items


def _lab_catalog_for_patient(p: Patient) -> list[dict]:
    book = _resolve_price_book_for_patient(p)
    items = _lab_items_from_book(book)
    if not items and (p.insurance_provider or "").strip():
        class _CashProxy:
            pass
        fake = _CashProxy()
        fake.insurance_provider = "Cash"
        items = _lab_items_from_book(_resolve_price_book_for_patient(fake))
    return items


def _get_or_create_open_visit(patient_id: int) -> Visit:
    v = (Visit.query
         .filter_by(patient_id=patient_id)
         .order_by(Visit.id.desc())
         .first())

    # Create a NEW visit if none exists OR last one is closed
    if (not v) or (getattr(v, "status", "").lower() == "closed") or (getattr(v, "closed_at", None) is not None):
        v = Visit(
            patient_id=patient_id,
            visit_date=datetime.utcnow().strftime("%Y-%m-%d"),
            reason="Auto-created",
            notes="",
            status="Open" if hasattr(Visit, "status") else getattr(v, "status", None),
        )
        db.session.add(v)
        db.session.flush()

    return v



def _enqueue_billing(patient_id: int, visit_id: int | None, note: str = ""):
    """Create or reopen a Billing queue entry for this visit."""
    if not visit_id:
        return
    try:
        q = (
            BillingQueue.query
            .filter_by(visit_id=visit_id)
            .order_by(BillingQueue.id.desc())
            .first()
        )
        if q:
            if hasattr(q, "status"):
                q.status = "Open"
            if hasattr(q, "kind"):
                q.kind = "BILLING"
            if note and hasattr(q, "description"):
                q.description = note
        else:
            q = BillingQueue()
            if hasattr(q, "patient_id"):
                q.patient_id = patient_id
            if hasattr(q, "visit_id"):
                q.visit_id = visit_id
            if hasattr(q, "status"):
                q.status = "Open"
            if hasattr(q, "added_at"):
                q.added_at = datetime.utcnow()
            if hasattr(q, "kind"):
                q.kind = "BILLING"
            if note and hasattr(q, "description"):
                q.description = note
            db.session.add(q)
    except Exception:
        pass

def _drug_unit_price(p: Patient, item_id: int | None, drug_name: str = "") -> Decimal:
    """
    Price priority:
      1) If insurance patient and ItemPrice exists -> ItemPrice.price
      2) Patient-matched PriceBook -> PriceItem.sell_price (drug rows)
      3) Inventory Item.sell_price
      4) 0
    """
    # 1) insurance item price table (per insurer)
    try:
        ip_raw = (p.insurance_provider or "").strip()
        if ip_raw and ip_raw.lower() != "cash" and item_id:
            ins = Insurer.query.filter(Insurer.name.ilike(ip_raw)).first()
            if ins:
                from ..models import ItemPrice
                row = ItemPrice.query.filter_by(item_id=item_id, insurer_id=ins.id).first()
                if row and getattr(row, "price", None) is not None:
                    return Decimal(str(row.price))
    except Exception:
        pass

    # 2) pricebook matched to this patient
    try:
        book = _resolve_price_book_for_patient(p)
        if book and PriceItem is not None:
            nm = (drug_name or "").strip()
            inv_item = Item.query.get(item_id) if (Item is not None and item_id) else None
            if inv_item and not nm:
                nm = inv_item.name

            q = PriceItem.query.filter_by(pricebook_id=book.id)
            if hasattr(PriceItem, "item_type"):
                q = q.filter(PriceItem.item_type == "drug")

            # Match by code (sku) or name
            if inv_item and getattr(inv_item, "sku", None):
                q2 = q.filter(PriceItem.item_code == inv_item.sku).first()
                if q2 and getattr(q2, "sell_price", None) is not None:
                    return Decimal(str(q2.sell_price))

            if nm:
                q1 = q.filter(PriceItem.item_name.ilike(nm)).first()
                if q1 and getattr(q1, "sell_price", None) is not None:
                    return Decimal(str(q1.sell_price))
    except Exception:
        pass

    # 3) inventory sell price
    try:
        if Item is not None and item_id:
            it = Item.query.get(item_id)
            if it and getattr(it, "sell_price", None) is not None:
                return Decimal(str(it.sell_price))
    except Exception:
        pass

    return Decimal("0")

def _lab_results_for_visit(visit_id: int):
    """
    Build a simple "results list" for the chart from LabOrder/LabOrderLine.

    Your models.py uses LabOrder + LabOrderLine (no LabResult model),
    and LabOrderLine carries the result fields (result_value/result_text/etc).
    """
    if not visit_id:
        return []

    # Prefer the explicitly imported models if present
    try:
        LO = LabOrder if "LabOrder" in globals() else None
        LL = LabOrderLine if "LabOrderLine" in globals() else None
    except Exception:
        LO = None
        LL = None

    if not LO or not LL:
        return []

    rows = []
    try:
        q = (
            db.session.query(LL, LO)
            .join(LO, LL.order_id == LO.id)
            .filter(LO.visit_id == visit_id)
        )

        # Ordering (best-effort)
        if hasattr(LL, "created_at"):
            q = q.order_by(LL.created_at.desc())
        elif hasattr(LO, "created_at"):
            q = q.order_by(LO.created_at.desc())
        else:
            q = q.order_by(LL.id.desc())

        for line, order in q.all():
            rows.append({
                "test_name": getattr(line, "test_name", None) or getattr(line, "name", None) or "Result",
                "created_at": getattr(line, "created_at", None) or getattr(order, "created_at", None) or "",
                "status": getattr(line, "status", None) or getattr(order, "status", None) or "Ready",
                "result_text": getattr(line, "result_text", None) or getattr(line, "comment", None) or None,
                "value": getattr(line, "result_value", None) or getattr(line, "value", None) or None,
                "units": getattr(line, "result_units", None) or getattr(line, "units", None) or "",
            })
    except Exception:
        current_app.logger.exception("Failed to load lab results for visit_id=%s", visit_id)
        rows = []

    return rows


# ---------- Clinical: Save clinician sections ----------
@bp.route("/patients", methods=["GET"])
@login_required
@roles_required("reception", "nurse", "doctor", "pediatrician", "admin")
def patients_list():
    q = (request.args.get("q") or "").strip()
    query = Patient.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Patient.first_name.ilike(like),
            Patient.last_name.ilike(like),
            Patient.phone.ilike(like),
            Patient.email.ilike(like),
            Patient.insurance_provider.ilike(like),
            Patient.policy_number.ilike(like),
            cast(Patient.id, String).ilike(like),
            Patient.patient_code.ilike(like),
        ))
    patients = query.order_by(Patient.id.desc()).limit(300).all()

    open_q_query = BillingQueue.query.filter_by(status="Open")
    if hasattr(BillingQueue, "kind"):
        open_q_query = open_q_query.filter(BillingQueue.kind == "BILLING")
    open_q = open_q_query.order_by(BillingQueue.added_at.asc()).all()

    return render_template(
        "patients_list.html",
        patients=patients,
        q=q,
        billing_queue=open_q,
    )


# ---------- Send to Lab (keeps visit open, auto-invoice) ----------
@bp.post("/visits/<int:visit_id>/send-to-lab")
@login_required
@roles_required("doctor", "pediatrician", "admin")
def visit_send_to_lab(visit_id):
    v = Visit.query.get_or_404(visit_id)
    p = Patient.query.get_or_404(v.patient_id)

    raw_ids = request.form.getlist("lab_tests")
    if not raw_ids:
        flash("Select at least one lab test.", "warning")
        return redirect(url_for("patients.patient_chart", patient_id=p.id, tab="lab"))

    inv = _get_or_create_invoice(p, v)
    
    # 1) Close any open DOCTOR queue entries for this visit
    try:
        dq = BillingQueue.query.filter_by(visit_id=v.id, status="Open").all()
        for e in dq:
            if (getattr(e, "kind", "") or "").upper() == "DOCTOR":
                e.status = "Closed"
    except Exception:
        pass

    # 2) Add to LAB queue (patient leaves doctor queue when sent to lab)
    labq = BillingQueue()
    if hasattr(labq, "patient_id"): labq.patient_id = p.id
    if hasattr(labq, "visit_id"):   labq.visit_id   = v.id
    if hasattr(labq, "status"):     labq.status     = "Open"
    if hasattr(labq, "added_at"):   labq.added_at   = datetime.utcnow()
    if hasattr(labq, "kind"):       labq.kind       = "LAB"
    if hasattr(labq, "description"):labq.description= "Sent to Lab from Doctor"
    db.session.add(labq)
    
    chosen = []
    if PriceItem:
        try:
            ids = [int(x) for x in raw_ids if str(x).isdigit()]
            if ids:
                chosen = PriceItem.query.filter(PriceItem.id.in_(ids)).all()
        except Exception:
            chosen = []

    if not chosen:
        flash("Selected lab tests not found in price book.", "danger")
        return redirect(url_for("patients.patient_chart", patient_id=p.id, tab="lab"))

    # If no InvoiceLine model, just log notes
    if not InvoiceLine:
        for r in chosen:
            name = getattr(r, "item_name", None) or getattr(r, "item_code", "Lab Test")
            _append_tagged_notes(v, "Lab Request", name)
        db.session.commit()
        flash("Lab tests noted in visit.", "success")
        return redirect(url_for("lab.lab_queue", from_chart=1, patient_id=p.id, visit_id=v.id))

    # 1) Create invoice lines
    new_lines = []
    for r in chosen:
        name = (getattr(r, "item_name", None) or getattr(r, "item_code", "Lab Test"))

        # map to Procedure by name so kind can be 'procedure'
        proc_id = None
        if Procedure:
            try:
                qq = Procedure.query.filter(Procedure.name.ilike(name))
                if hasattr(Procedure, "category"):
                    qq = qq.filter(Procedure.category.ilike("lab"))
                proc = qq.first()
                proc_id = getattr(proc, "id", None) if proc else None
            except Exception:
                proc_id = None

        # unit price from PriceItem
        if hasattr(r, "price"):
            unit = Decimal(str(r.price or "0"))
        elif hasattr(r, "sell_price"):
            unit = Decimal(str(r.sell_price or "0"))
        elif hasattr(r, "amount"):
            unit = Decimal(str(r.amount or "0"))
        else:
            unit = Decimal("0")

        ln = InvoiceLine()
        if hasattr(ln, "invoice_id"):
            ln.invoice_id = inv.id
        if hasattr(ln, "kind"):
            ln.kind = _normalized_kind(proc_id=proc_id)
        if proc_id and hasattr(ln, "procedure_id"):
            ln.procedure_id = proc_id
        if hasattr(ln, "description"):
            ln.description = name
        if hasattr(ln, "qty"):
            ln.qty = 1
        if hasattr(ln, "unit_price"):
            ln.unit_price = unit
        if hasattr(ln, "line_total"):
            ln.line_total = unit
        if hasattr(ln, "price_item_id"):
            ln.price_item_id = r.id

        db.session.add(ln)
        new_lines.append(ln)

    # recompute invoice
    try:
        inv.amount = sum((l.line_total or Decimal("0")) for l in getattr(inv, "lines", new_lines))
    except Exception:
        inv.amount = (inv.amount or Decimal("0")) + sum(
            (getattr(ln, "line_total", Decimal("0")) for ln in new_lines)
        )

        # --- Ensure lab aliases exist (prevents NameError even if lab module is optional) ---
    try:
        _LabOrderModel
    except NameError:
        _LabOrderModel = None
    try:
        _LabLineModel
    except NameError:
        _LabLineModel = None
    try:
        _LabResultModel
    except NameError:
        _LabResultModel = None


        # 2) Create LabOrder + LabOrderLine (robust)
    try:
        if LabOrder is not None:
            lo = LabOrder()
            if hasattr(lo, "patient_id"): lo.patient_id = p.id
            if hasattr(lo, "visit_id"):   lo.visit_id   = v.id
            if hasattr(lo, "status"):     lo.status     = "PendingPayment"
            if hasattr(lo, "created_at"): lo.created_at = datetime.utcnow()
            if hasattr(lo, "created_by"): lo.created_by = getattr(current_user, "id", None)

            names = ", ".join([
                (getattr(r, "item_name", None) or getattr(r, "item_code", "Lab Test"))
                for r in chosen
            ])
            if hasattr(lo, "tests"):
                lo.tests = names
            elif hasattr(lo, "description"):
                lo.description = f"Requested tests: {names}"

            db.session.add(lo)
            db.session.flush()  # ensure lo.id exists

            if LabOrderLine is not None:
                for r in chosen:
                    tname = (getattr(r, "item_name", None) or getattr(r, "item_code", "Lab Test"))
                    line = LabOrderLine(
                        order_id=lo.id,
                        procedure_id=None,
                        test_name=tname
                    )
                    db.session.add(line)

        else:
            current_app.logger.warning("LabOrder model not available; will rely on BillingQueue only.")

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to create lab order/lines")
        flash("Could not create lab order. Please try again.", "danger")
        return redirect(url_for("patients.patient_chart", patient_id=p.id, tab="lab"))


    # 3) Put/keep the visit in the Billing queue
    _enqueue_billing(
        p.id,
        v.id,
        note=f"Lab tests: {', '.join([(getattr(r,'item_name', None) or getattr(r,'item_code','Lab Test')) for r in chosen])}"
    )

    db.session.commit()
    flash(
        f"Added {len(chosen)} lab test(s) to invoice. Patient must pay before the request reaches the lab queue.",
        "success",
    )
    return redirect(url_for("patients.patient_chart", patient_id=p.id, tab="billing"))


# --------- EVERYTHING ELSE BELOW THIS POINT: leave as-is in your project ---------
# (You can paste the rest of your existing file below unchanged.)



# ---------- Invoice recompute ----------
@bp.post("/visits/<int:visit_id>/reprice_invoice")
@login_required
@roles_required("doctor", "pediatrician", "admin")
def visit_reprice_invoice(visit_id):
    v = Visit.query.get_or_404(visit_id)
    inv = Invoice.query.filter_by(visit_id=visit_id).order_by(Invoice.id.desc()).first()
    if not inv:
        flash("No invoice yet for this visit.", "warning")
        return redirect(url_for("patients.patient_chart", patient_id=v.patient_id))
    inv.amount = sum((l.line_total or Decimal("0")) for l in getattr(inv, "lines", []))
    db.session.commit()
    flash("Invoice updated.", "success")
    return redirect(url_for("patients.patient_chart", patient_id=v.patient_id))


# ---------- Registration / Edit / Detail ----------
@bp.route("/patients/new", methods=["GET", "POST"])
@login_required
@roles_required("reception", "nurse", "admin")
def patients_new():
    form = PatientForm()
    insurers = (
        Insurer.query.filter_by(active=True)
        .order_by(Insurer.name.asc())
        .limit(10)
        .all()
    )
    if form.validate_on_submit():
        p = Patient(
            first_name=(form.first_name.data or "").strip(),
            last_name=(form.last_name.data or "").strip(),
            sex=form.sex.data or None,
            date_of_birth=(
                form.date_of_birth.data.strftime("%Y-%m-%d")
                if form.date_of_birth.data else None
            ),
            phone=(form.phone.data or "").strip(),
            email=((form.email.data or "").strip() or None),
            address=((form.address.data or "").strip() or None),
            next_of_kin=((form.next_of_kin.data or "").strip() or None),
            insurance_provider=((form.insurance_provider.data or "").strip() or None),
            policy_number=((form.policy_number.data or "").strip() or None),
            allergies=((form.allergies.data or "").strip() or None),
            medical_history=((form.medical_history.data or "").strip() or None),
            consent=bool(form.consent.data),
        )
        db.session.add(p)
        db.session.flush()
        p.patient_code = generate_patient_code()
        db.session.commit()
        flash("Patient registered.", "success")
        return redirect(url_for("patients.patients_list"))

    if request.method == "POST":
        flash("Please correct the errors below.", "danger")

    return render_template("patients_new.html", form=form, insurers=insurers)


@bp.route("/patients/<int:patient_id>")
@login_required
@roles_required("reception", "nurse", "doctor", "pediatrician", "admin", "labtech")
def patient_detail(patient_id):
    return redirect(url_for("patients.patient_chart", patient_id=patient_id))


@bp.route("/patients/<int:patient_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required("reception", "nurse", "admin")
def patient_edit(patient_id):
    p = Patient.query.get_or_404(patient_id)
    form = PatientForm(obj=p)

    if request.method == "GET":
        if isinstance(p.date_of_birth, str) and p.date_of_birth:
            try:
                form.date_of_birth.data = datetime.strptime(p.date_of_birth, "%Y-%m-%d").date()
            except Exception:
                form.date_of_birth.data = None

    if form.validate_on_submit():
        p.first_name = (form.first_name.data or "").strip()
        p.last_name = (form.last_name.data or "").strip()
        p.sex = form.sex.data or None
        p.date_of_birth = (
            form.date_of_birth.data.strftime("%Y-%m-%d")
            if form.date_of_birth.data else None
        )
        p.phone = (form.phone.data or "").strip()
        p.email = ((form.email.data or "").strip() or None)
        p.address = ((form.address.data or "").strip() or None)
        p.next_of_kin = ((form.next_of_kin.data or "").strip() or None)
        p.insurance_provider = ((form.insurance_provider.data or "").strip() or None)
        p.policy_number = ((form.policy_number.data or "").strip() or None)
        p.allergies = ((form.allergies.data or "").strip() or None)
        p.medical_history = ((form.medical_history.data or "").strip() or None)
        p.consent = bool(form.consent.data)

        db.session.commit()
        flash("Patient updated.", "success")
        return redirect(url_for("patients.patient_chart", patient_id=p.id))

    if request.method == "POST":
        flash("Please correct the errors below.", "danger")

    return render_template("patients_new.html", form=form, edit_mode=True, p=p)


# ---------- Add Visit & Chart ----------
@bp.post("/patients/<int:patient_id>/add_visit")
@login_required
@roles_required("reception", "nurse", "doctor", "pediatrician", "admin")
def add_visit(patient_id):
    p = Patient.query.get_or_404(patient_id)
    form = VisitForm()
    if not form.validate_on_submit():
        flash("Please correct the visit form errors.", "danger")
        return redirect(url_for("patients.patient_chart", patient_id=p.id))

    v = Visit(
        patient_id=p.id,
        visit_date=form.visit_date.data.strftime("%Y-%m-%d"),
        reason=(form.reason.data or "").strip(),
        notes=(form.notes.data or "").strip(),
        diagnosis=(form.diagnosis.data or "").strip(),
        weight_kg=form.weight_kg.data,
        height_cm=form.height_cm.data,
        temp_c=form.temp_c.data,
        pulse_bpm=int(form.pulse_bpm.data) if form.pulse_bpm.data is not None else None,
        bp_sys=int(form.bp_sys.data) if form.bp_sys.data is not None else None,
        bp_dia=int(form.bp_dia.data) if form.bp_dia.data is not None else None,
        spo2=int(form.spo2.data) if form.spo2.data is not None else None,
    )
    db.session.add(v)
    db.session.flush()

    payer_type = "Insurance" if ((p.insurance_provider or "").strip().lower() not in ("", "cash")) else "Cash"
    inv = Invoice(
        patient_id=p.id,
        visit_id=v.id,
        issue_date=form.visit_date.data.strftime("%Y-%m-%d"),
        description="",
        amount=Decimal("0"),
        payer_type=payer_type,
    )
    db.session.add(inv)
    db.session.flush()
    inv.number = generate_invoice_number(form.visit_date.data)

    db.session.commit()
    flash("Visit saved. You can now add procedures, drugs, and lab tests.", "success")
    return redirect(url_for("patients.patient_chart", patient_id=p.id))


@bp.route("/patients/<int:patient_id>/chart", methods=["GET"])
@login_required
def patient_chart(patient_id):
    """Chart view: always drives data off the selected visit_id."""
    patient = Patient.query.get_or_404(patient_id)

    # Always fetch visits list (right panel)
    visits = Visit.query.filter_by(patient_id=patient_id).order_by(Visit.id.desc()).all()

    # Selected visit
    visit_id = request.args.get("visit_id", type=int)
    if visit_id:
        visit = Visit.query.filter_by(patient_id=patient_id, id=visit_id).first()
    else:
        visit = visits[0] if visits else None

    # Visit-specific invoice (THIS is what prevents mixing across visits)
    visit_invoice = None
    if visit:
        try:
            visit_invoice = (
                Invoice.query
                .filter_by(patient_id=patient_id, visit_id=visit.id)
                .order_by(Invoice.id.desc())
                .first()
            )
        except Exception:
            visit_invoice = None

    # (Optional) Show all invoices in the side list, but the "current invoice" panel must use visit_invoice
    invoices = Invoice.query.filter_by(patient_id=patient_id).order_by(Invoice.id.desc()).all()

    # Catalogs (kept as-is)
    drug_options = _get_catalog(_DrugModel, "name") if _DrugModel else []
    procedure_options = _get_catalog(_ProcModel, "name") if _ProcModel else []

    # Lab tests available for this patient/payer
    lab_tests_options = _lab_catalog_for_patient(patient)

    # Results must be visit-based (your DB uses LabOrder/LabOrderLine, not LabResult)
    lab_results = _lab_results_for_visit(visit.id) if visit else []

    active_tab = (request.args.get("tab") or "visits").lower()
    if active_tab not in {"visits", "billing", "lab"}:
        active_tab = "visits"

    return render_template(
        "patient_chart.html",
        p=patient,
        v=visit,
        visits=visits,
        invoices=invoices,
        visit_invoice=visit_invoice,   # ✅ template will use this
        visit_form=VisitForm(obj=visit) if visit else VisitForm(),
        inv_form=InvoiceForm(),
        pay_form=PaymentForm(),
        active_tab=active_tab,
        drug_options=drug_options,
        procedure_options=procedure_options,
        lab_tests_options=lab_tests_options,
        lab_results=lab_results,       # ✅ now visit-based and works with LabOrderLine
    )

# ---------- Close visit -> route to Pharmacy/Billing ----------
@bp.post("/visits/<int:visit_id>/close")
@login_required
@roles_required("doctor", "pediatrician", "admin")
def visit_close_and_bill(visit_id):
    v = Visit.query.get_or_404(visit_id)

    inv = Invoice.query.filter_by(visit_id=visit_id).order_by(Invoice.id.desc()).first()
    if not inv:
        p = Patient.query.get(v.patient_id)
        inv = Invoice(
            patient_id=v.patient_id,
            visit_id=v.id,
            issue_date=datetime.utcnow().strftime("%Y-%m-%d"),
            description="",
            amount=Decimal("0"),
            payer_type=_resolve_payer_kind(p),
        )
        db.session.add(inv)
        db.session.flush()
        inv.number = generate_invoice_number(datetime.utcnow())
        db.session.flush()  # ensure inv.id exists for lines
        
        v.status = "Closed"
        v.closed_at = datetime.utcnow()
        inv = Invoice.query.filter_by(patient_id=v.patient_id, visit_id=v.id).order_by(Invoice.id.desc()).first()
        if inv and hasattr(inv, "status"):
            inv.status = "closed"


    
        # ---- Add prescribed drugs (if any) to invoice on Save & Close ----
    drug_ids   = request.form.getlist("drug_id[]")
    drug_names = request.form.getlist("drug_name[]")
    doses      = request.form.getlist("dose[]")
    qtys       = request.form.getlist("qty[]")

    for i in range(max(len(drug_ids), len(drug_names))):
        item_id = int(drug_ids[i]) if i < len(drug_ids) and str(drug_ids[i]).isdigit() else None
        drug_name = (drug_names[i].strip() if i < len(drug_names) else "")
        dose = (doses[i].strip() if i < len(doses) else "")
        qty = int(qtys[i]) if i < len(qtys) and str(qtys[i]).isdigit() else 1

        if not item_id and not drug_name:
            continue

        # Prevent duplicate lines if user previously clicked "Add all drugs"
        try:
            desc = drug_name or "Drug"
            if dose:
                desc = f"{desc} ({dose})"

            exists = False
            for l in getattr(inv, "lines", []) or []:
                if (getattr(l, "kind", "") or "").lower() == "drug":
                    if item_id and getattr(l, "item_id", None) == item_id:
                        exists = True
                        break
                    if (getattr(l, "description", "") or "").strip() == desc:
                        exists = True
                        break
            if exists:
                continue
        except Exception:
            pass

        line = InvoiceLine() if InvoiceLine else None
        if not line:
            # fallback: at least record in notes
            _append_tagged_notes(v, "Prescription", f"{drug_name} {dose} x{qty}")
            continue

        if hasattr(line, "invoice_id"): line.invoice_id = inv.id
        if hasattr(line, "kind"): line.kind = "drug"
        if hasattr(line, "item_id") and item_id: line.item_id = item_id

        desc = drug_name or "Drug"
        if dose:
            desc = f"{desc} ({dose})"
        if hasattr(line, "description"): line.description = desc

        if hasattr(line, "qty"): line.qty = qty

        unit_price = _drug_unit_price(Patient.query.get(v.patient_id), item_id, drug_name)
        if hasattr(line, "unit_price"): line.unit_price = unit_price
        if hasattr(line, "line_total"): line.line_total = unit_price * Decimal(str(qty))

        db.session.add(line)

    
    try:
        inv.amount = sum((l.line_total or Decimal("0")) for l in getattr(inv, "lines", []))
    except Exception:
        inv.amount = inv.amount or Decimal("0")

    has_drugs = False
    try:
        for l in getattr(inv, "lines", []):
            if (getattr(l, "kind", "") or "").lower() == "drug" or getattr(l, "item_id", None):
                has_drugs = True
                break
    except Exception:
        has_drugs = False
    has_procedures = False
    try:
        for l in getattr(inv, "lines", []):
            if (getattr(l, "kind", "") or "").lower() == "procedure":
                has_procedures = True
                break
            if getattr(l, "procedure_id", None) or getattr(l, "price_item_id", None):
                has_procedures = True
                break
    except Exception:
        has_procedures = False

    # Close any open queue entries that should no longer be active
    try:
        open_entries = BillingQueue.query.filter_by(visit_id=v.id, status="Open").all()
        for e in open_entries:
            if (getattr(e, "kind", "") or "").upper() in {"DOCTOR", "LAB", "TRIAGE"}:
                e.status = "Closed"
    except Exception:
        pass


    note = None
    if has_drugs and has_procedures:
        note = "Visit closed: drugs and procedures billed"
    elif has_drugs:
        note = "Visit closed: drugs billed"
    elif has_procedures:
        note = "Visit closed: procedures billed"
    if note:
        _enqueue_billing(v.patient_id, v.id, note=note)

    _safe_setattr(v, "closed_at", datetime.utcnow())
    _safe_setattr(v, "status", "Closed")

    db.session.commit()
    flash("Visit saved and routed to next step.", "success")
    return redirect(url_for("patients.doctors_queue"))

@bp.post("/visits/<int:visit_id>/update-clinical")
@bp.post("/visits/<int:visit_id>/clinical")
@login_required
def visit_update_clinical(visit_id):
    """
    Save / update clinical notes for a visit.
    Stores all sections inside Visit.notes using bracket format:
    [Presenting], [HOPC], [Exam], [Plan]
    Diagnosis is stored separately in Visit.diagnosis.
    """
    v = Visit.query.get_or_404(visit_id)

    presenting = (request.form.get("presenting") or "").strip()
    hopc       = (request.form.get("hopc") or "").strip()
    exam       = (request.form.get("exam") or "").strip()
    plan       = (request.form.get("plan") or "").strip()
    diagnosis  = (request.form.get("diagnosis") or "").strip()

    # Build the clinical block (matches what you printed in v.notes)
    clinical_block = (
        "[Presenting]\n"
        f"{presenting}\n\n"
        "[HOPC]\n"
        f"{hopc}\n\n"
        "[Exam]\n"
        f"{exam}\n\n"
        "[Plan]\n"
        f"{plan}\n"
    ).strip()

    existing = v.notes or ""

    tags = ["[Presenting]", "[HOPC]", "[Exam]", "[Plan]"]
    has_existing_block = any(t in existing for t in tags)

    if has_existing_block:
        # Keep anything written before the first clinical tag
        positions = [existing.find(t) for t in tags if t in existing]
        first_pos = min(positions) if positions else 0
        prefix = existing[:first_pos].rstrip()

        if prefix:
            v.notes = prefix + "\n\n" + clinical_block
        else:
            v.notes = clinical_block
    else:
        # No previous clinical notes → append cleanly
        if existing.strip():
            v.notes = existing.rstrip() + "\n\n" + clinical_block
        else:
            v.notes = clinical_block

    # Diagnosis (separate column)
    v.diagnosis = diagnosis or None

    db.session.commit()
    flash("Clinical notes saved.", "success")
    return redirect(url_for("patients.patient_chart", patient_id=v.patient_id, visit_id=v.id))


@bp.post("/visits/<int:visit_id>/add_procedure", endpoint="visit_add_procedure")
@login_required
def visit_add_procedure(visit_id):
    v = Visit.query.get_or_404(visit_id)

    proc_id = request.form.get("proc_id", type=int)
    proc_name = (request.form.get("procedure_name") or "").strip()

    if not InvoiceLine:
        flash("Procedure billing is not enabled (missing models).", "danger")
        return redirect(url_for("patients.patient_chart", patient_id=v.patient_id))

    proc = None
    if Procedure and proc_id:
        proc = Procedure.query.get(proc_id)

    if not proc and Procedure and proc_name:
        proc = (
            Procedure.query
            .filter(or_(Procedure.name.ilike(proc_name), Procedure.code.ilike(proc_name)))
            .first()
        )

    if not proc and not proc_name:
        flash("Please enter or select a procedure.", "warning")
        return redirect(url_for("patients.patient_chart", patient_id=v.patient_id))

    # Ensure invoice exists for this visit
    inv = Invoice.query.filter_by(visit_id=v.id).order_by(Invoice.id.desc()).first()
    if not inv:
        today = datetime.utcnow().date().strftime("%Y-%m-%d")
        payer_type = "Insurance" if (v.patient and v.patient.insurance_provider) else "Cash"

        inv = Invoice(
            patient_id=v.patient_id,
            visit_id=v.id,
            issue_date=today,
            payer_type=payer_type,
            amount=0,
        )
        db.session.add(inv)
        db.session.flush()  # ensures inv.id

    # Determine price: prefer matched PriceBook entry, then insurer-specific, else catalog default (if linked).
    unit_price = (proc.default_price or 0) if proc else 0
    insurer_amount = 0
    patient_amount = unit_price

    pricebook_hit = None
    try:
        book = _resolve_price_book_for_patient(v.patient) if v.patient else None
        if book and PriceItem is not None:
            pq = PriceItem.query.filter_by(pricebook_id=book.id)
            if hasattr(PriceItem, "item_type"):
                pq = pq.filter(func.lower(func.trim(func.coalesce(PriceItem.item_type, ""))) == "procedure")
            if proc and getattr(proc, "code", None):
                pricebook_hit = pq.filter(func.lower(func.trim(func.coalesce(PriceItem.item_code, ""))) == proc.code.strip().lower()).first()
            if not pricebook_hit and proc:
                pricebook_hit = pq.filter(func.lower(func.trim(func.coalesce(PriceItem.item_name, ""))) == proc.name.strip().lower()).first()
            if not pricebook_hit and proc_name:
                pricebook_hit = pq.filter(func.lower(func.trim(func.coalesce(PriceItem.item_name, ""))) == proc_name.lower()).first()
            if not pricebook_hit and proc_name:
                pricebook_hit = pq.filter(PriceItem.item_name.ilike(f"%{proc_name}%")).first()
            if pricebook_hit and getattr(pricebook_hit, "sell_price", None) is not None:
                unit_price = pricebook_hit.sell_price
    except Exception:
        pricebook_hit = None

    if inv.payer_type == "Insurance" and not pricebook_hit and proc:
        insurer_name = (v.patient.insurance_provider or "").strip() if v.patient else ""
        if insurer_name:
            insurer = Insurer.query.filter(Insurer.name.ilike(insurer_name)).first()
            if insurer:
                pp = ProcedurePrice.query.filter_by(procedure_id=proc.id, insurer_id=insurer.id).first() if ProcedurePrice else None
                if pp and pp.price is not None:
                    unit_price = pp.price

    if inv.payer_type == "Insurance":
        insurer_amount = unit_price
        patient_amount = 0

    qty = 1
    line_total = (unit_price or 0) * qty

    description = proc.name if proc else proc_name

    line = InvoiceLine(
        invoice_id=inv.id,
        kind="procedure",
        procedure_id=proc.id if proc else None,
        description=description,
        qty=qty,
        unit_price=unit_price,
        line_total=line_total,
        insurer_amount=insurer_amount,
        patient_amount=patient_amount,
    )
    db.session.add(line)

    # Update invoice amount
    inv.amount = (inv.amount or 0) + line_total

    _enqueue_billing(v.patient_id, v.id, note=f"Procedure added: {description}")

    try:
        db.session.commit()
        flash("Procedure added.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to add procedure: {e}", "danger")

    return redirect(url_for("patients.patient_chart", patient_id=v.patient_id))

# ---------- Queues ----------
@bp.post("/patients/<int:patient_id>/send-to-triage")
@login_required
@roles_required("reception", "nurse", "admin")
def patients_send_to_triage(patient_id):
    p = Patient.query.get_or_404(patient_id)
    v = _get_or_create_open_visit(p.id)

    q = BillingQueue()
    if hasattr(q, "patient_id"): q.patient_id = p.id
    if hasattr(q, "visit_id"): q.visit_id = v.id
    if hasattr(q, "status"): q.status = "Open"
    if hasattr(q, "added_at"): q.added_at = datetime.utcnow()
    if hasattr(q, "kind"): q.kind = "TRIAGE"
    if hasattr(q, "description"): q.description = f"Triage requested by {getattr(current_user, 'username', 'user')}"

    db.session.add(q)
    db.session.commit()
    flash(f"{p.first_name} {p.last_name} added to Triage queue.", "success")
    return redirect(url_for("patients.triage_queue"))

@bp.post("/patients/<int:patient_id>/send-to-pharmacy")
@login_required
@roles_required("nurse", "admin")
def patients_send_to_pharmacy(patient_id):
    p = Patient.query.get_or_404(patient_id)
    v = _get_or_create_open_visit(p.id)

    existing = (
        BillingQueue.query.filter_by(visit_id=v.id, status="Open")
        .filter(BillingQueue.kind == "PHARMACY")
        .first()
    )
    if existing:
        flash(f"{p.first_name} {p.last_name} is already in Pharmacy queue.", "info")
        return redirect(url_for("pharmacy.pharmacy_dashboard", queue_id=existing.id))

    q = BillingQueue()
    if hasattr(q, "patient_id"): q.patient_id = p.id
    if hasattr(q, "visit_id"): q.visit_id = v.id
    if hasattr(q, "status"): q.status = "Open"
    if hasattr(q, "added_at"): q.added_at = datetime.utcnow()
    if hasattr(q, "kind"): q.kind = "PHARMACY"
    if hasattr(q, "description"):
        q.description = "Sent from patients list to pharmacy (invoice needed)"

    db.session.add(q)
    db.session.commit()
    flash(f"{p.first_name} {p.last_name} added to Pharmacy queue.", "success")
    return redirect(url_for("pharmacy.pharmacy_dashboard", queue_id=q.id))


@bp.get("/triage/queue")
@login_required
@roles_required("reception", "nurse", "admin")
def triage_queue():
    query = BillingQueue.query.filter_by(status="Open")
    if hasattr(BillingQueue, "kind"):
        query = query.filter(BillingQueue.kind == "TRIAGE")
    entries = query.order_by(BillingQueue.added_at.asc()).all()

    by_id = {}
    if entries:
        p_ids = list({e.patient_id for e in entries if getattr(e, "patient_id", None)})
        v_ids = list({e.visit_id for e in entries if getattr(e, "visit_id", None)})

        patients = Patient.query.filter(Patient.id.in_(p_ids)).all() if p_ids else []
        visits = Visit.query.filter(Visit.id.in_(v_ids)).all() if v_ids else []

        by_id["patients"] = {p.id: p for p in patients}
        by_id["visits"] = {v.id: v for v in visits}

    return render_template("triage_queue.html", entries=entries, by_id=by_id)
    
# --- add just below triage_queue() in app/routes/patients.py ---

@bp.route("/triage/<int:q_id>/start", methods=["GET", "POST"])
@login_required
@roles_required("nurse", "reception", "admin")
def triage_start(q_id):
    # 1) Load the queue entry and guard that it’s TRIAGE
    e = BillingQueue.query.get_or_404(q_id)
    if hasattr(e, "kind") and (e.kind or "").upper() != "TRIAGE":
        flash("This queue entry is not for TRIAGE.", "warning")
        return redirect(url_for("patients.triage_queue"))

    # 2) Ensure there is a Visit to attach vitals to
    v = Visit.query.get(e.visit_id) if getattr(e, "visit_id", None) else None
    if not v:
        v = _get_or_create_open_visit(e.patient_id)
        if hasattr(e, "visit_id"):
            e.visit_id = v.id

    # 3) Load patient
    p = Patient.query.get_or_404(e.patient_id)

    # 4) Bind/validate the VisitForm for vitals + notes
    form = VisitForm(obj=v)
    if form.validate_on_submit():
        # Save vitals
        v.weight_kg   = form.weight_kg.data
        v.height_cm   = form.height_cm.data
        v.temp_c      = form.temp_c.data
        v.pulse_bpm   = int(form.pulse_bpm.data) if form.pulse_bpm.data is not None else None
        v.bp_sys      = int(form.bp_sys.data) if form.bp_sys.data is not None else None
        v.bp_dia      = int(form.bp_dia.data) if form.bp_dia.data is not None else None
        v.spo2        = int(form.spo2.data) if form.spo2.data is not None else None
        v.notes       = (form.notes.data or "").strip()  # if your form has notes

        # Mark this triage entry done/closed
        if hasattr(e, "status"):
            e.status = "Closed"

        # Move patient to Doctor queue
        dq = BillingQueue()
        if hasattr(dq, "patient_id"): dq.patient_id = p.id
        if hasattr(dq, "visit_id"):   dq.visit_id   = v.id
        if hasattr(dq, "status"):     dq.status     = "Open"
        if hasattr(dq, "added_at"):   dq.added_at   = datetime.utcnow()
        if hasattr(dq, "kind"):       dq.kind       = "DOCTOR"
        if hasattr(dq, "description"):dq.description= "From Triage"
        db.session.add(dq)

        db.session.commit()
        flash("Triage saved. Patient sent to Doctor queue.", "success")
        return redirect(url_for("patients.doctors_queue"))

    # 5) GET: show the triage form page
    db.session.commit()  # persist any visit_id linkage
    return render_template("triage_form.html", p=p, form=form)
    

@bp.get("/doctors/queue")
@login_required
@roles_required("reception", "nurse", "doctor", "pediatrician", "admin")
def doctors_queue():
    query = BillingQueue.query.filter_by(status="Open")
    if hasattr(BillingQueue, "kind"):
        query = query.filter(BillingQueue.kind == "DOCTOR")
    entries = query.order_by(BillingQueue.added_at.asc()).all()

    by_id = {}
    if entries:
        p_ids = list({e.patient_id for e in entries if getattr(e, "patient_id", None)})
        v_ids = list({e.visit_id for e in entries if getattr(e, "visit_id", None)})

        patients = Patient.query.filter(Patient.id.in_(p_ids)).all() if p_ids else []
        visits = Visit.query.filter(Visit.id.in_(v_ids)).all() if v_ids else []

        by_id["patients"] = {p.id: p for p in patients}
        by_id["visits"] = {v.id: v for v in visits}

    return render_template("doctors_queue.html", entries=entries, by_id=by_id)
    

@bp.post("/triage/<int:q_id>/finish")
@login_required
@roles_required("nurse", "admin")
def triage_finish(q_id):
    """Close the triage entry and move the visit to the DOCTOR queue."""
    q = BillingQueue.query.get_or_404(q_id)

    if hasattr(q, "status"):
        q.status = "Closed"

    # Open a new Doctor queue entry
    next_q = BillingQueue()
    if hasattr(next_q, "patient_id"): next_q.patient_id = q.patient_id
    if hasattr(next_q, "visit_id"):   next_q.visit_id = q.visit_id
    if hasattr(next_q, "status"):     next_q.status = "Open"
    if hasattr(next_q, "added_at"):   next_q.added_at = datetime.utcnow()
    if hasattr(next_q, "kind"):       next_q.kind = "DOCTOR"
    if hasattr(next_q, "description"): next_q.description = "From Triage"

    db.session.add(next_q)
    db.session.commit()
    flash("Triage completed. Patient moved to Doctor queue.", "success")
    return redirect(url_for("patients.doctors_queue"))

@bp.post("/visits/<int:visit_id>/add_drugs")
@login_required
@roles_required("doctor", "pediatrician", "admin")
def visit_add_drugs_bulk(visit_id):
    v = Visit.query.get_or_404(visit_id)
    p = Patient.query.get_or_404(v.patient_id)
    inv = _get_or_create_invoice(p, v)

    # Expect arrays: drug_id[], drug_name[], dose[], qty[]
    drug_ids   = request.form.getlist("drug_id[]")
    drug_names = request.form.getlist("drug_name[]")
    doses      = request.form.getlist("dose[]")
    qtys       = request.form.getlist("qty[]")

    added_any = False
    for i in range(max(len(drug_ids), len(drug_names))):
        item_id   = int(drug_ids[i]) if i < len(drug_ids) and str(drug_ids[i]).isdigit() else None
        drug_name = (drug_names[i].strip() if i < len(drug_names) else "")
        dose      = (doses[i].strip() if i < len(doses) else "")
        qty       = int(qtys[i]) if i < len(qtys) and str(qtys[i]).isdigit() else 1

        if not item_id and not drug_name:
            continue

        # Create an invoice line if the model exists, else append to visit notes
        try:
            line = InvoiceLine() if InvoiceLine else None
            if line:
                if hasattr(line, "invoice_id"): line.invoice_id = inv.id
                if hasattr(line, "kind"):       line.kind = _normalized_kind(item_id=item_id)
                if hasattr(line, "item_id") and item_id: line.item_id = item_id
            
                desc = drug_name or "Drug"
                if dose: desc = f"{desc} ({dose})"
                if hasattr(line, "description"): line.description = desc
            
                if hasattr(line, "qty"): line.qty = qty
            
                unit_price = _drug_unit_price(p, item_id, drug_name)
                if hasattr(line, "unit_price"): line.unit_price = unit_price
                if hasattr(line, "line_total"): line.line_total = unit_price * Decimal(str(qty))
            
                db.session.add(line)   # ✅ THIS IS THE MISSING PIECE
            else:
                _append_tagged_notes(v, "Prescription", f"{drug_name} {dose} x{qty}")
            
            added_any = True
        except Exception:
            _append_tagged_notes(v, "Prescription", f"{drug_name} {dose} x{qty}")

    if not added_any:
        flash("No valid drug rows provided.", "warning")
        return redirect(url_for("patients.patient_chart", patient_id=p.id, tab="visits"))

    # Recompute invoice from lines if present
    try:
        inv.amount = sum((l.line_total or Decimal("0")) for l in getattr(inv, "lines", []))
    except Exception:
        pass

    _enqueue_billing(p.id, v.id, note="Drugs added")

    db.session.commit()
    flash("Drugs added to visit.", "success")
    return redirect(url_for("patients.patient_chart", patient_id=p.id, tab="visits"))
    
