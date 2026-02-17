# app/routes/inventory.py
from decimal import Decimal
from collections import defaultdict
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func, or_, and_
from ..permissions import roles_required
from ..extensions import db
from ..forms import StockTxnForm, ItemPriceForm, VisitConsumeForm, BulkStockTxnForm
from ..models import (
    Item, ItemTxn, DispenseTxn, Visit, Invoice, InvoiceLine, Patient,
    PriceBook, PriceItem, Payer
)

bp = Blueprint("inventory", __name__)

def normalize_payer_name(name: str) -> str:
    """Normalize UI/query payer names so they match DB values more reliably."""
    if not name:
        return "Cash"
    n = (name or "").strip().lower().replace("insurance", "").strip()
    mapping = {
        "aar": "AAR",
        "apa": "APA",
        "ga": "GA",
        "icea": "ICEA",
        "prudential": "Prudential",
        "sanlam": "Sanlam",
        "medicard": "Medicard",
        "case": "Case",
        "cash": "Cash",
    }
    return mapping.get(n, name.strip())


# ---------------------------
# Items (create/edit + quick price save)
# ---------------------------
@bp.route("/inventory/items", methods=["GET"])
@login_required
@roles_required("admin", "nurse")
def items_prices():
    """Back-compat endpoint (kept). Now shows Inventory/PriceBook/Merged via inventory_stock.html."""
    # Delegate to the main inventory page, default to inventory mode
    return redirect(url_for("inventory.inventory_stock", mode=request.args.get("mode") or "inventory"))

@bp.route("/inventory/items/save", methods=["POST"])
@login_required
@roles_required("admin")
def items_prices_save():
    items = Item.query.all()
    changed = 0
    for it in items:
        field = f"price_{it.id}"
        if field in request.form:
            try:
                new_price = Decimal(request.form.get(field) or "0")
            except Exception:
                new_price = None
            if new_price is not None and (it.sell_price or Decimal("0")) != new_price:
                it.sell_price = new_price
                changed += 1
    db.session.commit()
    flash(f"Item prices saved. {changed} change(s).", "success")
    return redirect(url_for("inventory.inventory_stock"))

