from flask import Blueprint, render_template, request
from app.permissions import roles_required

bp = Blueprint("pharmacy", __name__, url_prefix="/pharmacy")


@bp.route("/", methods=["GET"])
@roles_required("nurse")
def pharmacy_dashboard():
    patient_id = request.args.get("patient_id")
    return render_template("pharmacy.html", patient_id=patient_id)
