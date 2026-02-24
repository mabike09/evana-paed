# app/__init__.py
import logging
import os
from datetime import timedelta
from flask import Flask
from sqlalchemy import text
from .extensions import db, migrate, login_manager, csrf
from .utils import within_24h, has_endpoint
from config import Config


def create_app():
    BASE_DIR = os.path.dirname(__file__)        # .../evana-paed/app
    PROJECT_DIR = os.path.dirname(BASE_DIR)     # .../evana-paed

    app = Flask(
        __name__,
        instance_relative_config=True,
        # templates live in app/templates (default)
        static_folder=os.path.join(PROJECT_DIR, "static"),  # project-level static
        static_url_path="/static",
    )
    app.config.from_object(Config)

    # -------------------------
    # Logging (idempotent)
    # -------------------------
    log_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    def _has_evana_handler(logger):
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler) and getattr(h, "_evana_marker", False):
                return True
        return False

    if not _has_evana_handler(app.logger):
        fh = logging.FileHandler(os.path.join(log_dir, "app.log"))
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [pid=%(process)d]: %(message)s [in %(pathname)s:%(lineno)d]"
        ))
        # mark so we don't add it again if create_app() is called twice
        fh._evana_marker = True  # type: ignore[attr-defined]
        app.logger.addHandler(fh)

    app.logger.setLevel(logging.INFO)
    app.logger.info("Evana–Paed startup (no branches)")


    # -------------------------
    # Folders
    # -------------------------
    os.makedirs(app.instance_path, exist_ok=True)
    upload_dir = os.path.join(PROJECT_DIR, "uploads")
    app.config.setdefault("UPLOAD_FOLDER", upload_dir)
    os.makedirs(upload_dir, exist_ok=True)
    app.config.setdefault("MAX_CONTENT_LENGTH", 20 * 1024 * 1024)

    # -------------------------
    # Sessions / CSRF
    # -------------------------
    app.config.setdefault("WTF_CSRF_ENABLED", True)
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=7))
    app.config.setdefault("REMEMBER_COOKIE_DURATION", timedelta(days=7))
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")

    # -------------------------
    # Extensions
    # -------------------------
    db.init_app(app)
    migrate.init_app(app, db, directory="db_migrations")
    login_manager.init_app(app)
    csrf.init_app(app)

    # Flask-Login defaults + USER LOADER
    from .models import User  # import after db.init_app
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # -------------------------
    # Jinja globals
    # -------------------------
    app.jinja_env.globals.update(
        within_24h=within_24h,
        has_endpoint=has_endpoint,
        enumerate=enumerate,
        zip=zip,
        len=len,
    )

    # -------------------------
    # Blueprints
    # -------------------------
    from .routes import auth, home, patients, queue, billing, lab, files, inventory, reports, prices, pharmacy
    app.register_blueprint(home.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(patients.bp)
    app.register_blueprint(queue.bp)
    app.register_blueprint(billing.bp)
    app.register_blueprint(lab.bp)
    app.register_blueprint(files.bp)
    app.register_blueprint(inventory.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(prices.bp)
    app.register_blueprint(pharmacy.bp)

    # -------------------------
    # Invoice editability helper
    # -------------------------
    from .utils import invoice_editable_now
    app.config.setdefault("INVOICE_EDIT_WINDOW_HOURS", 24)
    app.jinja_env.globals.update(invoice_editable_now=invoice_editable_now)

    # -------------------------
    # Normalize legacy invoice payer enum values once per process
    # -------------------------
    @app.before_request
    def _normalize_invoice_payer_values_once():
        from flask import current_app
        if getattr(app, "_invoice_payer_normalized", False):
            return
        try:
            fixed_insurance = db.session.execute(
                text("UPDATE invoice SET payer_type = 'Insurance' WHERE lower(payer_type) = 'insurance'")
            ).rowcount or 0
            fixed_cash = db.session.execute(
                text("UPDATE invoice SET payer_type = 'Cash' WHERE lower(payer_type) = 'cash'")
            ).rowcount or 0
            if fixed_insurance or fixed_cash:
                db.session.commit()
                current_app.logger.info(
                    f"Normalized invoice payer_type values (Insurance={fixed_insurance}, Cash={fixed_cash})"
                )
            else:
                db.session.rollback()
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Invoice payer_type normalization skipped: {e}")
        app._invoice_payer_normalized = True  # type: ignore[attr-defined]


    def _ensure_billing_queue_closed_at_once():
        from flask import current_app
        if getattr(app, "_billing_queue_closed_at_checked", False):
            return
        try:
            dialect = db.engine.dialect.name
            if dialect == "sqlite":
                cols = db.session.execute(text("PRAGMA table_info(billing_queue)")).fetchall()
                col_names = {str(row[1]).lower() for row in cols}
                if "closed_at" not in col_names:
                    db.session.execute(text("ALTER TABLE billing_queue ADD COLUMN closed_at DATETIME"))
                    db.session.commit()
                    current_app.logger.info("Auto-added missing billing_queue.closed_at column")
                else:
                    db.session.rollback()
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_billing_queue_closed_at ON billing_queue(closed_at)"))
                db.session.commit()
            app._billing_queue_closed_at_checked = True  # type: ignore[attr-defined]
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"billing_queue.closed_at schema check skipped: {e}")

    with app.app_context():
        _ensure_billing_queue_closed_at_once()

    @app.before_request
    def _ensure_billing_queue_closed_at_before_request():
        _ensure_billing_queue_closed_at_once()

    # -------------------------
    # Seed insurers once per process
    # -------------------------
    @app.before_request
    def _seed_insurers_once():
        from .models import Insurer
        from flask import current_app
        if getattr(app, "_insurers_seeded", False):
            return
        try:
            want = ["AAR", "APA", "GA", "Prudential", "Sanlam", "ICEA", "Case"]
            existing = {i.name for i in Insurer.query.all()}
            missing = [n for n in want if n not in existing]
            for name in missing:
                db.session.add(Insurer(name=name, active=True))
            if missing:
                db.session.commit()
                current_app.logger.info(f"Seeded insurers: {', '.join(missing)}")
        except Exception as e:
            app.logger.warning(f"Insurer seed skipped: {e}")
        app._insurers_seeded = True  # type: ignore[attr-defined]

    return app