@bp.route("/inventory/items/new", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def items_new():
    form = ItemPriceForm()
    if form.validate_on_submit():
        it = Item(
            name=form.name.data.strip(),
            sku=(form.sku.data or "").strip() or None,
            unit=(form.unit.data or "unit").strip(),
            min_level=0,
            current_qty=0,
            is_drug=bool(form.is_drug.data),
            sell_price=form.sell_price.data or 0
        )
        db.session.add(it)
        db.session.commit()
        flash("Item created.", "success")
        return redirect(url_for("inventory.inventory_stock", mode="inventory"))
    elif request.method == "POST":
        flash("Please correct the errors below.", "danger")
    return render_template("items_new.html", form=form)

@bp.route("/inventory/items/<int:item_id>/edit", methods=["GET","POST"])
@login_required
@roles_required("admin")
def items_edit(item_id):
    it = Item.query.get_or_404(item_id)
    form = ItemPriceForm(obj=it)
    if form.validate_on_submit():
        it.name = form.name.data.strip()
        it.sku = (form.sku.data or "").strip() or None
        it.unit = (form.unit.data or "unit").strip()
        it.is_drug = bool(form.is_drug.data)
        it.sell_price = form.sell_price.data or 0
        db.session.commit()
        flash("Item updated.", "success")
        return redirect(url_for("inventory.inventory_stock", mode="inventory"))
    # ensure initial values
    form.name.data = it.name
    form.sku.data = it.sku
    form.unit.data = it.unit or "unit"
    form.sell_price.data = it.sell_price or 0
    form.is_drug.data = it.is_drug
    return render_template("items_new.html", form=form)

# ---------------------------
# Inventory / Price Books / Merged
# ---------------------------
def _list_price_books(limit=50):
    q = (
        db.session.query(PriceBook, Payer.name.label("payer_name"))
        .join(Payer, PriceBook.payer_id == Payer.id)
        .order_by(PriceBook.effective_date.desc().nullslast(), PriceBook.id.desc())
        .limit(limit)
    )
    return q.all()

@bp.route("/inventory/stock")
@login_required
@roles_required("admin", "nurse")
def inventory_stock():
    """
    mode:
      - inventory : show Item table
      - pricebook : show PriceItem rows from selected/last book
      - merged    : Item LEFT JOIN selected PriceBook by SKU/Name
    query params: mode, payer, book_id, q
    """
    mode = (request.args.get("mode") or "inventory").strip().lower()
    payer_name_raw = (request.args.get("payer") or "Cash").strip()
    payer_name = normalize_payer_name(payer_name_raw)
    book_id = request.args.get("book_id", type=int)
    q = (request.args.get("q") or "").strip()

    # Resolve book for pricebook/merged modes
    book = None
    if mode in ("pricebook", "merged"):
        if book_id:
            # If a specific book id is given, prefer it
            book = (
                db.session.query(PriceBook)
                .filter(PriceBook.id == book_id)
                .first()
            )
            if book:
                # Refresh payer_name from DB to keep the UI consistent
                payer_row = db.session.query(Payer).filter(Payer.id == book.payer_id).first()
                if payer_row:
                    payer_name = payer_row.name
        if not book:
            # Fall back to "latest book for (case-insensitive, trimmed) payer name"
            qb = (
                db.session.query(PriceBook)
                .join(Payer, PriceBook.payer_id == Payer.id)
                .filter(func.lower(func.trim(Payer.name)) == payer_name.lower())
                .order_by(PriceBook.effective_date.desc().nullslast(), PriceBook.id.desc())
            )
            book = qb.first()
            if not book:
                flash(f"No price book found for payer '{payer_name}'.", "warning")

    # Build data
    items = []
    rows_price = []
    rows_merged = []

    if mode == "inventory" or not book:
        query = Item.query
        if q:
            like = f"%{q}%"
            query = query.filter(or_(Item.name.ilike(like), Item.sku.ilike(like)))
        items = query.order_by(Item.name.asc()).all()

    if mode == "pricebook" and book:
        piq = PriceItem.query.filter(PriceItem.pricebook_id == book.id)
        if q:
            like = f"%{q}%"
            piq = piq.filter(or_(PriceItem.item_name.ilike(like), PriceItem.item_code.ilike(like)))
        rows_price = piq.order_by(PriceItem.item_type.asc(), PriceItem.item_name.asc()).all()

    if mode == "merged" and book:
        pi = db.aliased(PriceItem)
        sub = db.session.query(pi).filter(pi.pricebook_id == book.id).subquery()
        query = (
            db.session.query(
                Item.id.label("item_id"),
                Item.name.label("item_name"),
                Item.sku.label("item_sku"),
                Item.unit.label("item_unit"),
                Item.current_qty.label("qty"),
                Item.sell_price.label("inv_sell"),
                Item.buying_price.label("inv_buy"),
                sub.c.item_type.label("pb_type"),
                sub.c.sell_price.label("pb_sell"),
                sub.c.buy_price.label("pb_buy"),
                sub.c.unit.label("pb_unit"),
                sub.c.item_code.label("pb_code"),
                sub.c.item_name.label("pb_name"),
            )
            .outerjoin(
                sub,
                or_(
                    and_(sub.c.item_code.isnot(None), sub.c.item_code == Item.sku),
                    func.lower(sub.c.item_name) == func.lower(Item.name),
                ),
            )
        )
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(Item.name.ilike(like), Item.sku.ilike(like), sub.c.item_name.ilike(like))
            )
        rows_merged = query.order_by(Item.name.asc()).all()

    low = [it for it in items if (it.min_level or 0) >= (it.current_qty or 0)]
    return render_template(
        "inventory_stock.html",
        mode=mode,
        payer_name=payer_name,
        book=book,
        books=_list_price_books(),
        items=items,
        rows_price=rows_price,
        rows_merged=rows_merged,
        low_count=len(low),
        q=q
    )


# ---------------------------
# Stock transaction
# ---------------------------
@bp.route("/inventory/stock/txn", methods=["GET", "POST"])
@login_required
@roles_required("admin", "nurse")
def inventory_txn():
    form = StockTxnForm()
    items = Item.query.order_by(Item.name.asc()).all()
    form.item_id.choices = [(it.id, f"{it.name} ({it.unit or ''}) • In stock: {it.current_qty}") for it in items]
    if form.validate_on_submit():
        it = Item.query.get_or_404(form.item_id.data)
        try:
            qty_change_int = int(Decimal(str(form.qty.data)))
        except Exception:
            flash("Quantity must be a whole number.", "danger")
            return redirect(url_for("inventory.inventory_txn"))
        it.current_qty = int(it.current_qty or 0) + qty_change_int
        db.session.add(
            ItemTxn(
                item_id=it.id,
                qty_change=qty_change_int,
                reason=form.reason.data,
                note=(form.note.data or "").strip(),
            )
        )
        db.session.commit()
        flash(f"Stock updated for {it.name}. New qty: {it.current_qty}", "success")
        return redirect(url_for("inventory.inventory_stock"))
    elif request.method == "POST":
        flash("Please correct the errors below.", "danger")
    return render_template("inventory_txn.html", form=form)

