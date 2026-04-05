from datetime import datetime

import requests
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Patient, SmsDispatchLog, SmsTemplate
from ..permissions import roles_required

bp = Blueprint("sms", __name__)


DEFAULT_SPEEDA_BASE_URL = "http://apidocs.speedamobile.com/api/SendSMS"
DEFAULT_SPEEDA_API_ID = "API29324194311"
DEFAULT_SPEEDA_API_PASSWORD = "Playtime@13pm"
DEFAULT_SPEEDA_SENDER_ID = "BULKSMS"


def _provider_settings():
    return {
        "base_url": current_app.config.get("SPEEDA_BASE_URL", DEFAULT_SPEEDA_BASE_URL),
        "api_id": current_app.config.get("SPEEDA_API_ID", DEFAULT_SPEEDA_API_ID),
        "api_password": current_app.config.get("SPEEDA_API_PASSWORD", DEFAULT_SPEEDA_API_PASSWORD),
        "sender_id": current_app.config.get("SPEEDA_SENDER_ID", DEFAULT_SPEEDA_SENDER_ID),
    }


def _send_sms(phone_number: str, message: str):
    cfg = _provider_settings()
    payload = {
        "api_id": cfg["api_id"],
        "api_password": cfg["api_password"],
        "sender_id": cfg["sender_id"],
        "phone": phone_number,
        "message": message,
    }

    # Provider docs use URL endpoint only; try form POST first and then GET query fallback.
    post_resp = requests.post(cfg["base_url"], data=payload, timeout=20)
    if post_resp.ok:
        return True, post_resp.text

    get_resp = requests.get(cfg["base_url"], params=payload, timeout=20)
    return get_resp.ok, get_resp.text


def _parse_manual_recipients(raw_value: str):
    recipients = [p.strip() for p in raw_value.replace("\n", ",").split(",") if p.strip()]
    return list(dict.fromkeys(recipients))


def _all_contact_phones():
    rows = (
        db.session.query(Patient.phone)
        .filter(Patient.phone.isnot(None))
        .filter(Patient.phone != "")
        .all()
    )
    recipients = [row[0].strip() for row in rows if row and row[0] and row[0].strip()]
    return list(dict.fromkeys(recipients))


@bp.route("/admin/sms", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def sms_manager():
    action = (request.form.get("action") or "").strip()

    if request.method == "POST" and action == "create_template":
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "").strip().lower()
        body = (request.form.get("body") or "").strip()

        if not name or not body:
            flash("Template name and message body are required.", "danger")
            return redirect(url_for("sms.sms_manager"))

        if category not in {"seasonal", "promotional"}:
            flash("Invalid template category.", "danger")
            return redirect(url_for("sms.sms_manager"))

        db.session.add(
            SmsTemplate(
                name=name,
                category=category,
                body=body,
                created_by=current_user.username,
            )
        )
        db.session.commit()
        flash("SMS template created.", "success")
        return redirect(url_for("sms.sms_manager"))

    if request.method == "POST" and action == "send_sms":
        template_id = (request.form.get("template_id") or "").strip()
        campaign_type = (request.form.get("campaign_type") or "").strip().lower()
        recipients_raw = (request.form.get("recipients") or "").strip()
        custom_message = (request.form.get("custom_message") or "").strip()
        recipient_mode = (request.form.get("recipient_mode") or "manual").strip().lower()

        template = SmsTemplate.query.get(int(template_id)) if template_id.isdigit() else None
        message = custom_message or (template.body if template else "")

        if campaign_type not in {"seasonal", "promotional"}:
            flash("Invalid campaign type.", "danger")
            return redirect(url_for("sms.sms_manager"))

        if not message:
            flash("Please provide a message or choose a template.", "danger")
            return redirect(url_for("sms.sms_manager"))

        if recipient_mode == "all_contacts":
            recipients = _all_contact_phones()
        else:
            recipients = _parse_manual_recipients(recipients_raw)

        if not recipients:
            if recipient_mode == "all_contacts":
                flash("No patient contacts found to send SMS to.", "danger")
            else:
                flash("Please provide at least one phone number.", "danger")
            return redirect(url_for("sms.sms_manager"))

        sent_count = 0
        failed_count = 0
        for phone_number in recipients:
            success = False
            provider_response = ""
            try:
                success, provider_response = _send_sms(phone_number=phone_number, message=message)
            except Exception as exc:
                provider_response = str(exc)

            db.session.add(
                SmsDispatchLog(
                    template_id=template.id if template else None,
                    campaign_type=campaign_type,
                    recipient_phone=phone_number,
                    message_body=message,
                    provider_response=provider_response[:1000],
                    status="sent" if success else "failed",
                    created_by=current_user.username,
                )
            )

            if success:
                sent_count += 1
            else:
                failed_count += 1

        db.session.commit()
        if failed_count:
            flash(f"Sent {sent_count} SMS, failed {failed_count}.", "warning")
        else:
            flash(f"Sent {sent_count} SMS successfully.", "success")

        return redirect(url_for("sms.sms_manager"))

    templates = SmsTemplate.query.order_by(SmsTemplate.created_at.desc()).all()
    logs = SmsDispatchLog.query.order_by(SmsDispatchLog.created_at.desc()).limit(25).all()

    return render_template(
        "admin_sms_manager.html",
        templates=templates,
        logs=logs,
        now=datetime.utcnow(),
    )
