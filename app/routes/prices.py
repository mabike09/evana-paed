# app/routes/prices.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required
from sqlalchemy import inspect
from decimal import Decimal, InvalidOperation
import csv, io, json
from datetime import datetime, date

from ..permissions import roles_required
from ..extensions import db
from ..models import Payer, PriceBook, PriceItem

bp = Blueprint("prices", __name__, url_prefix="/prices")

DEFAULT_PAYERS = [
    {"name": "Cash", "payer_type": "cash"},
    {"name": "AAR", "payer_type": "insurance"},
    {"name": "APA", "payer_type": "insurance"},
    {"name": "CIC", "payer_type": "insurance"},
    {"name": "GA", "payer_type": "insurance"},
    {"name": "ICEA", "payer_type": "insurance"},
    {"name": "Prudential", "payer_type": "insurance"},
    {"name": "Sanlam", "payer_type": "insurance"},
    {"name": "Medicard", "payer_type": "insurance"},
    {"name": "Case", "payer_type": "insurance"},
]

def ensure_default_payers():
    """Idempotently seed our standard Cash + insurers."""
    for p in DEFAULT_PAYERS:
        _get_or_create(Payer, name=p["name"], defaults={"payer_type": p["payer_type"]})
    db.session.flush()
    _sync_cic_prices_from_cash()
    db.session.commit()


def _price_item_key(item):
    if getattr(item, "item_code", None):
        return ("code", item.item_code.strip().lower())
    return (
        "composite",
        (item.item_type or "").strip().lower(),
        (item.item_name or "").strip().lower(),
        (item.unit or "").strip().lower() if item.unit else "",
    )


def _sync_cic_prices_from_cash():
    """Keep CIC linked to the latest Cash price book values."""
    cash_payer = Payer.query.filter_by(name="Cash").first()
    cic_payer = Payer.query.filter_by(name="CIC").first()
    if not cash_payer or not cic_payer:
        return

    cash_book = (
        PriceBook.query
        .filter_by(payer_id=cash_payer.id)
        .order_by(PriceBook.effective_date.desc(), PriceBook.id.desc())
        .first()
    )
    if not cash_book:
        return

    cic_book_name = (cash_book.name or "Cash Price Book").replace("Cash", "CIC")
    if cic_book_name == cash_book.name:
        cic_book_name = f"CIC - {cash_book.name}"

    cic_book = (
        PriceBook.query
        .filter_by(payer_id=cic_payer.id, name=cic_book_name)
        .order_by(PriceBook.id.desc())
        .first()
    )
    if not cic_book:
        cic_book = PriceBook(
            name=cic_book_name,
            payer_id=cic_payer.id,
            effective_date=cash_book.effective_date,
            currency=cash_book.currency,
        )
        db.session.add(cic_book)
        db.session.flush()
    else:
        cic_book.effective_date = cash_book.effective_date
        cic_book.currency = cash_book.currency

    existing = {
        _price_item_key(item): item
        for item in PriceItem.query.filter_by(pricebook_id=cic_book.id).all()
    }
    for cash_item in PriceItem.query.filter_by(pricebook_id=cash_book.id).all():
        key = _price_item_key(cash_item)
        cic_item = existing.get(key)
        if cic_item is None:
            cic_item = PriceItem(
                pricebook_id=cic_book.id,
                item_type=cash_item.item_type,
                item_code=cash_item.item_code,
                item_name=cash_item.item_name,
                unit=cash_item.unit,
                category=cash_item.category,
                sell_price=cash_item.sell_price,
                buy_price=cash_item.buy_price,
            )
            db.session.add(cic_item)
            existing[key] = cic_item
        else:
            cic_item.item_type = cash_item.item_type
            cic_item.item_code = cash_item.item_code
            cic_item.item_name = cash_item.item_name
            cic_item.unit = cash_item.unit
            cic_item.category = cash_item.category
            cic_item.sell_price = cash_item.sell_price
            cic_item.buy_price = cash_item.buy_price

