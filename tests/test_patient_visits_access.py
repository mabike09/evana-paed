import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager

# Production supplies an untracked config module. These tests use an isolated
# Flask application and only need the package import to succeed.
config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.extensions import db
from app.models import User
from app.routes import patients


class AccountantPatientVisitsAccessTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(
            SECRET_KEY="test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            TESTING=True,
        )
        db.init_app(self.app)
        self.login_manager = LoginManager(self.app)
        self.app.register_blueprint(patients.bp)

        @self.login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        accountant = User(
            username="accountant",
            email="accountant@example.test",
            password_hash="unused",
            role="accountant",
        )
        db.session.add(accountant)
        db.session.commit()
        self.accountant_id = accountant.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_accountant_can_open_patient_visits_page(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.accountant_id)
            session["_fresh"] = True

        with patch.object(patients, "render_template", return_value="Patient Visits"):
            response = client.get("/patients/visits")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "Patient Visits")

    def test_accountant_sees_visits_link_in_patients_dropdown(self):
        template = self.app.jinja_env.get_template("base.html")
        with self.app.test_request_context():
            html = template.render(
                current_user=SimpleNamespace(
                    is_authenticated=True,
                    role="accountant",
                    username="accountant",
                ),
                url_for=lambda endpoint, **values: f"/{endpoint}",
                has_endpoint=lambda endpoint: False,
            )

        self.assertIn('href="/patients.patient_visits">Visits</a>', html)


if __name__ == "__main__":
    unittest.main()
