from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys
import types
import unittest

from flask import Flask, g
from flask_login import LoginManager
from flask_wtf.csrf import generate_csrf
from jinja2 import ChoiceLoader, DictLoader

config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.extensions import csrf, db
from app.models import (
    BillingQueue, Invoice, InvoiceLine, Item, ItemTxn, LabOrder, LabOrderLine,
    Patient, Payer, PriceBook, PriceItem, Procedure, User, Visit,
)
from app.routes import lab, patients


class ClosedVisitProtectionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "app/templates"))
        self.app.config.update(TESTING=True, SECRET_KEY="test-secret", SQLALCHEMY_DATABASE_URI="sqlite://",
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            self.app.jinja_loader,
        ])
        db.init_app(self.app)
        csrf.init_app(self.app)
        login_manager = LoginManager(self.app)
        self.app.register_blueprint(patients.bp)
        self.app.register_blueprint(lab.bp)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        @self.app.get("/test/csrf")
        def token():
            return generate_csrf()

        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.users = {}
        for role in ("admin", "doctor", "pediatrician", "nurse"):
            user = User(username=role, email=f"{role}@example.test", password_hash="unused", role=role)
            db.session.add(user)
            db.session.flush()
            self.users[role] = user.id

        self.patient = Patient(first_name="Test", last_name="Patient", phone="000", insurance_provider="Cash")
        self.drug = Item(name="Test drug", is_drug=True, current_qty=10, sell_price=500)
        self.procedure = Procedure(code="TEST-PROC", name="Test procedure", default_price=1000)
        payer = Payer(name="Cash")
        db.session.add_all([self.patient, self.drug, self.procedure, payer])
        db.session.flush()
        book = PriceBook(name="Cash", payer_id=payer.id, effective_date=date(2026, 1, 1))
        db.session.add(book)
        db.session.flush()
        self.lab_test = PriceItem(pricebook_id=book.id, item_type="lab", item_name="Test blood count", sell_price=2000)
        self.closed = Visit(patient_id=self.patient.id, visit_date="2026-05-09", status="Closed",
                            notes="[Presenting]\nOriginal complaint\n\n[HOPC]\nOriginal history\n\n"
                                  "[Exam]\nOriginal exam\n\n[Plan]\nOriginal plan",
                            diagnosis="Original diagnosis")
        self.open = Visit(patient_id=self.patient.id, visit_date="2026-11-07", status="Open")
        db.session.add_all([self.lab_test, self.closed, self.open])
        db.session.flush()
        self.invoice = Invoice(patient_id=self.patient.id, visit_id=self.closed.id, number="INV-HISTORY",
                               issue_date=self.closed.visit_date, amount=500)
        self.triage = BillingQueue(patient_id=self.patient.id, visit_id=self.closed.id, kind="TRIAGE", status="Open")
        order = LabOrder(patient_id=self.patient.id, visit_id=self.closed.id, status="Completed")
        db.session.add_all([self.invoice, self.triage, order])
        db.session.flush()
        db.session.add_all([
            InvoiceLine(invoice_id=self.invoice.id, kind="drug", item_id=self.drug.id,
                        description="Historical prescription", qty=1, unit_price=500, line_total=500),
            LabOrderLine(order_id=order.id, test_name="Historical test", result_text="Original result", status="Done"),
        ])
        db.session.commit()
        self.chart = f"/patients/{self.patient.id}/chart"
        self.client = self.app.test_client()
        self._login("admin")

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _login(self, role):
        g.pop("_login_user", None)
        g.pop("csrf_token", None)
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(self.users[role])
            session["_fresh"] = True

    def _post(self, path, data):
        return self.client.post(path, data={**data, "csrf_token": self.client.get("/test/csrf").get_data(as_text=True)})

    def _actions(self, visit):
        return [
            (f"/visits/{visit.id}/clinical", {"presenting": "Changed", "diagnosis": "Changed"}),
            (f"/visits/{visit.id}/update-clinical", {"plan": "Changed"}),
            (f"/visits/{visit.id}/add_drugs", {"drug_id[]": str(self.drug.id), "drug_name[]": self.drug.name, "qty[]": "2"}),
            (f"/visits/{visit.id}/add_procedure", {"proc_id": str(self.procedure.id)}),
            (f"/visits/{visit.id}/send-to-lab", {"lab_tests": str(self.lab_test.id)}),
            (f"/visits/{visit.id}/close", {"drug_id[]": str(self.drug.id), "drug_name[]": self.drug.name, "qty[]": "2"}),
        ]

    def _snapshot(self):
        # Read persisted values so a guard placed after a commit cannot pass.
        return {
            model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
            for model in (Visit, Invoice, InvoiceLine, BillingQueue, LabOrder, LabOrderLine, Item, ItemTxn)
        }

    def test_closed_visit_rejects_all_chart_writes_for_clinicians_and_admin(self):
        for status, closed_at in (("Closed", None), (" cLoSeD ", None), ("Open", datetime(2026, 5, 9, 12))):
            self.closed.status, self.closed.closed_at = status, closed_at
            db.session.commit()
            for role in ("doctor", "pediatrician", "admin"):
                self._login(role)
                for path, data in self._actions(self.closed):
                    with self.subTest(status=status, closed_at=closed_at, role=role, path=path):
                        before = self._snapshot()
                        response = self._post(path, data)
                        self.assertEqual(response.status_code, 403)
                        self.assertIn("closed and read-only", response.get_data(as_text=True))
                        self.assertEqual(self._snapshot(), before)

    def test_closed_visit_without_invoice_does_not_create_one(self):
        db.session.delete(self.invoice)
        db.session.commit()
        for path, data in self._actions(self.closed):
            with self.subTest(path=path):
                before = self._snapshot()
                self.assertEqual(self._post(path, data).status_code, 403)
                self.assertEqual(self._snapshot(), before)

    def test_stale_triage_cannot_overwrite_closed_visit_notes(self):
        for role in ("admin", "nurse"):
            self._login(role)
            before = self._snapshot()
            path = f"/triage/{self.triage.id}/start"
            self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self._post(path, {"notes": "Changed", "weight_kg": "12"}).status_code, 403)
            self.assertEqual(self._snapshot(), before)

    def test_closed_chart_preserves_history_and_has_no_edit_controls(self):
        self._login("doctor")
        before = self._snapshot()
        response = self.client.get(self.chart, query_string={"visit_id": self.closed.id})
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for text in ("closed and read-only", "Original complaint", "Original history", "Original exam",
                     "Original plan", "Original diagnosis", "INV-HISTORY", "Historical prescription", "Original result"):
            self.assertIn(text, html)
        self.assertEqual(html.count("readonly"), 5)
        for control in ("Save Clinical Notes", "Add all drugs", "Add procedure", "Request selected tests", "Save &amp; Close Visit"):
            self.assertNotIn(control, html)
        self.assertNotIn('id="close-visit-form"', html)
        self.assertNotIn('name="lab_tests"', html)
        self.assertEqual(self._snapshot(), before)

    def test_timestamp_closed_chart_is_also_read_only(self):
        self.closed.status = "Open"
        self.closed.closed_at = datetime(2026, 5, 9, 12)
        db.session.commit()
        html = self.client.get(self.chart, query_string={"visit_id": self.closed.id}).get_data(as_text=True)
        self.assertIn("closed and read-only", html)
        self.assertNotIn("Add all drugs", html)

    def test_switching_to_open_visit_restores_controls_and_tracks_clinician(self):
        self._login("doctor")
        html = self.client.get(self.chart, query_string={"visit_id": self.open.id}).get_data(as_text=True)
        self.assertNotIn("closed and read-only", html)
        self.assertNotIn("readonly", html)
        for control in ("Save Clinical Notes", "Add all drugs", "Add procedure", "Request selected tests", 'id="close-visit-form"'):
            self.assertIn(control, html)
        self.assertEqual(self.open.seen_by_id, self.users["doctor"])
        self.assertIsNone(self.closed.seen_by_id)

    def test_open_visit_still_accepts_notes_drugs_procedures_and_tests(self):
        self._login("doctor")
        for path, data in self._actions(self.open)[:5]:
            with self.subTest(path=path):
                self.assertEqual(self._post(path, data).status_code, 302)
        self.assertIn("Changed", self.open.notes)
        inv = Invoice.query.filter_by(visit_id=self.open.id).one()
        self.assertEqual(len(inv.lines), 3)
        self.assertEqual({line.description for line in inv.lines}, {self.drug.name, self.procedure.name, self.lab_test.item_name})
        self.assertEqual(LabOrder.query.filter_by(visit_id=self.open.id).count(), 1)
        self.assertEqual(self.closed.diagnosis, "Original diagnosis")

    def test_visit_closed_after_page_load_rejects_stale_submission(self):
        self.assertEqual(self.client.get(self.chart, query_string={"visit_id": self.open.id}).status_code, 200)
        self.assertEqual(self._post(f"/visits/{self.open.id}/close", {}).status_code, 302)
        self.assertTrue(self.open.is_closed)
        for path, data in self._actions(self.open):
            before = self._snapshot()
            self.assertEqual(self._post(path, data).status_code, 403)
            self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