def normalize_payer_name(name: str) -> str:
    """
    Make payer names tolerant, e.g. 'ICEA Insurance' -> 'ICEA'
    '  case  ' -> 'Case'
    """
    if not name:
        return "Cash"
    n = name.strip().lower()
    n = n.replace("insurance", "").strip()
    # Title-case but preserve known acronyms
    mapping = {
        "aar": "AAR",
        "apa": "APA",
        "cic": "CIC",
        "ga": "GA",
        "icea": "ICEA",
        "prudential": "Prudential",
        "sanlam": "Sanlam",
        "medicard": "Medicard",
        "case": "Case",
        "cash": "Cash",
    }
    return mapping.get(n, name.strip())


def _to_decimal(val, default=None):
    if val in (None, "", "NA", "N/A"):
        return default
    try:
        s = str(val).replace(",", "").strip()
        return Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return default


def _get_or_create(model, defaults=None, **kwargs):
    inst = model.query.filter_by(**kwargs).first()
    if inst:
        return inst, False
    params = dict(kwargs)
    if defaults:
        params.update(defaults)
    inst = model(**params)
    db.session.add(inst)
    db.session.flush()
    return inst, True


@bp.get("/upload", endpoint="upload_price_form")
@login_required
@roles_required("admin")
def upload_price_form():
    ensure_default_payers()
    """
    Render the redesigned upload page with:
    - Payer dropdown (Cash + insurers)
    - Dependent PriceBook dropdown filtered by payer
    - Option to create a new PriceBook
    Also seeds the default payers if they don't exist yet.
    """
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(db.engine)

    # Guard: required tables
    required_tables = ["payer", getattr(PriceBook, "__tablename__", "price_book"),
                       getattr(PriceItem, "__tablename__", "price_item")]
    missing = [t for t in required_tables if not insp.has_table(t)]
    if missing:
        flash(
            "Price upload not ready: missing tables {}. Run DB migrations first."
            .format(", ".join(missing)),
            "warning",
        )

    # ---- Seed the required payers (idempotent) ----
    DEFAULT_PAYERS = [
        {"name": "Cash", "payer_type": "cash"},
        {"name": "AAR", "payer_type": "insurance"},
        {"name": "APA", "payer_type": "insurance"},
        {"name": "CIC", "payer_type": "insurance"},
        {"name": "GA", "payer_type": "insurance"},
        {"name": "ICEA", "payer_type": "insurance"},
        {"name": "Prudential", "payer_type": "insurance"},
        {"name": "Sanlam", "payer_type": "insurance"},
        {"name": "Medicard", "payer_type": "insurance"},
        {"name": "Case", "payer_type": "insurance"},
    ]
    for p in DEFAULT_PAYERS:
        _get_or_create(Payer, name=p["name"], defaults={"payer_type": p["payer_type"]})
    db.session.flush()

    # Build dropdown data
    payers = (
        Payer.query
        .filter(Payer.name.in_([p["name"] for p in DEFAULT_PAYERS]))
        .order_by(Payer.payer_type.asc(), Payer.name.asc())
        .all()
    )
    books = PriceBook.query.order_by(PriceBook.name.asc()).all()

    payers_json = [
        {"id": p.id, "name": p.name, "payer_type": getattr(p, "payer_type", "cash")}
        for p in payers
    ]
    books_json = [
        {"id": b.id, "name": b.name, "payer_id": b.payer_id}
        for b in books
    ]
    return render_template(
        "prices_upload.html",
        payers=payers,
        books=books,
        payers_json=json.dumps(payers_json),
        books_json=json.dumps(books_json),
    )

