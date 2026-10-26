import sys
import types
import unittest

from flask import Flask
from sqlalchemy import text

config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app import _normalize_invoice_payer_values
from app.extensions import db
from app.models import Invoice


class InvoicePayerNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        db.session.add_all(
            [
                Invoice(patient_id=1, issue_date="2026-09-18", amount=0, payer_type="Cash"),
                Invoice(patient_id=2, issue_date="2026-09-18", amount=0, payer_type="Insurance"),
            ]
        )
        db.session.commit()
        db.session.execute(
            text("UPDATE invoice SET payer_type = 'cash' WHERE patient_id = 1")
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_only_legacy_values_are_updated(self):
        self.assertEqual(_normalize_invoice_payer_values(), (0, 1))
        values = db.session.execute(
            text("SELECT payer_type FROM invoice ORDER BY patient_id")
        ).scalars().all()
        self.assertEqual(values, ["Cash", "Insurance"])

        self.assertEqual(_normalize_invoice_payer_values(), (0, 0))


if __name__ == "__main__":
    unittest.main()