@bp.route("/inventory/stock/txn/bulk", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def inventory_bulk_txn():
    form = BulkStockTxnForm()
    items = Item.query.filter_by(is_drug=True).order_by(Item.name.asc()).all()

    if form.validate_on_submit():
        changes = []
        for it in items:
            raw_qty = (request.form.get(f"qty_{it.id}") or "").strip()
            if not raw_qty:
                continue
            try:
                qty_change_int = int(Decimal(raw_qty))
            except Exception:
                flash(f"{it.name}: quantity must be a whole number.", "danger")
                return redirect(url_for("inventory.inventory_bulk_txn"))
            if qty_change_int == 0:
                continue
            changes.append((it, qty_change_int))

        if not changes:
            flash("No quantity changes entered.", "warning")
            return redirect(url_for("inventory.inventory_bulk_txn"))

        note = (form.note.data or "").strip()
        for it, qty_change_int in changes:
            it.current_qty = int(it.current_qty or 0) + qty_change_int
            db.session.add(
                ItemTxn(
                    item_id=it.id,
                    qty_change=qty_change_int,
                    reason=form.reason.data,
                    note=note,
                    user_id=current_user.id,
                )
            )

        db.session.commit()
        flash(f"Bulk stock transaction recorded for {len(changes)} drug(s).", "success")
        return redirect(url_for("inventory.inventory_stock", mode="inventory"))
    elif request.method == "POST":
        flash("Please correct the errors below.", "danger")

    return render_template("inventory_bulk_txn.html", form=form, items=items)

# ---------------------------
# Dispense (as you had)
# ---------------------------
@bp.route("/patients/<int:patient_id>/visits/<int:visit_id>/dispense", methods=["GET", "POST"])
@login_required
@roles_required("admin", "nurse")
def visit_dispense(patient_id, visit_id):
    p = Patient.query.get_or_404(patient_id)
    v = Visit.query.filter_by(id=visit_id, patient_id=patient_id).first_or_404()
    inv = Invoice.query.filter_by(visit_id=visit_id, patient_id=patient_id).first()
    rows = []
    if inv:
        disp_by_line = defaultdict(Decimal)
        disp_by_item = defaultdict(Decimal)
        for d in DispenseTxn.query.filter_by(visit_id=visit_id).all():
            if d.invoice_line_id:
                disp_by_line[d.invoice_line_id] += Decimal(d.qty or 0)
            elif d.item_id:
                disp_by_item[d.item_id] += Decimal(d.qty or 0)
        for ln in inv.lines:
            if not (ln.kind == "drug" or ln.item_id):
                continue
            item = Item.query.get(ln.item_id) if ln.item_id else None
            stock = item.current_qty if item else 0
            prescribed = Decimal(ln.qty or 0)
            dispensed = disp_by_line.get(ln.id, Decimal("0"))
            if dispensed == 0 and ln.item_id:
                dispensed = disp_by_item.get(ln.item_id, Decimal("0"))
            remaining = max(Decimal("0"), prescribed - dispensed)
            rows.append(
                {
                    "line_id": ln.id,
                    "item": item,
                    "item_id": ln.item_id,
                    "desc": ln.description,
                    "unit_price": Decimal(ln.unit_price or 0),
                    "prescribed_qty": prescribed,
                    "dispensed_qty": dispensed,
                    "remaining_qty": remaining,
                    "stock": stock,
                }
            )
    if request.method == "POST":
        line_ids = request.form.getlist("line_id[]")
        item_ids = request.form.getlist("item_id[]")
        qtys = request.form.getlist("qty[]")
        if not line_ids:
            flash("No items to dispense.", "warning")
            return redirect(url_for("inventory.visit_dispense", patient_id=patient_id, visit_id=visit_id))
        remaining_map = {str(r["line_id"]): r["remaining_qty"] for r in rows}
        dispensed_any, errors = False, []
        for lid, iid, qraw in zip(line_ids, item_ids, qtys):
            try:
                q = Decimal((qraw or "0")).quantize(Decimal("1"))
            except Exception:
                q = Decimal("0")
            if q <= 0:
                continue
            remaining = remaining_map.get(lid, Decimal("0"))
            if q > remaining:
                errors.append(f"Line #{lid}: quantity {q} exceeds remaining {remaining}.")
                continue
            it = Item.query.get(int(iid)) if iid and iid.isdigit() else None
            if not it:
                errors.append(f"Line #{lid}: item not found.")
                continue
            current_stock = int(it.current_qty or 0)
            if q > current_stock:
                errors.append(f"{it.name}: only {current_stock} in stock, cannot dispense {q}.")
                continue
            it.current_qty = current_stock - int(q)
            db.session.add(
                DispenseTxn(
                    item_id=it.id,
                    patient_id=patient_id,
                    visit_id=v.id,
                    invoice_line_id=int(lid) if lid and lid.isdigit() else None,
                    qty=q,
                    unit_price=Decimal(it.sell_price or 0),
                    line_total=Decimal(it.sell_price or 0) * q,
                )
            )
            db.session.add(
                ItemTxn(
                    item_id=it.id,
                    qty_change=int(-q),
                    reason="Consume-Visit",
                    visit_id=v.id,
                    note=f"Dispensed for visit {v.id}",
                )
            )
            dispensed_any = True
        if errors:
            for e in errors:
                flash(e, "warning")
        if dispensed_any:
            db.session.commit()
            flash("Dispense recorded and inventory updated.", "success")
            return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="billing"))
        return redirect(url_for("inventory.visit_dispense", patient_id=patient_id, visit_id=visit_id))
    rows_to_show = [r for r in rows if r["remaining_qty"] > 0]
    return render_template("visit_dispense.html", p=p, v=v, rows=rows_to_show)
