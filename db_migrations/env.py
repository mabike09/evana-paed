# db_migrations/env.py
from __future__ import annotations
from alembic import context
from logging.config import fileConfig
import os, sys

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import create_app
from app.extensions import db

app = create_app()
with app.app_context():
    target_metadata = db.metadata

def run_migrations_offline():
    with app.app_context():
        url = app.config.get("SQLALCHEMY_DATABASE_URI")
        if not url:
            raise RuntimeError("SQLALCHEMY_DATABASE_URI not set")
        context.configure(
            url=url,
            target_metadata=target_metadata,
            literal_binds=True,
            compare_type=True,
            render_as_batch=True,  # important for SQLite
        )
        with context.begin_transaction():
            context.run_migrations()

def run_migrations_online():
    with app.app_context():
        conn = db.engine.connect()
        try:
            context.configure(
                connection=conn,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=True,  # important for SQLite
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            conn.close()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
