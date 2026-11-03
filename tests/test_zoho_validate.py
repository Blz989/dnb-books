from migration.zoho.reader import read_export
from migration.zoho.validate import validate
from tests.zoho_sample import CUSTOMERS, VENDORS, build_sample_export, sample_rows

LINE = {"Item Name": "Blue Widget", "SKU": "WID-A", "Quantity": "1", "Item Price": "10.00"}


def _check(tmp_path, **kwargs):
    export = read_export(build_sample_export(tmp_path, **kwargs))
    return export, validate(export)


def _last_line(export, short):
    """(file name, file line) of the last data row in a short name's last file."""
    name, count = export.origins[short][-1]
    return name, count + 1


def _invoice(**over):
    return {"Invoice ID": "X1", "Invoice Number": "INV-X1", "Invoice Status": "Open",
            "Customer Name": CUSTOMERS[0], "Invoice Date": "2024-05-01", "Total": "10.00",
            "Item Total": "10.00", "Payment Terms Label": "Net 30", **LINE, **over}


def test_clean_sample_has_no_problems(tmp_path):
    assert _check(tmp_path)[1] == []


def test_unknown_customer(tmp_path):
    export, problems = _check(tmp_path, extra_rows={"invoices": [_invoice(**{"Customer Name": "Nobody Inc"})]})
    name, line = _last_line(export, "invoices")
    assert problems == [f'{name} row {line}: customer "Nobody Inc" is not in the export.']
    assert name == "Invoice01.csv"


def test_unknown_item_sku_then_name(tmp_path):
    rows = [
        _invoice(**{"Invoice ID": "X1", "Invoice Number": "INV-X1", "SKU": "NOPE-1"}),
        _invoice(**{"Invoice ID": "X2", "Invoice Number": "INV-X2", "SKU": "", "Item Name": "Ghost Item"}),
        _invoice(**{"Invoice ID": "X3", "Invoice Number": "INV-X3", "SKU": "", "Item Name": "Blue Widget"}),
    ]
    export, problems = _check(tmp_path, extra_rows={"invoices": rows})
    name, line = _last_line(export, "invoices")
    assert problems == [
        f'{name} row {line - 2}: item "NOPE-1" is not in the export.',
        f'{name} row {line - 1}: item "Ghost Item" is not in the export.',
    ]


def test_unknown_account(tmp_path):
    extra = {"expenses": [{"Expense Reference ID": "E9", "Expense Date": "2024-05-01",
                           "Expense Account": "Mystery Costs", "Paid Through": "BUS COMPLETE CHK",
                           "Expense Amount": "5.00", "Total": "5.00"}]}
    export, problems = _check(tmp_path, extra_rows=extra)
    name, line = _last_line(export, "expenses")
    assert problems == [f'{name} row {line}: account "Mystery Costs" is not in the export.']


def test_unknown_invoice_on_payment(tmp_path):
    extra = {"customer_payments": [{
        "CustomerPayment ID": "P9", "Payment Number": "P-9", "Customer Name": CUSTOMERS[0],
        "Deposit To": "BUS COMPLETE CHK", "Invoice Number": "INV-MISSING",
        "Amount Applied to Invoice": "1.00"}]}
    export, problems = _check(tmp_path, extra_rows=extra)
    name, line = _last_line(export, "customer_payments")
    assert problems == [f'{name} row {line}: invoice "INV-MISSING" is not in the export.']


def test_bill_of_other_vendor_on_payment(tmp_path):
    bill = sample_rows()["bills"][0]
    other = next(v for v in VENDORS if v != bill["Vendor Name"])
    extra = {"vendor_payments": [{
        "VendorPayment ID": "V9", "Payment Number": "VP-9", "Vendor Name": other,
        "Paid Through": "BUS COMPLETE CHK", "Bill Number": bill["Bill Number"]}]}
    export, problems = _check(tmp_path, extra_rows=extra)
    name, line = _last_line(export, "vendor_payments")
    assert problems == [
        f'{name} row {line}: bill "{bill["Bill Number"]}" is not a bill of vendor "{other}".'
    ]


def test_duplicate_invoice_number(tmp_path):
    existing = sample_rows()["invoices"][0]["Invoice Number"]
    export, problems = _check(tmp_path, extra_rows={"invoices": [_invoice(**{"Invoice Number": existing})]})
    name, line = _last_line(export, "invoices")
    assert problems == [f"{name} row {line}: number {existing} appears twice."]


def test_unsupported_account_type(tmp_path):
    extra = {"accounts": [{"Account ID": "A9", "Account Name": "Odd One", "Account Type": "Other Income"}]}
    export, problems = _check(tmp_path, extra_rows=extra)
    name, line = _last_line(export, "accounts")
    assert problems == [f'{name} row {line}: account type "Other Income" is not supported.']


def test_unsupported_payment_terms(tmp_path):
    extra = {"customers": [{"Contact ID": "C9", "Display Name": "Slow Payer", "Payment Terms Label": "Whenever"}]}
    export, problems = _check(tmp_path, extra_rows=extra)
    name, line = _last_line(export, "customers")
    assert problems == [f'{name} row {line}: payment terms "Whenever" is not supported.']


def test_second_receivable_account_is_reported(tmp_path):
    extra = {"accounts": [{"Account ID": "A9", "Account Name": "Receivables Two",
                           "Account Type": "Accounts Receivable"}]}
    _, problems = _check(tmp_path, extra_rows=extra)
    assert problems == [
        "Chart_of_Accounts.csv: Too many Zoho 'Accounts Receivable' accounts for the number range 1100-1100."
    ]


def test_all_problems_listed_at_once(tmp_path):
    extra = {
        "invoices": [_invoice(**{"Customer Name": "Nobody Inc"})],
        "customers": [{"Contact ID": "C9", "Display Name": "Slow Payer", "Payment Terms Label": "Whenever"}],
        "expenses": [{"Expense Reference ID": "E9", "Expense Account": "Mystery Costs",
                      "Paid Through": "BUS COMPLETE CHK"}],
    }
    _, problems = _check(tmp_path, extra_rows=extra)
    assert len(problems) == 3


def test_free_text_bill_line_with_an_account_is_an_expense_line(tmp_path):
    """Zoho's Cordon bills name each line "Shipping <date> <customer>" with no SKU and an expense account."""
    bill = dict(next(r for r in sample_rows()["bills"] if r["Bill Number"] == "BILL-005"))
    bill.update({"Bill ID": "X9", "Bill Number": "PW-77", "SKU": "",
                 "Item Name": "Shipping 2024-04-29 Kabayan Foods", "Account": "Shipping Expenses"})
    _, problems = _check(tmp_path, extra_rows={"bills": [bill]})
    assert problems == []


def test_credit_note_item_names_are_descriptions(tmp_path):
    note = dict(sample_rows()["credit_notes"][0])
    note.update({"CreditNotes ID": "X8", "Credit Note Number": "CN-X8", "Item Name": "Nagaraya Cracker Nuts-BBQ"})
    _, problems = _check(tmp_path, extra_rows={"credit_notes": [note]})
    assert problems == []
