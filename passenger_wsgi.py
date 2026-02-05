# passenger_wsgi.py — Evana – Paed Edition
import os
import sys

APP_DIR = os.path.dirname(__file__)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# ***** EDITION FLAG (optional; not required by the factory) *****
os.environ.setdefault("EVANA_EDITION", "paed")

# Logging path (your factory writes to logs/app.log; ensure the folder exists)
LOG_FILE = os.path.join(APP_DIR, "logs", "app.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.environ.setdefault("EVANA_LOG_FILE", LOG_FILE)

# Database (SQLite by default — override in your environment if needed)
os.environ.setdefault(
    "SQLALCHEMY_DATABASE_URI",
    f"sqlite:///{os.path.join(APP_DIR, 'evana_paed.sqlite')}"
)

# Flask config
os.environ.setdefault("FLASK_ENV", "production")
os.environ.setdefault("FLASK_SKIP_DOTENV", "1")
os.environ.setdefault("SECRET_KEY", "change-me-to-a-strong-random-string")

# Import the application factory from app/__init__.py
from app import create_app  # package module, not app.py

# Passenger WSGI entrypoint
application = create_app()
