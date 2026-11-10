from datetime import date, datetime
from html import unescape
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager
from flask_wtf.csrf import generate_csrf
from jinja2 import ChoiceLoader, DictLoader

config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.extensions import csrf, db
from app.models import (
    BillingQueue, Invoice, InvoiceLine, Item, LabOrder, LabOrderLine,
    Patient, Payer, PriceBook, PriceItem, User, Visit,
)
from app.routes import lab, patients


class LabReturnWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "app/templates"),
        )
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
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
        for role in ("doctor", "labtech"):
            user = User(
                username=role, email=f"{role}@example.test",
                password_hash="unused", role=role,
            )
            db.session.add(user)
            db.session.flush()
            self.users[role] = user.id

        payer = Payer(name="Cash")
        self.drug = Item(name="Follow-up drug", is_drug=True, current_qty=20, sell_price=500)
        db.session.add_all([payer, self.drug])
        db.session.flush()
        book = PriceBook(name="Cash", payer_id=payer.id, effective_date=date(2026, 1, 1))
        db.session.add(book)
        db.session.flush()
        self.additional_test = PriceItem(
            pricebook_id=book.id, item_type="lab", item_name="Additional blood count", sell_price=2000,
        )
        db.session.add(self.additional_test)
        db.session.commit()
        self.client = self.app.test_client()

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
        token = self.client.get("/test/csrf").get_data(as_text=True)
        return self.client.post(path, data={**data, "csrf_token": token})

    def _lab_case(self, provider="Cash", status="Open", closed_at=datetime(2026, 11, 8, 12),
                  description="Sent to Lab from Doctor", test_count=1):
        patient = Patient(first_name="Returning", last_name=provider, phone="000", insurance_provider=provider)
        db.session.add(patient)
        db.session.flush()
        visit = Visit(
            patient_id=patient.id, visit_date="2026-11-08", status=status,
            current_station="LAB", closed_at=closed_at, notes="Original visit notes",
        )
        db.session.add(visit)
        db.session.flush()
        order = LabOrder(patient_id=patient.id, visit_id=visit.id, status="Pending")
        queue = BillingQueue(
            patient_id=patient.id, visit_id=visit.id, kind="LAB", status="Open", description=description,
        )
        db.session.add_all([order, queue])
        db.session.flush()
        lines = [LabOrderLine(order_id=order.id, test_name=f"Original test {i + 1}") for i in range(test_count)]
        db.session.add_all(lines)
        db.session.commit()
        return patient, visit, order, queue, lines

    def _save_results(self, order, lines):
        self._login("labtech")
        return self._post(f"/lab/orders/{order.id}/results", {
            "line_id[]": [str(line.id) for line in lines],
            "result_value[]": ["Normal" for _ in lines],
            "result_text[]": ["Completed result" for _ in lines],
        })

    def test_cash_and_insured_doctor_returns_clear_closure_and_allow_follow_up_care(self):
        for provider, prior_status in (("Cash", "Open"), ("Example Insurance", "Closed")):
            with self.subTest(provider=provider, prior_status=prior_status):
                patient, visit, order, queue, lines = self._lab_case(provider, status=prior_status)
                self.assertTrue(visit.is_closed)

                self.assertEqual(self._save_results(order, lines).status_code, 200)
                db.session.expire_all()
                self.assertEqual(order.status, "Completed")
                self.assertEqual(visit.status, "Open")
                self.assertEqual(visit.current_station, "DOCTOR")
                self.assertIsNone(visit.closed_at)
                self.assertFalse(visit.is_closed)
                self.assertEqual(queue.status, "Closed")
                self.assertEqual(BillingQueue.query.filter_by(
                    visit_id=visit.id, kind="DOCTOR", status="Open",
                ).count(), 1)

                self._login("doctor")
                response = self.client.get(f"/patients/{patient.id}/chart", query_string={"visit_id": visit.id})
                self.assertEqual(response.status_code, 200)
                chart = response.get_data(as_text=True)
                self.assertNotIn("closed and read-only", chart)
                self.assertIn("Completed result", chart)
                for control in ("Save Clinical Notes", "Add all drugs", "Add procedure", "Request selected tests"):
                    self.assertIn(control, chart)

                self.assertEqual(self._post(f"/visits/{visit.id}/clinical", {
                    "presenting": "Post-lab review", "diagnosis": "Follow-up diagnosis",
                }).status_code, 302)
                drug_response = self._post(f"/visits/{visit.id}/add_drugs", {
                    "drug_id[]": str(self.drug.id), "drug_name[]": self.drug.name,
                    "dose[]": "As directed", "qty[]": "2",
                })
                self.assertEqual(drug_response.status_code, 302)
                self.assertIn(f"visit_id={visit.id}", drug_response.headers["Location"])
                self.assertEqual(self.client.get(drug_response.headers["Location"]).status_code, 200)
                lab_response = self._post(f"/visits/{visit.id}/send-to-lab", {
                    "lab_tests": str(self.additional_test.id),
                })
                self.assertEqual(lab_response.status_code, 302)
                self.assertTrue(lab_response.headers["Location"].startswith(f"/patients/{patient.id}/chart?"))
                self.assertIn(f"visit_id={visit.id}", lab_response.headers["Location"])
                self.assertEqual(self.client.get(lab_response.headers["Location"]).status_code, 200)
                db.session.expire_all()
                self.assertIn("Post-lab review", visit.notes)
                self.assertEqual(visit.diagnosis, "Follow-up diagnosis")
                invoice = Invoice.query.filter_by(visit_id=visit.id).one()
                drug_line = InvoiceLine.query.filter_by(invoice_id=invoice.id, kind="drug").one()
                self.assertEqual(drug_line.item_id, self.drug.id)
                self.assertEqual(drug_line.qty, 2)
                self.assertEqual(LabOrder.query.filter_by(visit_id=visit.id).count(), 2)
                self.assertEqual(LabOrder.query.filter_by(visit_id=visit.id, status="Completed").count(), 1)

    def test_lab_return_does_not_unlock_another_closed_visit(self):
        patient, visit, order, queue, lines = self._lab_case()
        stamp = datetime(2026, 5, 9, 12)
        historical = Visit(
            patient_id=patient.id, visit_date="2026-05-09", status="Closed",
            current_station="CLOSED", closed_at=stamp, notes="Historical notes",
        )
        db.session.add(historical)
        db.session.commit()
        self.assertEqual(self._save_results(order, lines).status_code, 200)
        self._login("doctor")
        chart = self.client.get(
            f"/patients/{patient.id}/chart", query_string={"visit_id": historical.id},
        ).get_data(as_text=True)
        self.assertIn("closed and read-only", chart)
        self.assertNotIn("Add all drugs", chart)
        self.assertNotIn('name="lab_tests"', chart)
        self.assertEqual(self._post(f"/visits/{historical.id}/clinical", {"plan": "Overwrite history"}).status_code, 403)
        db.session.refresh(historical)
        self.assertEqual(historical.status, "Closed")
        self.assertEqual(historical.closed_at, stamp)
        self.assertEqual(historical.notes, "Historical notes")

    def test_partial_results_keep_visit_at_lab_and_preserve_closure_marker(self):
        patient, visit, order, queue, lines = self._lab_case(test_count=2)
        stamp = visit.closed_at
        self.assertEqual(self._save_results(order, lines[:1]).status_code, 200)
        db.session.expire_all()
        self.assertEqual(order.status, "Pending")
        self.assertEqual(visit.current_station, "LAB")
        self.assertEqual(visit.closed_at, stamp)
        self.assertTrue(visit.is_closed)
        self.assertEqual(queue.status, "Open")
        self.assertEqual(BillingQueue.query.filter_by(visit_id=visit.id, kind="DOCTOR").count(), 0)

    def test_failed_completion_rolls_back_results_and_routing_then_allows_retry(self):
        patient, visit, order, queue, lines = self._lab_case()
        models = (Visit, LabOrder, LabOrderLine, BillingQueue)
        before = {
            model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
            for model in models
        }
        with patch.object(db.session, "commit", side_effect=RuntimeError("Simulated commit failure")) as commit:
            with self.assertLogs(self.app.logger, level="ERROR"):
                self.assertEqual(self._save_results(order, lines).status_code, 200)
        self.assertEqual(commit.call_count, 1)
        after = {
            model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
            for model in models
        }
        self.assertEqual(after, before)
        with self.client.session_transaction() as session:
            self.assertIn(("danger", "Could not save lab results. Please try again."), session.get("_flashes", []))
            self.assertNotIn(("success", "Lab results saved successfully."), session.get("_flashes", []))

        self.assertEqual(self._save_results(order, lines).status_code, 200)
        db.session.expire_all()
        self.assertEqual(order.status, "Completed")
        self.assertEqual(lines[0].result_text, "Completed result")
        self.assertEqual(visit.status, "Open")
        self.assertIsNone(visit.closed_at)
        self.assertEqual(visit.current_station, "DOCTOR")
        self.assertEqual(queue.status, "Closed")
        self.assertEqual(BillingQueue.query.filter_by(visit_id=visit.id, kind="DOCTOR", status="Open").count(), 1)

    def test_insured_non_doctor_lab_completion_keeps_existing_close_out_behavior(self):
        patient, visit, order, queue, lines = self._lab_case(
            provider="Example Insurance", closed_at=None, description="Sent from patient list",
        )
        self.assertEqual(self._save_results(order, lines).status_code, 200)
        db.session.expire_all()
        self.assertEqual(order.status, "Completed")
        self.assertEqual(visit.status, "Closed")
        self.assertEqual(visit.current_station, "CLOSED")
        self.assertIsNotNone(visit.closed_at)
        self.assertTrue(visit.is_closed)
        self.assertEqual(queue.status, "Closed")
        self.assertEqual(BillingQueue.query.filter_by(visit_id=visit.id, kind="DOCTOR").count(), 0)

    def test_old_pending_cash_order_with_closed_lab_queue_does_not_reopen_finalized_visit(self):
        patient, visit, order, queue, lines = self._lab_case(status="Closed")
        stamp = visit.closed_at
        visit.current_station = "CLOSED"
        queue.status = "Closed"
        queue.closed_at = stamp
        db.session.commit()
        before = {
            model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
            for model in (Visit, BillingQueue)
        }

        self.assertEqual(self._save_results(order, lines).status_code, 200)
        db.session.expire_all()
        self.assertEqual(order.status, "Completed")
        self.assertEqual(lines[0].status, "Done")
        self.assertEqual(lines[0].result_text, "Completed result")
        self.assertEqual(visit.status, "Closed")
        self.assertEqual(visit.current_station, "CLOSED")
        self.assertEqual(visit.closed_at, stamp)
        after = {
            model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
            for model in (Visit, BillingQueue)
        }
        self.assertEqual(after, before)
        self.assertEqual(BillingQueue.query.filter_by(visit_id=visit.id, kind="DOCTOR").count(), 0)

    def test_mismatched_order_patient_does_not_change_another_patients_visit_or_lab_queue(self):
        for status, closed_at in (("Closed", datetime(2026, 11, 8, 12)), ("Open", None)):
            with self.subTest(status=status):
                patient, visit, order, queue, lines = self._lab_case(status=status, closed_at=closed_at)
                other_patient = Patient(first_name="Different", last_name="Patient", phone="001", insurance_provider="Cash")
                db.session.add(other_patient)
                db.session.flush()
                order.patient_id = other_patient.id
                db.session.commit()
                before = {
                    model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
                    for model in (Visit, BillingQueue)
                }

                self.assertEqual(self._save_results(order, lines).status_code, 200)
                db.session.expire_all()
                self.assertEqual(order.status, "Completed")
                self.assertEqual(lines[0].result_text, "Completed result")
                self.assertEqual(queue.status, "Open")
                after = {
                    model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
                    for model in (Visit, BillingQueue)
                }
                self.assertEqual(after, before)
                self.assertEqual(BillingQueue.query.filter_by(visit_id=visit.id, kind="DOCTOR").count(), 0)

    def test_editing_completed_results_does_not_reopen_closed_visits_or_consume_later_lab_queue(self):
        for provider in ("Cash", "Example Insurance"):
            with self.subTest(provider=provider):
                patient, visit, completed, later_queue, lines = self._lab_case(provider, status="Closed")
                stamp = visit.closed_at
                visit.current_station = "CLOSED"
                completed.status = "Completed"
                lines[0].status = "Done"
                lines[0].result_text = "Previous result"
                pending = LabOrder(patient_id=patient.id, visit_id=visit.id, status="Pending")
                closed_doctor_queue = BillingQueue(
                    patient_id=patient.id, visit_id=visit.id, kind="DOCTOR",
                    status="Closed", closed_at=stamp, description="Completed consultation",
                )
                db.session.add_all([pending, closed_doctor_queue])
                db.session.flush()
                pending_line = LabOrderLine(order_id=pending.id, test_name="Later pending test")
                db.session.add(pending_line)
                db.session.commit()
                queue_before = db.session.execute(
                    db.select(BillingQueue.__table__).where(BillingQueue.visit_id == visit.id).order_by(BillingQueue.id),
                ).all()

                self.assertEqual(self._save_results(completed, lines).status_code, 200)
                db.session.expire_all()
                self.assertEqual(lines[0].result_text, "Completed result")
                self.assertEqual(visit.status, "Closed")
                self.assertEqual(visit.current_station, "CLOSED")
                self.assertEqual(visit.closed_at, stamp)
                self.assertTrue(visit.is_closed)
                self.assertEqual(pending.status, "Pending")
                self.assertEqual(pending_line.status, "Ordered")
                queue_after = db.session.execute(
                    db.select(BillingQueue.__table__).where(BillingQueue.visit_id == visit.id).order_by(BillingQueue.id),
                ).all()
                self.assertEqual(queue_after, queue_before)

    def test_doctor_queue_opens_returned_visit_even_when_a_newer_visit_exists(self):
        patient, visit, order, queue, lines = self._lab_case()
        newer = Visit(
            patient_id=patient.id, visit_date="2026-11-10", status="Closed",
            current_station="CLOSED", closed_at=datetime(2026, 11, 10, 12),
        )
        db.session.add(newer)
        db.session.commit()
        self.assertGreater(newer.id, visit.id)
        self.assertEqual(self._save_results(order, lines).status_code, 200)
        self._login("doctor")
        response = self.client.get("/doctors/queue")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'href="([^"]+)"[^>]*>\s*Open Chart', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        chart_url = unescape(match.group(1))
        self.assertEqual(chart_url, f"/patients/{patient.id}/chart?visit_id={visit.id}")
        chart = self.client.get(chart_url).get_data(as_text=True)
        self.assertNotIn("closed and read-only", chart)
        self.assertIn(f'action="/visits/{visit.id}/add_drugs"', chart)
        self.assertIn(f'action="/visits/{visit.id}/send-to-lab"', chart)
        self.assertIsNone(newer.seen_by_id)


if __name__ == "__main__":
    unittest.main()
