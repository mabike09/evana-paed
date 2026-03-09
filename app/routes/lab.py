# app/routes/lab.py
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user

from ..permissions import roles_required
from ..extensions import db
from ..models import LabOrder, LabOrderLine, Procedure, Patient

bp = Blueprint("lab", __name__)

# Optional models to avoid hard crashes if they don't exist
try:
    from ..models import BillingQueue, Visit, User
except Exception:
    BillingQueue = None
    Visit = None
    User = None


# ---------------------------------------------------------------------
# Small helpers for print headers and "Run by"
# ---------------------------------------------------------------------
def _clinic_info():
    """Header/footer info with safe defaults (override via app config)."""
    cfg = current_app.config
    return {
        "name":  cfg.get("CLINIC_NAME", "Bambi Children’s Clinic"),
        "addr":  cfg.get("CLINIC_ADDRESS", "Ssenge, Wakiso District"),
        "phone": cfg.get("CLINIC_PHONE", "+256 000 000 000"),
        "email": cfg.get("CLINIC_EMAIL", "info@bambichildrensclinic.com"),
    }


def _performed_by_name(lines):
    """Best effort to resolve the lab technician who ran these tests."""
    tech_id = None
    for ln in lines:
        tech_id = getattr(ln, "performed_by", None)
        if tech_id:
            break

    if tech_id and User:
        try:
            u = User.query.get(int(tech_id))
            if u:
                return getattr(u, "full_name", None) or getattr(u, "username", None) or str(u.id)
        except Exception:
            pass

    # Fallback to current user (useful if printing right after entry)
    return getattr(current_user, "full_name", None) or getattr(current_user, "username", None) or "—"


# ---------------------------------------------------------------------
# Create a Lab Order (from patient chart or anywhere you POST to this)
# ---------------------------------------------------------------------
@bp.route("/patients/<int:patient_id>/lab/order", methods=["POST"])
@login_required
@roles_required("doctor", "pediatrician", "reception", "nurse", "admin")
def lab_order_create(patient_id):
    Patient.query.get_or_404(patient_id)

    visit_id = request.form.get("visit_id", type=int)  # may be None for direct
    proc_ids = request.form.getlist("lab_proc_id[]")
    names    = request.form.getlist("lab_name[]")

    # Nothing selected?
    if not proc_ids and not any((nm or "").strip() for nm in names):
        flash("No lab tests selected.", "warning")
        return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="lab"))

    # Create order header
    order = LabOrder(
        patient_id=patient_id,
        visit_id=visit_id,
        status="Pending",
        created_by=getattr(current_user, "id", None),
        created_at=datetime.utcnow() if hasattr(LabOrder, "created_at") else None,
    )
    db.session.add(order)
    db.session.flush()

    # Add procedure-backed tests
    for pid in proc_ids:
        pid = (pid or "").strip()
        if pid.isdigit():
            pr = Procedure.query.get(int(pid))
            if pr and (getattr(pr, "category", "").lower() == "lab"):
                db.session.add(LabOrderLine(
                    order_id=order.id,
                    procedure_id=pr.id,
                    test_name=pr.name
                ))

    # Add ad-hoc names
    for nm in names:
        nm = (nm or "").strip()
        if nm:
            db.session.add(LabOrderLine(
                order_id=order.id,
                procedure_id=None,
                test_name=nm
            ))

    db.session.commit()
    flash("Lab order created.", "success")
    return redirect(url_for("patients.patient_chart", patient_id=patient_id, tab="lab"))


