# app/__init__.py
import logging
import os
from datetime import timedelta
from flask import Flask
from sqlalchemy import inspect, text
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

    app.config.setdefault("SPEEDA_BASE_URL", os.getenv("SPEEDA_BASE_URL", "http://apidocs.speedamobile.com/api/SendSMS"))
    app.config.setdefault("SPEEDA_API_ID", os.getenv("SPEEDA_API_ID", "API29324194311"))
    app.config.setdefault("SPEEDA_API_PASSWORD", os.getenv("SPEEDA_API_PASSWORD", "Playtime@13pm"))
    app.config.setdefault("SPEEDA_SENDER_ID", os.getenv("SPEEDA_SENDER_ID", "BULKSMS"))

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
    from .routes import auth, home, patients, queue, billing, lab, files, inventory, reports, prices, pharmacy, finance, sms, claims, payroll
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
    app.register_blueprint(finance.bp)
    app.register_blueprint(sms.bp)
    app.register_blueprint(claims.bp)
    app.register_blueprint(payroll.bp)

    # -------------------------
    # Invoice editability helper
    # -------------------------
    from .utils import invoice_editable_by_user, invoice_editable_now
    app.config.setdefault("INVOICE_EDIT_WINDOW_HOURS", 24)
    app.jinja_env.globals.update(
        invoice_editable_by_user=invoice_editable_by_user,
        invoice_editable_now=invoice_editable_now,
    )

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


    @app.before_request
    def _ensure_claims_table_once():
        """Create the insurance claims table defensively when migrations lag behind."""
        if getattr(app, "_claims_table_checked", False):
            return
        try:
            insp = inspect(db.engine)
            if not insp.has_table("insurance_claim"):
                from .models import InsuranceClaim

                InsuranceClaim.__table__.create(bind=db.engine, checkfirst=True)
                app.logger.warning("Created missing insurance_claim table at runtime.")
        except Exception as e:
            app.logger.warning(f"Claims table check skipped: {e}")
        finally:
            app._claims_table_checked = True  # type: ignore[attr-defined]

    @app.before_request
    def _ensure_sms_tables_once():
        """Create SMS tables defensively if migrations have not yet been applied."""
        if getattr(app, "_sms_tables_checked", False):
            return

        try:
            insp = inspect(db.engine)
            if not insp.has_table("sms_template") or not insp.has_table("sms_dispatch_log"):
                from .models import SmsDispatchLog, SmsTemplate

                SmsTemplate.__table__.create(bind=db.engine, checkfirst=True)
                SmsDispatchLog.__table__.create(bind=db.engine, checkfirst=True)
                app.logger.warning("Created missing SMS tables at runtime (sms_template/sms_dispatch_log).")
        except Exception as e:
            app.logger.warning(f"SMS table check skipped: {e}")
        finally:
            app._sms_tables_checked = True  # type: ignore[attr-defined]


    # -------------------------
    # Backfill legacy SQLite schema drift once per process
    # -------------------------
    @app.before_request
    def _ensure_billing_queue_closed_at_once():
        """
        Some deployments have BillingQueue model code that expects `closed_at`
        before the migration has been applied. Add it defensively to avoid 500s.
        """
        if getattr(app, "_billing_queue_closed_at_checked", False):
            return

        try:
            insp = inspect(db.engine)
            if not insp.has_table("billing_queue"):
                return

            col_names = {c.get("name") for c in insp.get_columns("billing_queue")}
            if "closed_at" not in col_names:
                db.session.execute(text("ALTER TABLE billing_queue ADD COLUMN closed_at DATETIME"))
                db.session.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_billing_queue_closed_at ON billing_queue (closed_at)")
                )
                db.session.commit()
                app.logger.warning("Added missing billing_queue.closed_at column at runtime.")
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"billing_queue schema check skipped: {e}")
        finally:
            app._billing_queue_closed_at_checked = True  # type: ignore[attr-defined]

    @app.before_request
    def _ensure_user_is_active_once():
        """Backfill user.is_active defensively when migrations have not run yet."""
        if getattr(app, "_user_is_active_checked", False):
            return

        try:
            insp = inspect(db.engine)
            if not insp.has_table("user"):
                return

            col_names = {c.get("name") for c in insp.get_columns("user")}
            if "is_active" not in col_names:
                db.session.execute(text("ALTER TABLE user ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"))
                db.session.commit()
                app.logger.warning("Added missing user.is_active column at runtime.")
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"user.is_active schema check skipped: {e}")
        finally:
            app._user_is_active_checked = True  # type: ignore[attr-defined]

    @app.before_request
    def _ensure_expense_tracker_tables_once():
        """
        Create expense tracker tables defensively if migrations are not yet applied.
        Prevents 500s on /finance/expense-tracker in older deployments.
        """
        if getattr(app, "_expense_tracker_tables_checked", False):
            return

        try:
            insp = inspect(db.engine)
            if not insp.has_table("expense_category") or not insp.has_table("expense_entry"):
                from .models import ExpenseCategory, ExpenseEntry

                ExpenseCategory.__table__.create(bind=db.engine, checkfirst=True)
                ExpenseEntry.__table__.create(bind=db.engine, checkfirst=True)
                app.logger.warning("Created missing expense tracker tables at runtime (expense_category/expense_entry).")
        except Exception as e:
            app.logger.warning(f"Expense tracker table check skipped: {e}")
        finally:
            app._expense_tracker_tables_checked = True  # type: ignore[attr-defined]

    return app
