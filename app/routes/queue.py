from flask import Blueprint, redirect, url_for, flash
from flask_login import login_required, current_user
from ..permissions import roles_required
from ..extensions import db
from ..models import ClinicianQueue, BillingQueue

bp = Blueprint("queue", __name__)

@bp.route("/queue")
@login_required
@roles_required("doctor", "pediatrician", "admin")
def clinician_queue():
    """Legacy endpoint: always send clinicians to the Doctor’s Queue."""
    return redirect(url_for("patients.doctors_queue"))

@bp.route("/queue/<int:queue_id>/seen", methods=["POST"])
@login_required
@roles_required("doctor", "pediatrician", "admin")
def mark_queue_seen(queue_id):
    q = ClinicianQueue.query.get_or_404(queue_id)
    q.status = "Seen"
    from datetime import datetime
    q.seen_at = datetime.utcnow()
    q.clinician_id = current_user.id

    db.session.commit()
    flash("Patient marked as seen.", "success")
    return redirect(url_for("patients.doctors_queue"))