@bp.post("/upload", endpoint="upload_price_apply")
@login_required
@roles_required("admin")
def upload_price_apply():
    """
    Upload CSV into the selected (or newly created) pricebook.
    - Select payer via dropdown.
    - Choose existing pricebook OR create a new one (name, currency, effective_date).
    - Upsert items (no duplicates per pricebook).
    """
    # -------- Guard: required tables must exist --------
    insp = inspect(db.engine)
    missing = []
    if not insp.has_table("payer"):
        missing.append("payer")
    if not insp.has_table(getattr(PriceBook, "__tablename__", "price_book")):
        missing.append(getattr(PriceBook, "__tablename__", "price_book"))
    if not insp.has_table(getattr(PriceItem, "__tablename__", "price_item")):
        missing.append(getattr(PriceItem, "__tablename__", "price_item"))

    if missing:
        flash(
            f"Price upload not ready: missing tables {', '.join(missing)}. "
            "Run your DB migrations first.",
            "warning",
        )
        return redirect(url_for("prices.upload_price_form"))

    # -------- Read form selections --------
    # Either existing payer from dropdown, or fallback to CSV headers.
    payer_id = request.form.get("payer_id")  # optional (int as string)
    book_id = request.form.get("pricebook_id")  # optional
    new_book_name = (request.form.get("new_book_name") or "").strip()
    new_currency = (request.form.get("new_currency") or "").strip().upper()
    new_effective_raw = (request.form.get("new_effective_date") or "").strip()

    # -------- Validate file --------
    f = request.files.get("file")
    if not f:
        flash("Please choose a CSV file.", "warning")
        return redirect(url_for("prices.upload_price_form"))

    try:
        raw = f.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="ignore")
        rdr = csv.DictReader(io.StringIO(text))
        rows = list(rdr)
    except Exception:
        flash("CSV format not recognized. Please upload a valid CSV.", "danger")
        return redirect(url_for("prices.upload_price_form"))

    if not rows:
        flash("No rows found in CSV.", "warning")
        return redirect(url_for("prices.upload_price_form"))

    # -------- Always ensure payers exist (POST-safe) --------
    ensure_default_payers()

    # -------- Determine payer robustly --------
    payer = None
    if payer_id:
        payer = Payer.query.get(int(payer_id))
        if not payer:
            # Fallback to CSV 'payer' column if user selected an ID that doesn't exist in this DB
            first = rows[0]
            csv_payer_raw = (first.get("payer") or "").strip()
            csv_payer_name = normalize_payer_name(csv_payer_raw or "Cash")
            payer, _ = _get_or_create(
                Payer,
                name=csv_payer_name,
                defaults={"payer_type": "cash" if csv_payer_name == "Cash" else "insurance"},
            )
            db.session.flush()
    else:
        # No payer chosen in form: derive from CSV
        first = rows[0]
        csv_payer_raw = (first.get("payer") or "").strip()
        csv_payer_name = normalize_payer_name(csv_payer_raw or "Cash")
        payer_type_raw = (first.get("payer_type") or "").strip().lower()
        payer_type = payer_type_raw if payer_type_raw in ("cash", "insurance") else (
            "cash" if csv_payer_name == "Cash" else "insurance"
        )
        payer, _ = _get_or_create(Payer, name=csv_payer_name, defaults={"payer_type": payer_type})
        db.session.flush()

    if not payer:
        flash("Could not determine payer.", "danger")
        return redirect(url_for("prices.upload_price_form"))
    # -------- Determine payer --------
    payer = None
    if payer_id:
        payer = Payer.query.get(int(payer_id))
        if not payer:
            flash("Selected payer not found.", "danger")
            return redirect(url_for("prices.upload_price_form"))
    else:
        # Derive from CSV (fallback)
        first = rows[0]
        payer_name = (first.get("payer") or "").strip() or "Cash"
        payer_type_raw = (first.get("payer_type") or "").strip().lower()
        payer_type = payer_type_raw if payer_type_raw in ("cash", "insurance") else "cash"
        payer, _ = _get_or_create(Payer, name=payer_name, defaults={"payer_type": payer_type})

    # -------- Determine pricebook (existing or new) --------
    book = None
    if book_id:
        book = PriceBook.query.get(int(book_id))
        if not book:
            flash("Selected pricebook not found.", "danger")
            return redirect(url_for("prices.upload_price_form"))
        # Align payer if needed
        if getattr(book, "payer_id", None) != payer.id:
            book.payer_id = payer.id
            db.session.flush()
    else:
        # Create a new pricebook (from form or CSV)
        first = rows[0]
        # name
        book_name = new_book_name or (first.get("book_name") or "").strip()
        if not book_name:
            # fallback default
            eff_year = date.today().year
            book_name = f"{payer.name} {eff_year}"

        # currency
        currency = new_currency or (first.get("currency") or "").strip().upper() or "UGX"

        # effective date
        effective_date = None
        eff_raw = new_effective_raw or (first.get("effective_date") or "").strip()
        if eff_raw:
            try:
                effective_date = datetime.fromisoformat(eff_raw).date()
            except Exception:
                for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
                    try:
                        effective_date = datetime.strptime(eff_raw, fmt).date()
                        break
                    except Exception:
                        pass
        if not effective_date:
            effective_date = date.today()

        book, _ = _get_or_create(
            PriceBook,
            name=book_name,
            defaults={
                "payer_id": payer.id,
                "effective_date": effective_date,
                "currency": currency,
            },
        )
        # If the name existed already, align fields
        changed = False
        if getattr(book, "payer_id", None) != payer.id:
            book.payer_id = payer.id; changed = True
        if getattr(book, "currency", None) != currency:
            book.currency = currency; changed = True
        if getattr(book, "effective_date", None) is None:
            book.effective_date = effective_date; changed = True
        if changed:
            db.session.flush()

    # -------- Prepare existing items map for upsert (avoid duplicates) --------
    # Prefer unique key by item_code if present; else fallback to (item_type, item_name, unit)
    existing_items = {}
    for pi in PriceItem.query.filter_by(pricebook_id=book.id).all():
        key = None
        if getattr(pi, "item_code", None):
            key = ("code", pi.item_code.strip().lower())
        else:
            key = (
                "composite",
                (pi.item_type or "").strip().lower(),
                (pi.item_name or "").strip().lower(),
                (pi.unit or "").strip().lower() if pi.unit else "",
            )
        existing_items[key] = pi

    # Track CSV-level duplicates (same key appears multiple times)
    seen_in_csv = set()

    created_items = 0
    updated_items = 0
    skipped_rows = 0

    for r in rows:
        try:
            item_type = (r.get("item_type") or "").strip().lower()
            if item_type not in ("drug", "lab", "procedure"):
                item_type = "unknown"

            item_name = (r.get("item_name") or "").strip()
            if not item_name:
                skipped_rows += 1
                continue

            sell_price = _to_decimal(r.get("sell_price"))
            if sell_price is None:
                skipped_rows += 1
                continue

            buy_price = _to_decimal(r.get("buy_price"))
            item_code = (r.get("item_code") or "").strip() or None
            unit = (r.get("unit") or "").strip() or None
            category = (r.get("category") or "").strip() or None

            # Build key
            if item_code:
                key = ("code", item_code.lower())
            else:
                key = ("composite", item_type, item_name.lower(), (unit or "").lower())

            # Skip duplicate rows within the same CSV
            if key in seen_in_csv:
                continue
            seen_in_csv.add(key)

            if key in existing_items:
                # UPSERT: update existing record instead of creating a duplicate
                pi = existing_items[key]
                changed = False

                if pi.sell_price != sell_price:
                    pi.sell_price = sell_price; changed = True
                # Only set buy_price for drugs (optional rule, but safe)
                if buy_price is not None and getattr(pi, "buy_price", None) != buy_price:
                    pi.buy_price = buy_price; changed = True

                # Keep catalog data tidy if provided
                if category and getattr(pi, "category", None) != category:
                    pi.category = category; changed = True
                if unit and getattr(pi, "unit", None) != unit:
                    pi.unit = unit; changed = True
                if item_type and getattr(pi, "item_type", None) != item_type:
                    pi.item_type = item_type; changed = True
                if item_name and getattr(pi, "item_name", None) != item_name:
                    pi.item_name = item_name; changed = True

                if changed:
                    updated_items += 1
            else:
                # Create new
                pi = PriceItem(
                    pricebook_id=book.id,
                    item_type=item_type,
                    item_code=item_code,
                    item_name=item_name,
                    unit=unit,
                    category=category,
                    sell_price=sell_price,
                    buy_price=buy_price,
                )
                db.session.add(pi)
                existing_items[key] = pi
                created_items += 1

        except Exception:
            skipped_rows += 1
            continue

    if (payer.name or "").strip().lower() == "cash":
        _sync_cic_prices_from_cash()

    db.session.commit()
    flash(
        f"Uploaded to “{book.name}” ({payer.name}). "
        f"Created {created_items}, updated {updated_items}, skipped {skipped_rows}.",
        "success",
    )
    return redirect(url_for("prices.upload_price_form"))


