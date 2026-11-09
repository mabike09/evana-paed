import sys
import types
import unittest
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from flask_wtf.csrf import generate_csrf
from jinja2 import ChoiceLoader, DictLoader

config_module = types.ModuleType('config')
config_module.Config = type('Config', (), {})
sys.modules.setdefault('config', config_module)

from app.extensions import csrf, db
from app.models import APBill, APAuditLog, APSupplier, User
from app.routes import finance


class SupplierInvoiceActionsTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'app/templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='test', SQLALCHEMY_DATABASE_URI='sqlite://')
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}'}), self.app.jinja_loader])
        db.init_app(self.app)
        csrf.init_app(self.app)
        login = LoginManager(self.app)
        login.user_loader(lambda user_id: db.session.get(User, int(user_id)))
        self.app.register_blueprint(finance.bp)
        self.app.add_url_rule('/token', view_func=generate_csrf)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.users = {}
        for role in ('admin', 'accountant'):
            user = User(username=role, email=role+'@test.example', password_hash='unused', role=role)
            db.session.add(user)
            db.session.flush()
            self.users[role] = user.id
        supplier = APSupplier(supplier_name='Supplier', category='utilities')
        db.session.add(supplier)
        db.session.flush()
        self.supplier_id = supplier.id
        bill = APBill(supplier_id=supplier.id, invoice_number='TEST-1', invoice_date='2026-11-01',
                      due_date='2026-11-30', expense_category='utilities', invoice_total=100,
                      submitted_by='admin', submitted_date='2026-11-01', created_by='admin')
        db.session.add(bill)
        db.session.commit()
        self.bill_id = bill.id
        self.ledger = f'/finance/accounts-payable/suppliers/{supplier.id}'
        self.client = self.app.test_client()
        self.login('admin')

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, role):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.users[role])
            session['_fresh'] = True

    def post(self, action, **data):
        data['csrf_token'] = self.client.get('/token').get_data(as_text=True)
        return self.client.post(f'/finance/accounts-payable/bills/{self.bill_id}/{action}', data=data)

    def test_admin_controls_and_edit_return_to_supplier(self):
        page = self.client.get(self.ledger)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Edit invoice', page.data)
        self.assertIn(b'Delete', page.data)
        self.assertIn(b'name="csrf_token"', page.data)
        result = self.post('edit', due_date='2026-12-01', invoice_total='150', return_to_supplier='1')
        self.assertEqual(result.location, self.ledger)
        bill = db.session.get(APBill, self.bill_id)
        self.assertEqual(bill.invoice_total, 150)
        self.assertEqual(bill.due_date, '2026-12-01')
        self.assertEqual(APAuditLog.query.filter_by(action='edit', entity='bill').count(), 1)

    def test_delete_returns_to_supplier_and_audits(self):
        result = self.post('delete', return_to_supplier='1')
        self.assertEqual(result.location, self.ledger)
        self.assertIsNone(db.session.get(APBill, self.bill_id))
        self.assertEqual(APAuditLog.query.filter_by(action='delete', entity='bill').count(), 1)
        self.assertIn(b'No invoices for this supplier.', self.client.get(result.location).data)

    def test_accountant_cannot_see_or_use_controls(self):
        self.login('accountant')
        self.assertNotIn(b'Edit invoice', self.client.get(self.ledger).data)
        for action in ('edit', 'delete'):
            self.assertEqual(self.post(action, invoice_total='200').status_code, 403)
        self.assertEqual(db.session.get(APBill, self.bill_id).invoice_total, 100)

    def test_csrf_required_and_default_return_preserved(self):
        path = f'/finance/accounts-payable/bills/{self.bill_id}/delete'
        self.assertEqual(self.client.post(path).status_code, 400)
        result = self.post('edit', due_date='2026-12-01')
        self.assertEqual(result.location, '/finance/accounts-payable')


if __name__ == '__main__':
    unittest.main()