# ---------------------------------------------------------------------
# Lab Queue
# Shows Pending LabOrders; if none exist, it materializes them from
# BillingQueue(kind="LAB") so the queue always has actionable orders.
# ---------------------------------------------------------------------
@bp.route("/lab/queue")
@login_required
@roles_required("labtech", "admin")
def lab_queue():
    # 1) Try to load real pending LabOrders
    try:
        pending = (LabOrder.query
                   .filter_by(status="Pending")
                   .order_by(LabOrder.created_at.asc())
                   .all())
    except Exception:
        pending = []

    created_from_queue = False

    # 2) If none, fallback to BillingQueue(kind="LAB") and create LabOrders
    if not pending and BillingQueue is not None:
        try:
            q = BillingQueue.query.filter_by(status="Open")
            if hasattr(BillingQueue, "kind"):
                q = q.filter(BillingQueue.kind == "LAB")
            lab_rows = q.order_by(BillingQueue.added_at.asc()).all()

            for row in lab_rows:
                # Don't duplicate if an order already exists for this visit and is pending
                existing = None
                try:
                    if row.visit_id:
                        existing = LabOrder.query.filter_by(
                            visit_id=row.visit_id, status="Pending"
                        ).first()
                    if not existing:
                        existing = LabOrder.query.filter_by(
                            patient_id=row.patient_id, status="Pending"
                        ).first()
                except Exception:
                    existing = None

                if existing:
                    continue

                lo = LabOrder(
                    patient_id=row.patient_id,
                    visit_id=getattr(row, "visit_id", None),
                    status="Pending",
                    created_by=getattr(current_user, "id", None),
                    created_at=datetime.utcnow() if hasattr(LabOrder, "created_at") else None,
                )
                db.session.add(lo)
                db.session.flush()

                # If queue description carries test names, store them as a single line
                desc = (getattr(row, "description", None) or "").strip()
                if desc:
                    db.session.add(LabOrderLine(
                        order_id=lo.id,
                        procedure_id=None,
                        test_name=desc
                    ))

                created_from_queue = True

            if created_from_queue:
                db.session.commit()
                pending = (LabOrder.query
                           .filter_by(status="Pending")
                           .order_by(LabOrder.created_at.asc())
                           .all())
        except Exception:
            # If anything goes wrong, at least render an empty list gracefully
            current_app.logger.exception("Failed to materialize LabOrders from BillingQueue")
            pending = pending or []

    # 3) Build quick lookup maps for template (patient/visit names etc.)
    by_id = {"patients": {}, "visits": {}}
    try:
        pids = list({o.patient_id for o in pending if getattr(o, "patient_id", None)})
        vids = list({o.visit_id for o in pending if getattr(o, "visit_id", None)})
        if pids:
            pts = Patient.query.filter(Patient.id.in_(pids)).all()
            by_id["patients"] = {p.id: p for p in pts}
        if vids and Visit is not None:
            vts = Visit.query.filter(Visit.id.in_(vids)).all()
            by_id["visits"] = {v.id: v for v in vts}
    except Exception:
        pass

    # 4) Collect test names per order (robust to either test_name or linked Procedure)
    tests_by_order_id = {}
    try:
        order_ids = [o.id for o in pending]
        if order_ids:
            # Load all lines for these orders in one go
            lines = LabOrderLine.query.filter(LabOrderLine.order_id.in_(order_ids)).all()
            # Optionally prefetch procedures for fewer round trips (if not eager-loaded)
            proc_ids = {ln.procedure_id for ln in lines if getattr(ln, "procedure_id", None)}
            procs = {}
            if proc_ids:
                procs = {p.id: p for p in Procedure.query.filter(Procedure.id.in_(proc_ids)).all()}

            from collections import defaultdict
            bucket = defaultdict(list)
            for ln in lines:
                name = (getattr(ln, "test_name", None) or "").strip()
                if not name:
                    pr = procs.get(getattr(ln, "procedure_id", None))
                    if pr and getattr(pr, "name", None):
                        name = pr.name.strip()
                if name:
                    bucket[ln.order_id].append(name)

            # de-duplicate while preserving order
            for oid, names in bucket.items():
                seen = set()
                out = []
                for n in names:
                    if n and n not in seen:
                        seen.add(n)
                        out.append(n)
                tests_by_order_id[oid] = out
    except Exception:
        current_app.logger.exception("Failed to build tests_by_order_id")

    return render_template(
        "lab_queue.html",
        orders=pending,
        by_id=by_id,
        tests_by_order_id=tests_by_order_id,  # << pass to template
    )