@bp.get("/lab-cash/upload", endpoint="upload_lab_cash_form")
@login_required
@roles_required("admin")
def upload_lab_cash_form():
    """Admin-only: Upload Cash Laboratory pricelist from Excel (.xlsx)."""
    return render_template("lab_pricelist_upload.html")


@bp.post("/lab-cash/upload", endpoint="upload_lab_cash_apply")
@login_required
@roles_required("admin")
def upload_lab_cash_apply():
    """Import/update Cash Laboratory tests into PriceBook + PriceItem from Excel (.xlsx) or CSV.

    Expected columns:
      - LABORATORY TEST
      - PRICE
    """
    f = request.files.get("file")
    if not f:
        flash("Please choose a file (.xlsx or .csv).", "warning")
        return redirect(url_for("prices.upload_lab_cash_form"))

    try:
        import pandas as pd

        filename = (f.filename or "").lower()

        if filename.endswith(".csv"):
            df = pd.read_csv(f)
        elif filename.endswith(".xlsx"):
            df = pd.read_excel(f, engine="openpyxl")
        else:
            flash("Unsupported file type. Please upload .xlsx or .csv.", "danger")
            return redirect(url_for("prices.upload_lab_cash_form"))

    except Exception:
        # log the real error on server
        try:
            current_app.logger.exception("Lab pricelist upload failed")
        except Exception:
            pass
        flash("Could not read the file. Ensure it is a valid Excel (.xlsx) or CSV (.csv).", "danger")
        return redirect(url_for("prices.upload_lab_cash_form"))

    # Normalize column names
    df.columns = [str(c).strip().upper() for c in df.columns]

    if "LABORATORY TEST" not in df.columns or "PRICE" not in df.columns:
        flash("File must contain columns: 'LABORATORY TEST' and 'PRICE'.", "danger")
        return redirect(url_for("prices.upload_lab_cash_form"))

    # Ensure Cash and CIC payers exist
    payer, _ = _get_or_create(Payer, name="Cash", defaults={"payer_type": "cash"})
    _get_or_create(Payer, name="CIC", defaults={"payer_type": "insurance"})

    # Ensure a Cash laboratory pricebook exists (latest), else create
    book = (
        PriceBook.query
        .filter_by(payer_id=payer.id)
        .order_by(PriceBook.effective_date.desc(), PriceBook.id.desc())
        .first()
    )
    if not book:
        book = PriceBook(
            name="Cash - Laboratory",
            payer_id=payer.id,
            effective_date=date.today(),
            currency="UGX",
        )
        db.session.add(book)
        db.session.flush()

    created = 0
    updated = 0
    skipped = 0

    for _, row in df.iterrows():
        name = str(row.get("LABORATORY TEST") or "").strip()
        if not name:
            skipped += 1
            continue

        price_val = row.get("PRICE")
        price = _to_decimal(price_val, default=None)
        if price is None:
            skipped += 1
            continue

        item = (
            PriceItem.query
            .filter_by(pricebook_id=book.id, item_name=name)
            .first()
        )

        if not item:
            item = PriceItem(
                pricebook_id=book.id,
                item_type="lab",
                item_name=name,
                sell_price=price,
            )
            db.session.add(item)
            created += 1
        else:
            item.item_type = "lab"
            item.sell_price = price
            updated += 1

    _sync_cic_prices_from_cash()

    db.session.commit()
    flash(f"Lab cash pricelist imported. Created: {created}, Updated: {updated}, Skipped: {skipped}.", "success")
    return redirect(url_for("prices.upload_lab_cash_form"))
