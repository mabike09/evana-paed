# app/routes/files.py
import os
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, send_from_directory, abort
from flask_login import login_required, current_user
from flask_wtf.file import FileField, FileAllowed, FileRequired
from wtforms import SelectField, SubmitField
from wtforms.validators import Optional
from flask_wtf import FlaskForm
from ..extensions import db
from ..models import FileAsset, Patient, Visit
from ..utils import *

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf"}

class FileUploadForm(FlaskForm):
    kind = SelectField("Kind", choices=[("pdf","PDF"),("photo","Photo"),("lab","Lab"),("other","Other")])
    visit_id = SelectField("Link to Visit (optional)", choices=[], validators=[Optional()])
    file = FileField("File", validators=[FileRequired(), FileAllowed(list(ALLOWED_EXTENSIONS), "Allowed: png, jpg, jpeg, gif, pdf")])
    submit = SubmitField("Upload")

bp = Blueprint("files", __name__)

@bp.route("/patients/<int:patient_id>/files", methods=["GET", "POST"])
@login_required
def patient_files(patient_id):
    from flask import current_app
    p = Patient.query.get_or_404(patient_id)
    form = FileUploadForm()
    visits = Visit.query.filter_by(patient_id=patient_id).order_by(Visit.visit_date.desc()).all()
    form.visit_id.choices = [("", "— No Visit —")] + [(str(v.id), f"{v.visit_date} • {v.reason or 'Visit'}") for v in visits]
    if form.validate_on_submit():
        f = form.file.data
        if f and "." in f.filename and f.filename.rsplit(".",1)[1].lower() in ALLOWED_EXTENSIONS:
            raw_name = f.filename
            folder = current_app.config["UPLOAD_FOLDER"] + f"/patient_{patient_id}"
            os.makedirs(folder, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
            stored_name = f"{ts}__{raw_name}"
            stored_path = os.path.join(folder, stored_name)
            f.save(stored_path)
            link_visit_id = int(form.visit_id.data) if form.visit_id.data else None
            fa = FileAsset(patient_id=patient_id, visit_id=link_visit_id, kind=form.kind.data,
                           filename=raw_name, mime=f.mimetype or "", size=os.path.getsize(stored_path),
                           stored_path=stored_path, uploaded_by=current_user.id if current_user.is_authenticated else None)
            db.session.add(fa); db.session.commit()
            flash("File uploaded.", "success")
            return redirect(url_for("files.patient_files", patient_id=patient_id))
        else:
            flash("Unsupported file type.", "danger")
    files = FileAsset.query.filter_by(patient_id=patient_id).order_by(FileAsset.uploaded_at.desc()).all()
    return render_template("patient_files.html", p=p, form=form, files=files)

@bp.route("/files/<int:file_id>/download")
@login_required
def file_download(file_id):
    fa = FileAsset.query.get_or_404(file_id)
    directory, fname = os.path.dirname(fa.stored_path), os.path.basename(fa.stored_path)
    if not os.path.exists(fa.stored_path): abort(404)
    return send_from_directory(directory, fname, as_attachment=True, download_name=fa.filename)

@bp.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
def file_delete(file_id):
    fa = FileAsset.query.get_or_404(file_id)
    pid = fa.patient_id
    try:
        if os.path.exists(fa.stored_path): os.remove(fa.stored_path)
        from ..extensions import db
        db.session.delete(fa); db.session.commit()
        flash("File deleted.", "success")
    except Exception:
        db.session.rollback(); flash("Could not delete file.", "danger")
    return redirect(url_for("files.patient_files", patient_id=pid))

@bp.route("/files/<int:file_id>/view")
@login_required
def file_view(file_id):
    fa = FileAsset.query.get_or_404(file_id)
    if not os.path.exists(fa.stored_path): abort(404)
    directory, fname = os.path.dirname(fa.stored_path), os.path.basename(fa.stored_path)
    return send_from_directory(directory, fname, as_attachment=False, download_name=fa.filename)
