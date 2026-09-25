import io
import sys
import types
import unittest
from decimal import Decimal
from types import SimpleNamespace

from openpyxl import load_workbook

config_module = types.ModuleType("config")
config_module.Config = type("Config", (), {})
sys.modules.setdefault("config", config_module)

from app.routes.finance import _petty_cash_excel, _petty_cash_pdf


class PettyCashExportTests(unittest.TestCase):
    def setUp(self):
        transaction = SimpleNamespace(
            date="2026-09-25",
            voucher_number="PV-101",
            receipt_number="RC-7",
            transaction_type="cash_out",
            amount=Decimal("25000.00"),
            purpose="Office supplies",
            expense_category="office supplies",
            payee="Test Vendor",
            entered_by="cashier",
            approved_by="manager",
        )
        self.rows = [{
            "transaction": transaction,
            "previous_balance": Decimal("100000.00"),
            "amount_in": Decimal("0.00"),
            "amount_out": Decimal("25000.00"),
            "new_balance": Decimal("75000.00"),
        }]
        self.filters = {"start_date": "2026-09-01", "end_date": "2026-09-30"}

    def test_excel_export_is_a_formatted_xlsx_workbook(self):
        output = _petty_cash_excel(self.rows, self.filters)
        workbook = load_workbook(io.BytesIO(output.getvalue()))
        sheet = workbook["Petty Cash Ledger"]

        self.assertEqual(sheet["A1"].value, "Petty Cash Ledger")
        self.assertEqual(sheet["A5"].value, "2026-09-25")
        self.assertEqual(sheet["F5"].value, "Office supplies")
        self.assertEqual(sheet["N5"].value, 75000)
        self.assertEqual(sheet.freeze_panes, "A5")

    def test_pdf_export_is_a_downloadable_pdf(self):
        output = _petty_cash_pdf(
            self.rows,
            self.filters,
            {"current_balance": Decimal("75000.00")},
        )

        self.assertTrue(output.getvalue().startswith(b"%PDF"))
        self.assertGreater(len(output.getvalue()), 1000)


if __name__ == "__main__":
    unittest.main()
