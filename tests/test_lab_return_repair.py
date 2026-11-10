from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import io
import sys
import types
import unittest
from unittest.mock import patch

from flask import Flask

config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.extensions import db
from app.models import BillingQueue, Invoice, LabOrder, Patient, Visit
from scripts.repair_lab_return_visits import RepairError, main, repair_visits


class LabReturnRepairTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://",
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.stamp = datetime(2026, 11, 8, 12)
        self.patient = Patient(first_name="Test", last_name="Patient", phone="000")
        db.session.add(self.patient)
        db.session.flush()
        self.visit = self._returned_visit()
        self.other = self._returned_visit()
        self.invoice = Invoice(patient_id=self.patient.id, visit_id=self.visit.id,
                               issue_date="2026-11-08", amount=2500)
        db.session.add(self.invoice)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _returned_visit(self):
        visit = Visit(patient_id=self.patient.id, visit_date="2026-11-08", status="Open",
                      current_station="DOCTOR", closed_at=self.stamp, notes="Preserve notes")
        db.session.add(visit)
        db.session.flush()
        db.session.add_all([
            BillingQueue(patient_id=self.patient.id, visit_id=visit.id, status="Open", kind="DOCTOR",
                         description="Returned from Lab", added_at=self.stamp + timedelta(minutes=10)),
            LabOrder(patient_id=self.patient.id, visit_id=visit.id, status="Completed"),
        ])
        return visit

    def _snapshot(self, *models):
        return {
            model.__tablename__: db.session.execute(db.select(model.__table__).order_by(model.id)).all()
            for model in models
        }

    def test_preview_does_not_mutate_any_data(self):
        before = self._snapshot(Visit, BillingQueue, LabOrder, Invoice)
        result = repair_visits([self.visit.id])
        self.assertEqual(result[0]["action"], "would clear")
        self.assertEqual(self._snapshot(Visit, BillingQueue, LabOrder, Invoice), before)

    def test_apply_repairs_only_selected_timestamp_and_is_idempotent(self):
        before = self._snapshot(BillingQueue, LabOrder, Invoice)
        repair_visits([self.visit.id], apply=True)
        db.session.expire_all()
        self.assertIsNone(self.visit.closed_at)
        self.assertFalse(self.visit.is_closed)
        self.assertEqual(self.visit.status, "Open")
        self.assertEqual(self.visit.current_station, "DOCTOR")
        self.assertEqual(self.visit.notes, "Preserve notes")
        self.assertEqual(self.other.closed_at, self.stamp)
        self.assertEqual(self._snapshot(BillingQueue, LabOrder, Invoice), before)
        result = repair_visits([self.visit.id], apply=True)
        self.assertEqual(result[0]["action"], "already consistent")

    def test_invalid_selection_prevents_all_repairs(self):
        self.other.status = "Closed"
        db.session.commit()
        before = self._snapshot(Visit)
        with self.assertRaisesRegex(RepairError, "not Open"):
            repair_visits([self.visit.id, self.other.id], apply=True)
        self.assertEqual(self._snapshot(Visit), before)

    def test_missing_selection_prevents_all_repairs(self):
        before = self._snapshot(Visit)
        with self.assertRaisesRegex(RepairError, "does not exist"):
            repair_visits([self.visit.id, 9999], apply=True)
        self.assertEqual(self._snapshot(Visit), before)

    def test_visit_requires_active_doctor_return_evidence(self):
        queue = BillingQueue.query.filter_by(visit_id=self.visit.id).one()
        for field, value in (("description", "From Triage"), ("status", "Closed"),
                             ("patient_id", self.patient.id + 100)):
            original = getattr(queue, field)
            setattr(queue, field, value)
            db.session.commit()
            with self.subTest(field=field):
                with self.assertRaisesRegex(RepairError, "no active Returned from Lab"):
                    repair_visits([self.visit.id], apply=True)
                self.assertEqual(self.visit.closed_at, self.stamp)
            setattr(queue, field, original)
            db.session.commit()

    def test_visit_at_another_station_is_refused(self):
        self.visit.current_station = "PHARMACY"
        db.session.commit()
        with self.assertRaisesRegex(RepairError, "not at the DOCTOR"):
            repair_visits([self.visit.id], apply=True)
        self.assertEqual(self.visit.closed_at, self.stamp)

    def test_pending_lab_work_for_selected_visit_is_refused(self):
        db.session.add(LabOrder(patient_id=self.patient.id, visit_id=self.visit.id, status="PendingPayment"))
        db.session.commit()
        with self.assertRaisesRegex(RepairError, "pending lab work"):
            repair_visits([self.visit.id], apply=True)
        self.assertEqual(self.visit.closed_at, self.stamp)

    def test_pending_lab_work_for_other_visit_does_not_block_repair(self):
        db.session.add(LabOrder(patient_id=self.patient.id, visit_id=self.other.id, status="Pending"))
        db.session.commit()
        repair_visits([self.visit.id], apply=True)
        self.assertIsNone(self.visit.closed_at)

    def test_completed_lab_work_must_match_visit_and_patient(self):
        order = LabOrder.query.filter_by(visit_id=self.visit.id).one()
        order.patient_id = self.patient.id + 100
        db.session.commit()
        with self.assertRaisesRegex(RepairError, "no completed lab order"):
            repair_visits([self.visit.id], apply=True)

    def test_conflicting_open_station_queue_is_refused(self):
        db.session.add(BillingQueue(patient_id=self.patient.id, visit_id=self.visit.id,
                                    status="Open", kind="LAB"))
        db.session.commit()
        with self.assertRaisesRegex(RepairError, "conflicting open queue"):
            repair_visits([self.visit.id], apply=True)

    def test_closure_after_lab_return_is_refused(self):
        self.visit.closed_at = self.stamp + timedelta(hours=1)
        db.session.commit()
        with self.assertRaisesRegex(RepairError, "not closed before"):
            repair_visits([self.visit.id], apply=True)

    def test_command_defaults_to_preview_and_requires_explicit_apply(self):
        for apply, expected in ((False, "would clear"), (True, "cleared")):
            output = io.StringIO()
            args = ["--visit-id", str(self.visit.id)] + (["--apply"] if apply else [])
            with patch("app.create_app", return_value=self.app), redirect_stdout(output):
                self.assertEqual(main(args), 0)
            self.assertIn(expected, output.getvalue())
            db.session.expire_all()
            self.assertEqual(self.visit.closed_at, None if apply else self.stamp)

    def test_command_failure_does_not_change_valid_selection(self):
        output = io.StringIO()
        args = ["--visit-id", str(self.visit.id), "--visit-id", "9999", "--apply"]
        with patch("app.create_app", return_value=self.app), redirect_stderr(output):
            self.assertEqual(main(args), 1)
        self.assertIn("No visits changed", output.getvalue())
        self.assertEqual(self.visit.closed_at, self.stamp)

    def test_selection_is_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main([])


if __name__ == "__main__":
    unittest.main()
