# app/routes/home.py
from flask import Blueprint, redirect, url_for
from flask_login import current_user

bp = Blueprint("home", __name__)

@bp.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.role in ("doctor", "pediatrician"):
            return redirect(url_for("queue.clinician_queue"))
        return redirect(url_for("patients.patients_list"))
    return redirect(url_for("auth.login"))

@bp.route("/health")
def health(): return ("OK", 200)
