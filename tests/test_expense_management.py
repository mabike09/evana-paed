from datetime import datetime
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
from app.models import ExpenseCategory, ExpenseEntry, User
from app.routes import finance


class ExpenseManagementTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "app/templates"))
        self.app.config.update(TESTING=True, SECRET_KEY="test-secret", SQLALCHEMY_DATABASE_URI="sqlite://",
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% for message in get_flashed_messages() %}{{ message }}{% endfor %}{% block content %}{% endblock %}"}),
            self.app.jinja_loader,
        ])
        db.init_app(self.app)
        csrf.init_app(self.app)
        login_manager = LoginManager(self.app)
        self.app.register_blueprint(finance.bp)

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
        for role in ("admin", "accountant", "reception", "nurse", "branch_manager", "doctor"):
            user = User(username=role, email=f"{role}@example.test", password_hash="unused", role=role)
            db.session.add(user)
            db.session.flush()
            self.users[role] = user.id
        self.category = ExpenseCategory(name="rent", created_by="original-user")
        self.other_category = ExpenseCategory(name="utilities", created_by="original-user")
        db.session.add_all([self.category, self.other_category])
        db.session.flush()
        self.expense = ExpenseEntry(expense_date="2026-07-24", category_id=self.category.id,
                                    description="Original expense", vendor_payee="Original vendor", reference="R-1",
                                    amount=Decimal("300000.00"), entered_by="original-user",
                                    created_at=datetime(2026, 7, 24, 10, 0))
        db.session.add(self.expense)
        db.session.commit()
        self.path = f"/finance/expense-tracker/{self.expense.id}"
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
            if role:
                session["_user_id"] = str(self.users[role])
                session["_fresh"] = True

    def _post(self, path, **data):
        data["csrf_token"] = self.client.get("/test/csrf").get_data(as_text=True)
        return self.client.post(path, data=data)

    def _edit(self, query="", **overrides):
        data = dict(expense_date="2026-07-25", category_id=str(self.other_category.id),
                    description="Corrected expense", vendor_payee="Corrected vendor", reference="R-2", amount="250000.50")
        data.update(overrides)
        return self._post(self.path + "/edit" + query, **data)

    def test_admin_can_edit_all_fields_and_original_attribution_is_preserved(self):
        response = self._edit()
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.expense)
        self.assertEqual(self.expense.expense_date, "2026-07-25")
        self.assertEqual(self.expense.category_id, self.other_category.id)
        self.assertEqual(self.expense.description, "Corrected expense")
        self.assertEqual(self.expense.vendor_payee, "Corrected vendor")
        self.assertEqual(self.expense.reference, "R-2")
        self.assertEqual(self.expense.amount, Decimal("250000.50"))
        self.assertEqual(self.expense.entered_by, "original-user")
        self.assertEqual(self.expense.created_at, datetime(2026, 7, 24, 10, 0))

    def test_admin_can_delete_and_get_cannot_delete(self):
        self.assertEqual(self.client.get(self.path + "/delete").status_code, 405)
        self.assertEqual(ExpenseEntry.query.count(), 1)
        self.assertEqual(self._post(self.path + "/delete").status_code, 302)
        self.assertEqual(ExpenseEntry.query.count(), 0)

    def test_other_roles_cannot_edit_or_delete_with_direct_requests(self):
        for role in self.users.keys() - {"admin"}:
            with self.subTest(role=role):
                self._login(role)
                self.assertEqual(self.client.get(self.path + "/edit").status_code, 403)
                self.assertEqual(self._edit().status_code, 403)
                self.assertEqual(self._post(self.path + "/delete").status_code, 403)
        self.assertEqual(ExpenseEntry.query.one().amount, Decimal("300000.00"))

    def test_anonymous_requests_cannot_edit_or_delete(self):
        self._login(None)
        self.assertEqual(self.client.get(self.path + "/edit").status_code, 401)
        self.assertEqual(self._edit().status_code, 401)
        self.assertEqual(self._post(self.path + "/delete").status_code, 401)

    def test_mutations_require_csrf(self):
        for action in ("edit", "delete"):
            self.assertEqual(self.client.post(self.path + "/" + action).status_code, 400)
        self.assertEqual(ExpenseEntry.query.one().amount, Decimal("300000.00"))

    def test_table_actions_are_admin_only_and_delete_requires_confirmation(self):
        html = self.client.get("/finance/expense-tracker?start_date=2026-07-24&end_date=2026-07-24").get_data(as_text=True)
        self.assertIn(f'aria-label="Edit expense {self.expense.id}"', html)
        self.assertIn(f'aria-label="Delete expense {self.expense.id}"', html)
        self.assertIn("return confirm(", html)
        self.assertIn("start_date=2026-07-24", html)
        self._login("accountant")
        html = self.client.get("/finance/expense-tracker").get_data(as_text=True)
        self.assertIn("Original expense", html)
        self.assertNotIn('aria-label="Edit expense', html)
        self.assertNotIn('aria-label="Delete expense', html)
        self.assertNotIn(">Actions</th>", html)

    def test_edit_form_is_prefilled_and_filters_survive_save_cancel_and_delete(self):
        query = "?start_date=2026-07-01&end_date=2026-07-31&next=https://example.test"
        html = self.client.get(self.path + "/edit" + query).get_data(as_text=True)
        for value in ("2026-07-24", "Original expense", "Original vendor", "R-1", "300000.00"):
            self.assertIn(f'value="{value}"', html)
        self.assertIn(f'value="{self.category.id}" selected', html)
        self.assertIn("start_date=2026-07-01", html)
        for response in (self._edit(query), self._post(self.path + "/delete" + query)):
            self.assertEqual(response.status_code, 302)
            self.assertIn("start_date=2026-07-01", response.location)
            self.assertIn("end_date=2026-07-31", response.location)
            self.assertNotIn("next=", response.location)

    def test_invalid_fields_preserve_values_and_do_not_change_the_expense(self):
        for changes in ({"amount": "NaN"}, {"amount": "sNaN"}, {"amount": "Infinity"}, {"amount": "bad"},
                        {"amount": "-0.001"}, {"amount": "10000000000"}, {"amount": ""},
                        {"expense_date": "2026-02-30"}, {"category_id": "bad"}, {"category_id": "99999"},
                        {"description": ""}, {"description": "x" * 256}, {"vendor_payee": ""},
                        {"vendor_payee": "x" * 181}, {"reference": "x" * 101}):
            with self.subTest(changes=changes):
                response = self._edit(**changes)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Save changes", response.get_data(as_text=True))
                self.assertIn("Corrected vendor" if "vendor_payee" not in changes else "Corrected expense",
                              response.get_data(as_text=True))
                db.session.refresh(self.expense)
                self.assertEqual(self.expense.description, "Original expense")
                self.assertEqual(self.expense.amount, Decimal("300000.00"))

    def test_current_inactive_category_can_be_kept_but_other_inactive_category_cannot_be_selected(self):
        self.category.is_active = self.other_category.is_active = False
        db.session.commit()
        html = self.client.get(self.path + "/edit").get_data(as_text=True)
        self.assertIn("rent (inactive)", html)
        self.assertNotIn("utilities (inactive)", html)
        self.assertEqual(self._edit().status_code, 200)
        self.assertEqual(self._edit(category_id=str(self.category.id)).status_code, 302)

    def test_zero_amount_and_blank_optional_reference_are_supported(self):
        self.assertEqual(self._edit(amount="0", reference="").status_code, 302)
        self.assertEqual(self.expense.amount, Decimal("0.00"))
        self.assertIsNone(self.expense.reference)

    def test_missing_entries_return_404(self):
        self.assertEqual(self.client.get("/finance/expense-tracker/9999/edit").status_code, 404)
        self.assertEqual(self._post("/finance/expense-tracker/9999/delete").status_code, 404)

    def test_accountant_can_still_record_expenses(self):
        self._login("accountant")
        response = self._post("/finance/expense-tracker", action="add_expense", expense_date="2026-07-26",
                              category_id=str(self.category.id), description="New expense", vendor_payee="Vendor", amount="500")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ExpenseEntry.query.count(), 2)
        self.assertEqual(ExpenseEntry.query.filter_by(description="New expense").one().entered_by, "accountant")


if __name__ == "__main__":
    unittest.main()
