import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from datetime import date
from decimal import Decimal

from flask import Flask, g
from flask_login import LoginManager
from flask_wtf.csrf import generate_csrf
from jinja2 import ChoiceLoader, DictLoader

config_module = types.ModuleType('config')
config_module.Config = type('Config', (), {})
sys.modules.setdefault('config', config_module)

from app.extensions import csrf, db
from app.models import PettyCashAuditLog, PettyCashPeriodLock, PettyCashReconciliation, PettyCashTransaction, User
from app.routes import finance


class PettyCashManagementTests(unittest.TestCase):
    def setUp(self):
        self.uploads = tempfile.TemporaryDirectory()
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'app/templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='test-secret', SQLALCHEMY_DATABASE_URI='sqlite://',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False, UPLOAD_FOLDER=self.uploads.name)
        # Render the real ledger/edit templates independently of unrelated navigation.
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '{% for message in get_flashed_messages() %}{{ message }}{% endfor %}{% block content %}{% endblock %}{% block scripts %}{% endblock %}'}),
            self.app.jinja_loader,
        ])
        db.init_app(self.app)
        csrf.init_app(self.app)
        self.login_manager = LoginManager(self.app)
        self.app.register_blueprint(finance.bp)

        @self.login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        @self.app.get('/test/csrf')
        def token():
            return generate_csrf()

        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.users = {}
        for role in ('admin', 'accountant', 'reception', 'nurse', 'branch_manager'):
            user = User(username=role, email=f'{role}@example.test', password_hash='unused', role=role)
            db.session.add(user)
            db.session.flush()
            self.users[role] = user.id
        self.funding = self._entry('cash_in', 1000, '2026-11-01', action_mode='top_up')
        self.spending = self._entry('cash_out', 300, '2026-11-02', attachment_path='petty_cash/original.pdf')
        db.session.commit()
        self.funding_id, self.spending_id = self.funding.id, self.spending.id
        self.client = self.app.test_client()
        self._login('admin')

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.uploads.cleanup()

    def _entry(self, transaction_type, amount, on, **values):
        txn = PettyCashTransaction(date=on, transaction_type=transaction_type, amount=amount,
                                   purpose='Original purpose', description='Original purpose',
                                   expense_category='fuel', entered_by='original-user', **values)
        db.session.add(txn)
        db.session.flush()
        return txn

    def _login(self, role):
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.users[role])
            session['_fresh'] = True

    def _post(self, path, data=None):
        data = dict(data or {})
        data['csrf_token'] = self.client.get('/test/csrf').get_data(as_text=True)
        return self.client.post(path, data=data)

    def _edit(self, txn_id=None, **overrides):
        data = dict(date='2026-11-02', transaction_type='cash_out', action_mode='transaction', amount='200.00',
                    purpose='Corrected purpose', expense_category='utilities', payee='Vendor',
                    voucher_number='V-12', receipt_number='R-12', approved_by='Manager', notes='Correction')
        data.update(overrides)
        return self._post(f'/finance/petty-cash/{txn_id or self.spending_id}/edit', data)

    def _messages(self):
        with self.client.session_transaction() as session:
            return str(session.get('_flashes', []))

    def test_admin_can_edit_and_balances_and_audit_update(self):
        response = self._edit(is_advance='on', is_accounted='on')
        self.assertEqual(response.status_code, 302)
        txn = db.session.get(PettyCashTransaction, self.spending_id)
        self.assertEqual(txn.amount, Decimal('200.00'))
        self.assertEqual(txn.purpose, 'Corrected purpose')
        self.assertEqual(txn.description, txn.purpose)
        self.assertEqual(txn.entered_by, 'original-user')
        self.assertEqual(txn.attachment_path, 'petty_cash/original.pdf')
        self.assertEqual(txn.voucher_number, 'V-12')
        self.assertEqual(txn.receipt_number, 'R-12')
        self.assertEqual(txn.approved_by, 'Manager')
        self.assertIsNotNone(txn.approval_timestamp)
        self.assertTrue(txn.is_advance and txn.is_accounted)
        self.assertEqual(finance._current_balance(), Decimal('800.00'))
        audit = PettyCashAuditLog.query.one()
        self.assertEqual((audit.action, audit.actor_role), ('update', 'admin'))
        changes = json.loads(audit.changes_json)
        self.assertEqual(changes['before']['amount'], '300.00')
        self.assertEqual(changes['after']['amount'], '200.00')

    def test_delete_spending_recalculates_balance_and_keeps_audit(self):
        response = self._post(f'/finance/petty-cash/{self.spending_id}/delete')
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(PettyCashTransaction, self.spending_id))
        self.assertEqual(finance._current_balance(), Decimal('1000.00'))
        audit = PettyCashAuditLog.query.one()
        self.assertEqual((audit.action, audit.record_id), ('delete', self.spending_id))
        self.assertEqual(json.loads(audit.changes_json)['before']['entered_by'], 'original-user')

    def test_other_roles_cannot_edit_or_delete_even_with_direct_requests(self):
        for role in ('accountant', 'reception', 'nurse', 'branch_manager'):
            with self.subTest(role=role):
                self._login(role)
                self.assertEqual(self.client.get(f'/finance/petty-cash/{self.spending_id}/edit').status_code, 403)
                self.assertEqual(self._edit().status_code, 403)
                self.assertEqual(self._post(f'/finance/petty-cash/{self.spending_id}/delete').status_code, 403)
        self.assertEqual(db.session.get(PettyCashTransaction, self.spending_id).amount, Decimal('300.00'))
        self.assertEqual(PettyCashAuditLog.query.count(), 0)

    def test_anonymous_access_and_get_delete_are_rejected(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(f'/finance/petty-cash/{self.spending_id}/edit').status_code, 401)
        self.assertEqual(self.client.get(f'/finance/petty-cash/{self.spending_id}/delete').status_code, 405)

    def test_mutations_require_csrf(self):
        for action in ('edit', 'delete'):
            response = self.client.post(f'/finance/petty-cash/{self.spending_id}/{action}', data={'amount': '10'})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(db.session.get(PettyCashTransaction, self.spending_id).amount, Decimal('300.00'))

    def test_closed_entries_cannot_be_changed_or_moved_to_open_period(self):
        db.session.add(PettyCashPeriodLock(locked_until=date(2026, 11, 2), locked_by='admin'))
        db.session.commit()
        self._edit(date='2026-11-03')
        self._post(f'/finance/petty-cash/{self.spending_id}/delete')
        self.assertEqual(db.session.get(PettyCashTransaction, self.spending_id).date, '2026-11-02')
        self.assertEqual(PettyCashAuditLog.query.count(), 0)
        self.assertIn('locked', self._messages())

    def test_open_entry_cannot_be_moved_into_closed_period(self):
        db.session.add(PettyCashPeriodLock(locked_until=date(2026, 11, 1), locked_by='admin'))
        db.session.commit()
        self._edit(date='2026-11-01')
        self.assertEqual(db.session.get(PettyCashTransaction, self.spending_id).date, '2026-11-02')
        self.assertEqual(PettyCashAuditLog.query.count(), 0)

    def test_reconciled_entries_cannot_be_changed(self):
        reconciliation = PettyCashReconciliation(reconciliation_date='2026-11-02', reconciled_by='admin')
        db.session.add(reconciliation)
        db.session.flush()
        self.spending.reconciliation_id = reconciliation.id
        db.session.commit()
        self._edit()
        self._post(f'/finance/petty-cash/{self.spending_id}/delete')
        self.assertEqual(db.session.get(PettyCashTransaction, self.spending_id).amount, Decimal('300.00'))
        self.assertEqual(PettyCashAuditLog.query.count(), 0)

    def test_deleting_funding_cannot_make_balance_negative(self):
        self._post(f'/finance/petty-cash/{self.funding_id}/delete')
        self.assertIsNotNone(db.session.get(PettyCashTransaction, self.funding_id))
        self.assertEqual(finance._current_balance(), Decimal('700.00'))
        self.assertIn('negative', self._messages())
        self.assertEqual(PettyCashAuditLog.query.count(), 0)

    def test_edit_cannot_make_any_historical_balance_negative(self):
        self._entry('cash_in', 1000, '2026-11-03')
        db.session.commit()
        self._edit(self.funding_id, amount='100', date='2026-11-01', transaction_type='cash_in')
        self.assertEqual(db.session.get(PettyCashTransaction, self.funding_id).amount, Decimal('1000.00'))
        self._edit(date='2026-10-31')
        self.assertEqual(db.session.get(PettyCashTransaction, self.spending_id).date, '2026-11-02')
        self.assertEqual(PettyCashAuditLog.query.count(), 0)

    def test_invalid_fields_leave_database_unchanged_and_preserve_form_values(self):
        for changes in ({'amount': 'NaN'}, {'amount': 'Infinity'}, {'amount': 'oops'}, {'amount': '-1'},
                        {'amount': '10000000000'}, {'date': 'bad-date'}, {'purpose': ''},
                        {'transaction_type': 'invalid'}, {'action_mode': 'invalid'}, {'payee': 'x' * 151}):
            with self.subTest(changes=changes):
                response = self._edit(**changes)
                self.assertEqual(response.status_code, 200)
                self.assertIn('Corrected purpose' if changes.get('purpose') != '' else 'Save changes', response.get_data(as_text=True))
                self.assertEqual(db.session.get(PettyCashTransaction, self.spending_id).amount, Decimal('300.00'))
        self.assertEqual(PettyCashAuditLog.query.count(), 0)

    def test_special_actions_are_always_cash_in_with_miscellaneous_category(self):
        self._edit(self.funding_id, action_mode='top_up', amount='1200', transaction_type='cash_out')
        txn = db.session.get(PettyCashTransaction, self.funding_id)
        self.assertEqual((txn.transaction_type, txn.expense_category), ('cash_in', 'miscellaneous'))
        self.assertEqual((txn.voucher_number, txn.receipt_number), ('', ''))
        self.assertEqual(finance._current_balance(), Decimal('900.00'))

    def test_attachment_replacement_and_invalid_attachment_rollback(self):
        self._edit(attachment=(io.BytesIO(b'bad'), 'bad.exe'))
        txn = db.session.get(PettyCashTransaction, self.spending_id)
        self.assertEqual(txn.attachment_path, 'petty_cash/original.pdf')
        self.assertEqual(txn.amount, Decimal('300.00'))
        self.assertEqual(PettyCashAuditLog.query.count(), 0)
        self._edit(attachment=(io.BytesIO(b'%PDF-test'), 'receipt.pdf'))
        txn = db.session.get(PettyCashTransaction, self.spending_id)
        self.assertNotEqual(txn.attachment_path, 'petty_cash/original.pdf')
        self.assertEqual((Path(self.uploads.name) / txn.attachment_path).read_bytes(), b'%PDF-test')

    def test_admin_controls_are_visible_and_audit_table_is_removed(self):
        html = self.client.get('/finance/petty-cash?start_date=2026-11-01&end_date=2026-11-30').get_data(as_text=True)
        self.assertIn(f'aria-label="Edit petty cash entry {self.spending_id}"', html)
        self.assertIn(f'aria-label="Delete petty cash entry {self.spending_id}"', html)
        self.assertIn('return confirm(', html)
        self.assertIn('d-print-none', html)
        self.assertNotIn('Audit trail', html)
        self._login('accountant')
        html = self.client.get('/finance/petty-cash?start_date=2026-11-01&end_date=2026-11-30').get_data(as_text=True)
        self.assertNotIn('aria-label="Edit petty cash entry', html)
        self.assertNotIn('aria-label="Delete petty cash entry', html)

    def test_closed_and_reconciled_rows_show_status_instead_of_actions(self):
        db.session.add(PettyCashPeriodLock(locked_until=date(2026, 11, 1), locked_by='admin'))
        reconciliation = PettyCashReconciliation(reconciliation_date='2026-11-02', reconciled_by='admin')
        db.session.add(reconciliation)
        db.session.flush()
        self.spending.reconciliation_id = reconciliation.id
        db.session.commit()
        html = self.client.get('/finance/petty-cash?start_date=2026-11-01&end_date=2026-11-30').get_data(as_text=True)
        self.assertIn('Closed period</span>', html)
        self.assertIn('Reconciled</span>', html)
        self.assertNotIn('aria-label="Edit petty cash entry', html)
        self.assertNotIn('aria-label="Delete petty cash entry', html)

    def test_edit_form_is_prefilled_and_filters_are_preserved(self):
        response = self.client.get(f'/finance/petty-cash/{self.spending_id}/edit?payee=Vendor&start_date=2026-11-01&end_date=2026-11-30')
        html = response.get_data(as_text=True)
        self.assertIn('value="300.00"', html)
        self.assertIn('value="Original purpose"', html)
        self.assertIn('value="fuel" selected', html)
        self.assertIn('payee=Vendor', html)
        response = self._post(f'/finance/petty-cash/{self.spending_id}/delete?payee=Vendor&export=pdf')
        self.assertIn('payee=Vendor', response.location)
        self.assertNotIn('export=', response.location)

    def test_missing_entries_return_404(self):
        self.assertEqual(self.client.get('/finance/petty-cash/9999/edit').status_code, 404)
        self.assertEqual(self._post('/finance/petty-cash/9999/delete').status_code, 404)

    def test_existing_create_flow_still_works(self):
        response = self._post('/finance/petty-cash', dict(date='2026-11-03', transaction_type='cash_out',
                                                        amount='50', purpose='New expense', expense_category='fuel'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PettyCashTransaction.query.count(), 3)
        self.assertEqual(finance._current_balance(), Decimal('650.00'))


if __name__ == '__main__':
    unittest.main()