# ---------------------------------------------------------------------
# Enter Results / Complete (+ now with print_url on GET)
# ---------------------------------------------------------------------
@bp.route("/lab/orders/<int:order_id>/results", methods=["GET", "POST"])
@login_required
@roles_required("labtech", "admin")
def lab_enter_results(order_id):
    order = LabOrder.query.get_or_404(order_id)
    lines = LabOrderLine.query.filter_by(order_id=order.id).all()

    if request.method == "POST":
        vals     = request.form.getlist("result_value[]")
        txts     = request.form.getlist("result_text[]")
        line_ids = request.form.getlist("line_id[]")

        for lid, rv, rt in zip(line_ids, vals, txts):
            if not str(lid).isdigit():
                continue
            ln = LabOrderLine.query.get(int(lid))
            if not ln:
                continue
            ln.result_value = (rv or "").strip() or None
            ln.result_text  = (rt or "").strip() or None
            ln.status       = "Done"
            if hasattr(ln, "result_at"):
                ln.result_at = datetime.utcnow()
            if hasattr(ln, "performed_by"):
                ln.performed_by = getattr(current_user, "id", None)

        try:
            order.status = "Completed" if all((l.status == "Done") for l in lines) else "Pending"
        except Exception:
            pass

        db.session.commit()
        flash("Lab results saved successfully.", "success")

        # -----------------------------------------------------------------
        # If completed, clear LAB queue entries.
        #
        # Routing after lab completion:
        # - If this lab request came from the patient chart/doctor flow,
        #   move the patient back to DOCTOR queue.
        # - Otherwise (e.g., patient-list initiated requests), keep existing
        #   behavior and close the visit.
        # -----------------------------------------------------------------
        try:
            is_completed = (getattr(order, "status", None) == "Completed")
            if is_completed and BillingQueue is not None:
                q = BillingQueue.query.filter_by(status="Open")
                if hasattr(BillingQueue, "kind"):
                    q = q.filter(BillingQueue.kind == "LAB")

                if getattr(order, "visit_id", None):
                    q = q.filter(BillingQueue.visit_id == order.visit_id)
                else:
                    q = q.filter(BillingQueue.patient_id == order.patient_id)

                lab_rows = q.all()
                came_from_doctor = False
                for row in lab_rows:
                    desc = (getattr(row, "description", None) or "").strip().lower()
                    if "from doctor" in desc or "sent to lab from doctor" in desc:
                        came_from_doctor = True
                    row.status = "Closed"
                    if hasattr(row, "closed_at"):
                        row.closed_at = datetime.utcnow()

                patient = Patient.query.get(order.patient_id)
                insurance_provider = (getattr(patient, "insurance_provider", None) or "").strip().lower()
                is_cash_patient = (not insurance_provider) or (insurance_provider == "cash")
                should_return_to_doctor = came_from_doctor or is_cash_patient

                if Visit is not None and getattr(order, "visit_id", None):
                    v = Visit.query.get(order.visit_id)
                    if v:
                        if should_return_to_doctor:
                            # Re-open/move to doctor queue.
                            existing_doctor = (
                                BillingQueue.query.filter_by(status="Open")
                                .filter(BillingQueue.visit_id == order.visit_id)
                                .filter(BillingQueue.kind == "DOCTOR")
                                .first()
                            )
                            if not existing_doctor:
                                dq = BillingQueue()
                                if hasattr(dq, "patient_id"):
                                    dq.patient_id = order.patient_id
                                if hasattr(dq, "visit_id"):
                                    dq.visit_id = order.visit_id
                                if hasattr(dq, "status"):
                                    dq.status = "Open"
                                if hasattr(dq, "added_at"):
                                    dq.added_at = datetime.utcnow()
                                if hasattr(dq, "added_by"):
                                    dq.added_by = getattr(current_user, "id", None)
                                if hasattr(dq, "kind"):
                                    dq.kind = "DOCTOR"
                                if hasattr(dq, "description"):
                                    dq.description = "Returned from Lab"
                                db.session.add(dq)

                            if hasattr(v, "status"):
                                v.status = "Open"
                            if hasattr(v, "current_station"):
                                v.current_station = "DOCTOR"
                        else:
                            # Non-cash, non-doctor-origin lab flows keep the old close-out behavior.
                            if hasattr(v, "status"):
                                v.status = "Closed"
                            if hasattr(v, "closed_at"):
                                v.closed_at = datetime.utcnow()
                            if hasattr(v, "current_station"):
                                v.current_station = "CLOSED"

                db.session.commit()
        except Exception:
            current_app.logger.exception("Failed to close lab queue/visit after lab completion")

        # ✅ Stay on the same page instead of redirecting
        lines = LabOrderLine.query.filter_by(order_id=order.id).all()

    return render_template(
        "lab_enter_results.html",
        order=order,
        lines=lines,
        print_url=url_for("lab.lab_results_print", order_id=order.id),
    )


