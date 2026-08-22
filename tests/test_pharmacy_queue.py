import sys
import types
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

# Production supplies an untracked config module.  The tests create an isolated
# Flask application and only need the package import to succeed.
config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.extensions import db
from app.models import BillingQueue, Invoice, InvoiceLine, Item, Patient, Payment
from app.routes import billing


class PaidInvoicePharmacyQueueTests(unittest.TestCase):
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

        patient = Patient(
            first_name="Test",
            last_name="Patient",
            phone="000",
            insurance_provider="Cash",
        )
        item = Item(name="Late prescription", is_drug=True, current_qty=10)
        db.session.add_all([patient, item])
        db.session.flush()

        self.patient_id = patient.id
        self.invoice = Invoice(
            patient_id=patient.id,
            issue_date="2026-08-22",
            amount=Decimal("10000.00"),
            payer_type="Cash",
        )
        db.session.add(self.invoice)
        db.session.flush()
        db.session.add(
            InvoiceLine(
                invoice_id=self.invoice.id,
                kind="drug",
                item_id=item.id,
                description=item.name,
                qty=1,
                unit_price=Decimal("10000.00"),
                line_total=Decimal("10000.00"),
                insurer_amount=0,
                patient_amount=Decimal("10000.00"),
            )
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _ensure_queue(self):
        with patch.object(billing, "current_user", SimpleNamespace(id=7)):
            return billing._ensure_pharmacy_queue_for_paid_invoice(
                self.invoice,
                self.patient_id,
            )

    def test_drug_added_to_already_paid_invoice_is_queued(self):
        db.session.add(
            Payment(
                invoice_id=self.invoice.id,
                payment_date="2026-08-22",
                amount=Decimal("10000.00"),
                method="Cash",
            )
        )
        db.session.commit()

        queue_entry = self._ensure_queue()
        db.session.commit()

        self.assertIsNotNone(queue_entry)
        self.assertEqual(queue_entry.kind, "PHARMACY")
        self.assertEqual(queue_entry.status, "Open")
        self.assertEqual(queue_entry.patient_id, self.patient_id)

    def test_drug_waits_until_recalculated_invoice_is_fully_paid(self):
        db.session.add(
            Payment(
                invoice_id=self.invoice.id,
                payment_date="2026-08-22",
                amount=Decimal("5000.00"),
                method="Cash",
            )
        )
        db.session.commit()

        self.assertIsNone(self._ensure_queue())
        self.assertEqual(BillingQueue.query.count(), 0)

        db.session.add(
            Payment(
                invoice_id=self.invoice.id,
                payment_date="2026-08-22",
                amount=Decimal("5000.00"),
                method="Cash",
            )
        )
        db.session.commit()

        self.assertIsNotNone(self._ensure_queue())


if __name__ == "__main__":
    unittest.main()
