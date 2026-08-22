import unittest
from datetime import date
import sys
import types

from flask import Flask

# Production supplies an untracked config module; the unit test only needs the
# package import to succeed before creating its isolated Flask application.
config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.extensions import db
from app.models import Invoice
from app.utils import generate_invoice_number


class InvoiceNumberTests(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _invoice(self, number):
        db.session.add(
            Invoice(
                number=number,
                patient_id=1,
                issue_date="2026-08-22",
                amount=0,
                payer_type="Cash",
            )
        )

    def test_uses_highest_sequence_when_numbers_have_gaps(self):
        self._invoice("INV-2608-0001")
        self._invoice("INV-2608-0283")
        db.session.commit()

        self.assertEqual(generate_invoice_number(date(2026, 8, 22)), "INV-2608-0284")

    def test_ignores_other_months_and_malformed_numbers(self):
        self._invoice("INV-2607-0999")
        self._invoice("INV-2608-imported")
        db.session.commit()

        self.assertEqual(generate_invoice_number(date(2026, 8, 22)), "INV-2608-0001")


if __name__ == "__main__":
    unittest.main()
