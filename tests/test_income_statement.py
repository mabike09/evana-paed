import sys
import types
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from flask import Flask

# Keep these tests independent of deployment configuration and live databases.
config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.extensions import db
from app.models import (
    APBill,
    APBillLine,
    APPayment,
    APSupplier,
    ExpenseCategory,
    ExpenseEntry,
    Invoice,
    PettyCashTransaction,
)
from app.routes import finance


class IncomeStatementTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.sequence = 0
        self.supplier = APSupplier(supplier_name="Test supplier", category="utilities")
        db.session.add(self.supplier)
        db.session.flush()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _tracker(self, category_name, amount, on="2026-09-15"):
        category = ExpenseCategory.query.filter_by(name=category_name).first()
        if category is None:
            category = ExpenseCategory(name=category_name, created_by="test")
            db.session.add(category)
            db.session.flush()
        entry = ExpenseEntry(
            expense_date=on,
            category_id=category.id,
            description="Test expense",
            vendor_payee="Test vendor",
            amount=amount,
            entered_by="test",
        )
        db.session.add(entry)
        return entry

    def _bill(self, category, amount, lines=(), on="2026-09-15", status="unpaid"):
        self.sequence += 1
        bill = APBill(
            supplier_id=self.supplier.id,
            invoice_number=f"TEST-{self.sequence}",
            invoice_date=on,
            due_date="2026-10-15",
            expense_category=category,
            invoice_total=amount,
            submitted_by="test",
            submitted_date=on,
            created_by="test",
            status=status,
        )
        db.session.add(bill)
        db.session.flush()
        for line_category, line_amount in lines:
            db.session.add(APBillLine(
                bill_id=bill.id,
                line_description="Test line",
                expense_category=line_category,
                amount=line_amount,
            ))
        return bill

    def _petty(self, category, amount, on="2026-09-15", **values):
        txn = PettyCashTransaction(
            date=on,
            transaction_type=values.pop("transaction_type", "cash_out"),
            amount=amount,
            purpose="Test petty cash expense",
            expense_category=category,
            entered_by="test",
            **values,
        )
        db.session.add(txn)
        return txn

    def _invoice(self, amount, payer="Cash", on="2026-09-15"):
        self.sequence += 1
        db.session.add(Invoice(
            number=f"INV-{self.sequence}",
            patient_id=1,
            issue_date=on,
            amount=amount,
            payer_type=payer,
        ))

    def _statement(self, query="period=custom&start_date=2026-09-01&end_date=2026-09-30"):
        db.session.commit()
        with self.app.test_request_context("/finance/income-statement?" + query):
            with patch.object(finance, "eat_today", return_value=date(2026, 10, 26)):
                with patch.object(finance, "render_template", side_effect=lambda template, **context: context):
                    # Exercise the report with real queries without authentication wrappers.
                    return finance.income_statement.__wrapped__.__wrapped__()

    def test_tracker_direct_costs_are_not_left_in_operating_expenses(self):
        self._tracker("drug supplies", 60)
        self._tracker("Drugs & Medical Supplies", 450)
        self._tracker("laboratory supplies", 25)
        self._tracker("Rent", 2000)
        self._invoice(10000)

        result = self._statement()

        self.assertEqual(result["drug_cost"], Decimal("510.00"))
        self.assertEqual(result["lab_supplies_cost"], Decimal("25.00"))
        self.assertEqual(result["cost_of_service"], Decimal("535.00"))
        self.assertEqual(result["total_operating_expenses"], Decimal("2000.00"))
        self.assertEqual(result["gross_profit"], Decimal("9465.00"))
        self.assertEqual(result["net_profit"], Decimal("7465.00"))
        self.assertEqual(len(result["operating_expenses"]), 1)

    def test_categories_are_classified_consistently_across_all_sources(self):
        self._tracker("  DRUGS  &  Medical Supplies  ", 100)
        self._bill("utilities", 200, [(" Drugs AND Medical Supplies ", 200)])
        self._petty(" DRUG supplies ", 300)
        self._tracker(" LABORATORY supplies ", 10)
        self._bill("utilities", 20, [(" Lab Supplies ", 20)])
        self._petty("laboratory supplies", 30)

        result = self._statement()

        self.assertEqual(result["drug_cost"], Decimal("600.00"))
        self.assertEqual(result["lab_supplies_cost"], Decimal("60.00"))
        self.assertEqual(result["total_operating_expenses"], Decimal("0.00"))

    def test_header_only_supplier_bills_are_included(self):
        self._bill("Drugs & Medical Supplies", 300)
        self._bill("lab supplies", 200)
        self._bill("rent", 500)

        result = self._statement()

        self.assertEqual(result["drug_cost"], Decimal("300.00"))
        self.assertEqual(result["lab_supplies_cost"], Decimal("200.00"))
        self.assertEqual(result["total_operating_expenses"], Decimal("500.00"))
        self.assertEqual(result["net_profit"], Decimal("-1000.00"))

    def test_supplier_bill_lines_and_payments_are_not_counted_twice(self):
        for status in ("paid", "unpaid"):
            bill = self._bill("rent", 600, [
                ("drug supplies", 100), ("lab supplies", 200), ("rent", 300),
            ], status=status)
            db.session.add(APPayment(
                bill_id=bill.id,
                payment_date="2026-09-20",
                amount=600,
                method="cash",
                processed_by="test",
            ))

        result = self._statement()

        self.assertEqual(result["drug_cost"], Decimal("200.00"))
        self.assertEqual(result["lab_supplies_cost"], Decimal("400.00"))
        self.assertEqual(result["total_operating_expenses"], Decimal("600.00"))
        self.assertEqual(result["net_profit"], Decimal("-1200.00"))

    def test_petty_cash_in_and_advances_are_not_treated_as_spending(self):
        self._petty("fuel", 150)
        self._petty("drug supplies", 20)
        self._petty("lab supplies", 30)
        self._petty("fuel", 1000, transaction_type="cash_in", action_mode="top_up")
        self._petty("fuel", 100, transaction_type="cash_in", action_mode="cash_returned")
        self._petty("fuel", 400, is_advance=True, is_accounted=False)
        self._petty("fuel", 500, is_advance=True, is_accounted=True)

        result = self._statement()

        self.assertEqual(result["drug_cost"], Decimal("20.00"))
        self.assertEqual(result["lab_supplies_cost"], Decimal("30.00"))
        self.assertEqual(result["total_operating_expenses"], Decimal("150.00"))
        self.assertEqual(result["net_profit"], Decimal("-200.00"))

    def test_custom_dates_include_boundaries_and_exclude_adjacent_days(self):
        for on, amount in (("2026-08-31", 1000), ("2026-09-01", 10),
                           ("2026-09-30", 20), ("2026-10-01", 2000)):
            self._tracker("rent", amount, on=on)
            self._bill("drug supplies", amount, on=on)
            self._petty("lab supplies", amount, on=on)
            self._invoice(amount, on=on)

        result = self._statement()

        self.assertEqual(result["total_revenue"], Decimal("30.00"))
        self.assertEqual(result["drug_cost"], Decimal("30.00"))
        self.assertEqual(result["lab_supplies_cost"], Decimal("30.00"))
        self.assertEqual(result["total_operating_expenses"], Decimal("30.00"))

    def test_last_month_uses_preset_instead_of_stale_date_fields(self):
        self._invoice(100, on="2026-09-30")
        self._invoice(999, on="2026-10-01")

        result = self._statement("period=last_month&start_date=2026-10-01&end_date=2026-10-26")

        self.assertEqual(result["period"], "last_month")
        self.assertEqual(result["start_date"], date(2026, 9, 1))
        self.assertEqual(result["end_date"], date(2026, 9, 30))
        self.assertEqual(result["total_revenue"], Decimal("100.00"))

    def test_reversed_custom_range_is_normalized(self):
        self._tracker("rent", 40)

        result = self._statement("period=custom&start_date=2026-09-30&end_date=2026-09-01")

        self.assertEqual(result["start_date"], date(2026, 9, 1))
        self.assertEqual(result["end_date"], date(2026, 9, 30))
        self.assertEqual(result["total_operating_expenses"], Decimal("40.00"))

    def test_cash_and_insurance_revenue_and_margins(self):
        self._invoice(800, payer="Cash")
        self._invoice(200, payer="Insurance")
        self._tracker("drug supplies", 100)
        self._tracker("rent", 200)

        result = self._statement()

        self.assertEqual(result["cash_revenue"], Decimal("800.00"))
        self.assertEqual(result["insurance_revenue"], Decimal("200.00"))
        self.assertEqual(result["total_revenue"], Decimal("1000.00"))
        self.assertEqual(result["gross_profit_margin"], Decimal("90.00"))
        self.assertEqual(result["net_income_margin"], Decimal("70.00"))

    def test_no_revenue_has_zero_margins_without_division_error(self):
        self._tracker("rent", 200)

        result = self._statement()

        self.assertEqual(result["total_revenue"], Decimal("0.00"))
        self.assertEqual(result["net_profit"], Decimal("-200.00"))
        self.assertEqual(result["gross_profit_margin"], Decimal("0.00"))
        self.assertEqual(result["net_income_margin"], Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