# ---------------------------------------------------------------------
# Printable Results (Doctor/Pediatrician/Admin/Labtech)
# ---------------------------------------------------------------------
@bp.route("/lab/orders/<int:order_id>/print", methods=["GET"])
@login_required
@roles_required("doctor", "pediatrician", "labtech", "admin")
def lab_results_print(order_id):
    order = LabOrder.query.get_or_404(order_id)
    lines = LabOrderLine.query.filter_by(order_id=order.id).all()

    patient = Patient.query.get(order.patient_id)
    visit = None
    if Visit is not None and getattr(order, "visit_id", None):
        try:
            visit = Visit.query.get(order.visit_id)
        except Exception:
            visit = None

    clinic = _clinic_info()
    tech_name = _performed_by_name(lines)

    # Determine “Completed” timestamp (latest result_at or order.created_at)
    completed_at = None
    try:
        ts = [getattr(ln, "result_at", None) for ln in lines if getattr(ln, "result_at", None)]
        completed_at = max(ts) if ts else getattr(order, "created_at", None)
    except Exception:
        completed_at = getattr(order, "created_at", None)
    
    age_years = None
    try:
        if getattr(patient, "dob", None):
            from datetime import date
            born = patient.dob if isinstance(patient.dob, (date,)) else None
        if born:
            today = date.today()
            age_years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    except Exception:
        pass    

    return render_template(
        "lab_results_print.html",
        clinic=clinic,
        order=order,
        lines=lines,
        patient=patient,
        visit=visit,
        tech_name=tech_name,
        completed_at=completed_at,
        age_years=age_years,
    )


# ---------------------------------------------------------------------
# Patient Lab History (Doctor/Pediatrician/Admin/Labtech)
# ---------------------------------------------------------------------
@bp.route("/patients/<int:patient_id>/lab/history", methods=["GET"])
@login_required
@roles_required("doctor", "pediatrician", "labtech", "admin")
def patient_lab_history(patient_id):
    patient = Patient.query.get_or_404(patient_id)

    # Pull orders + lines for this patient, newest first
    try:
        orders = (LabOrder.query
                  .filter_by(patient_id=patient_id)
                  .order_by(LabOrder.id.desc())
                  .all())
    except Exception:
        orders = []

    lines_by_order = {}
    try:
        ids = [o.id for o in orders]
        all_lines = LabOrderLine.query.filter(LabOrderLine.order_id.in_(ids)).all() if ids else []
        for ln in all_lines:
            lines_by_order.setdefault(ln.order_id, []).append(ln)
    except Exception:
        pass

    # Resolve a compact per-order summary (test list & short values)
    summary = []
    for o in orders:
        arr = []
        for ln in lines_by_order.get(o.id, []):
            name = (getattr(ln, "test_name", None) or "").strip()
            if not name and getattr(ln, "procedure_id", None):
                try:
                    pr = Procedure.query.get(ln.procedure_id)
                    if pr and getattr(pr, "name", None):
                        name = pr.name.strip()
                except Exception:
                    pass
            if name:
                val = getattr(ln, "result_value", None)
                txt = getattr(ln, "result_text", None)
                if val:
                    name = f"{name}: {val}"
                elif txt:
                    name = f"{name}: {txt[:50]}{'…' if len(txt) > 50 else ''}"
                arr.append(name)
        summary.append({"order": o, "tests": arr})

    clinic = _clinic_info()
    return render_template(
        "lab_results_history.html",
        clinic=clinic,
        patient=patient,
        visit=None,
        summary=summary
    )
