import dataclasses
from datetime import date
from decimal import Decimal

import pytest

from items.stock import check_invariants, on_hand
from ledger.models import JournalLine
from migration.models import ZohoRecord
from migration.zoho.reader import read_export
from migration.zoho.replay import Replay
from purchasing.checks import ap_check
from purchasing.models import Bill, PurchaseOrder
from sales.balances import invoice_total
from sales.checks import ar_check
from sales.models import Invoice, OrderStatus, SalesOrder
from tests.zoho_sample import EXPECTED, build_sample_export

DOC_KINDS = ("sales_order", "purchase_order", "bill", "invoice")


def _replay(admin_user, tmp_path, extra_rows=None):
    r = Replay(read_export(build_sample_export(tmp_path, extra_rows=extra_rows)), admin_user)
    r.load_master()
    for event in r.events(kinds=DOC_KINDS):
        r.post(event)
    return r


@pytest.fixture
def replay(db, admin_user, tmp_path):
    return _replay(admin_user, tmp_path)


def _invoice_row(number, zid, item, sku, qty, price, item_total, total, *, adjustment="0.0",
                 adjustment_account=""):
    return {
        "Invoice ID": zid, "Invoice Date": "2026-03-01", "Invoice Number": number,
        "Invoice Status": "Sent", "Customer Name": "Cedar Ridge Outfitters", "Sales Order Number": "",
        "Item Name": item, "SKU": sku, "Item Desc": "", "Quantity": str(qty), "Item Price": price,
        "Item Total": item_total, "Total": total, "Shipping Charge": "0.000",
        "Shipping Charge Account": "", "Adjustment": adjustment,
        "Adjustment Account": adjustment_account, "Item Tax Amount": "0.00", "Notes": "",
        "Payment Terms Label": "Due on Receipt",
    }


def _lines(invoice):
    """Lines by sku; ADJ-/SHIP- copies that post to a named Zoho account are keyed ADJ and SHIP."""
    out = {}
    for l in invoice.lines.select_related("item"):
        out[l.item.sku.split("-")[0] if l.item.sku.startswith(("ADJ-", "SHIP-")) else l.item.sku] = l
    return out


def _entry(doc):
    lines = JournalLine.objects.in_ledger().filter(entry=doc.journal_entry).select_related("account")
    return {l.account.name: l.debit - l.credit for l in lines}


def test_orders_keep_zoho_numbers_and_void_closed(replay):
    numbers = set(SalesOrder.objects.values_list("number", flat=True))
    assert numbers == {f"SO-000{n}" for n in range(1, 9)}
    void = SalesOrder.objects.get(number="SO-0008")
    assert void.status == OrderStatus.CLOSED and "Void in Zoho" in void.notes
    assert void.closed_by == replay.user
    assert not void.invoices.exists()
    discounted = SalesOrder.objects.get(number="SO-0002").lines.get(item__sku="WID-A")
    assert discounted.price == Decimal("23.7500")  # 712.50 / 30
    pos = PurchaseOrder.objects.order_by("number")
    assert [p.number for p in pos] == ["PO-0001", "PO-0002", "PO-0003"]
    assert all(p.status == OrderStatus.CLOSED for p in pos)
    assert ZohoRecord.objects.get(kind="sales_order", zoho_id="50000000008").target == void
    assert ZohoRecord.objects.filter(kind="purchase_order").count() == 3


def test_invoice_totals_equal_zoho(replay):
    totals = {i.number: invoice_total(i) for i in Invoice.objects.all()}
    assert totals == EXPECTED["invoice_totals"]
    assert ZohoRecord.objects.filter(kind="invoice").count() == len(EXPECTED["invoice_totals"])
    assert not [w for w in replay.warnings if "vs Zoho" in w]
    inv6 = Invoice.objects.get(number="INV-000006")
    adj = _lines(inv6)["ADJ"]
    assert adj.amount == Decimal("-0.50") and adj.price == Decimal("-0.50")
    # Zoho's Adjustment Account is honored: the negative adjustment debits Other Charges.
    assert adj.item.income_account.name == "Other Charges"
    assert _entry(inv6) == {"Accounts Receivable": Decimal("599.50"), "Sales": Decimal("-600.00"),
                            "Other Charges": Decimal("0.50")}


def test_discount_leftover_goes_on_adjustment_line(db, admin_user, tmp_path):
    # 300 x 0.3333 = 99.99, so one cent is left over from Zoho's Item Total of 100.00.
    up = _invoice_row("INV-000010", "51000000010", "Gift Wrap", "WRAP", 300, "5.00", "100.00", "100.00")
    # 300 x 0.3333 = 99.99 against 99.98: one cent too many, so the adjustment line takes -0.01
    # on top of Zoho's own adjustment of 0.50.
    down = _invoice_row("INV-000011", "51000000011", "Gift Wrap", "WRAP", 300, "5.00", "99.98", "100.48",
                        adjustment="0.50", adjustment_account="Other Charges")
    r = _replay(admin_user, tmp_path, extra_rows={"invoices": [up, down]})
    first = Invoice.objects.get(number="INV-000010")
    lines = _lines(first)
    assert lines["WRAP"].price == Decimal("0.3333") and lines["WRAP"].amount == Decimal("99.99")
    assert lines["ADJ"].quantity == 1 and lines["ADJ"].amount == Decimal("0.01")
    assert lines["ADJ"].item == r.items["ADJ"]  # blank Adjustment Account: the default ADJ item (Sales)
    assert invoice_total(first) == Decimal("100.00")
    second = Invoice.objects.get(number="INV-000011")
    assert _lines(second)["ADJ"].amount == Decimal("0.49")
    # Same account as INV-000006's adjustment: the one ADJ copy for Other Charges is reused.
    assert _lines(second)["ADJ"].item == _lines(Invoice.objects.get(number="INV-000006"))["ADJ"].item
    assert ZohoRecord.objects.filter(kind="service_item").count() == 1
    assert invoice_total(second) == Decimal("100.48")
    # The discounted sample invoice needs no adjustment: 712.50 / 30 is exactly 23.75.
    inv2 = _lines(Invoice.objects.get(number="INV-000002"))
    assert "ADJ" not in inv2 and inv2["WID-A"].price == Decimal("23.7500")
    assert inv2["WID-A"].amount == Decimal("712.50")
    assert not [w for w in r.warnings if "vs Zoho" in w]


def test_shipping_and_tax_lines(replay):
    shipped = _lines(Invoice.objects.get(number="INV-000007"))
    assert shipped["SHIP"].amount == Decimal("45.00") and shipped["SHIP"].quantity == 1
    taxed = Invoice.objects.get(number="INV-000008")
    assert _lines(taxed)["TAX"].amount == Decimal("21.00")
    credits = {l.account.name: l.credit for l in JournalLine.objects.in_ledger().filter(entry=taxed.journal_entry)}
    assert credits["Tax Payable"] == Decimal("21.00") and credits["Sales"] == Decimal("300.00")
    ship_entry = Invoice.objects.get(number="INV-000007").journal_entry
    ship_credits = {l.account.name: l.credit for l in JournalLine.objects.in_ledger().filter(entry=ship_entry)}
    assert ship_credits["Shipping Charge"] == Decimal("45.00")


def test_two_invoices_against_one_order(replay):
    order = SalesOrder.objects.get(number="SO-0004")
    line = order.lines.get()
    a, b = Invoice.objects.get(number="INV-000004"), Invoice.objects.get(number="INV-000005")
    assert a.sales_order == order and b.sales_order == order
    assert a.lines.get().so_line == line and b.lines.get().so_line == line
    assert order.status == OrderStatus.CLOSED  # fully invoiced, closed by the invoices
    assert order.closed_by is None


def test_bill_stock_in_at_rate_and_expense_lines(replay):
    bill = Bill.objects.get(vendor_invoice_number="BILL-002")
    assert bill.number.startswith("BILL-0000")
    lines = {l.item.sku: l for l in bill.item_lines.select_related("item", "stock_movement")}
    assert lines["BOLT-D"].price == Decimal("3.0000") and lines["BOLT-D"].amount == Decimal("300.00")
    assert lines["GAD-C"].stock_movement.unit_cost == Decimal("28.0000")
    assert lines["GAD-C"].stock_movement.quantity == 30
    assert bill.notes == "Imported from Zoho (ID 53000000002)"
    totals = {b.vendor_invoice_number: sum((l.amount for l in [*b.item_lines.all(), *b.expense_lines.all()]),
                                           Decimal("0")) for b in Bill.objects.all()}
    assert totals == EXPECTED["bill_totals"]
    assert ZohoRecord.objects.filter(kind="bill").count() == 5


def test_cordon_bill_on_shipping_expenses_with_order_note(replay):
    cordon = Bill.objects.get(vendor_invoice_number="BILL-005")
    expense = cordon.expense_lines.get()
    assert expense.account.name == "Shipping Expenses" and expense.amount == Decimal("45.00")
    assert expense.description == "Shipping for Order SO-0006"
    assert not cordon.item_lines.exists()
    assert cordon.due_date == cordon.date  # Due on Receipt
    drop = Bill.objects.get(vendor_invoice_number="BILL-004")
    assert drop.notes == "Imported from Zoho (ID 53000000004)\nSales order SO-0007"


def test_negative_stock_allowed_and_warned(replay):
    sku = EXPECTED["negative_item_sku"]
    item = replay.items[sku]
    assert on_hand(item) == 80  # -20 on 2024-03-02, then +100 on 2024-08-01
    warnings = [w for w in replay.warnings if w.startswith(f"{sku}:")]
    assert warnings == [f"{sku}: stock went below zero on 2024-03-02 (INV-000001)."]
    assert replay.negative_items == {sku}


def test_event_order_within_a_day(replay):
    events = replay.events(kinds=DOC_KINDS)
    same_day = [e for e in events if e.date == date(2025, 6, 2)]
    assert [(e.kind, e.number) for e in same_day] == [("bill", "BILL-004"), ("invoice", "INV-000008")]
    assert events == sorted(events, key=lambda e: (e.date, e.rank, e.number))
    bill = Bill.objects.get(vendor_invoice_number="BILL-004")
    invoice = Invoice.objects.get(number="INV-000008")
    assert bill.pk and invoice.pk
    assert bill.item_lines.get().stock_movement.id < invoice.lines.get(item__sku="WID-B").stock_movement.id
    # Every kind in the rank table is built from the start; later tasks only add handlers.
    kinds = {e.kind for e in replay.events()}
    assert kinds >= {"customer_payment", "vendor_payment", "undeposited_transfer", "transfer", "adjustment"}
    ranks = {e.kind: e.rank for e in replay.events() if e.kind != "adjustment"}
    assert ranks["undeposited_transfer"] == 5 and ranks["expense"] == 6
    adjustments = {e.zoho_id: e.rank for e in replay.events(kinds=("adjustment",))}
    assert adjustments == {"60000000001": 7, "60000000002": 7}
    # Every kind now has a handler (Task 6); a kind without one is still refused.
    expense = next(e for e in replay.events() if e.kind == "expense")
    with pytest.raises(NotImplementedError):
        replay.post(dataclasses.replace(expense, kind="unknown"))


def test_ledger_checks_zero_after_docs(replay):
    assert ar_check() == []
    assert ap_check() == []
    assert check_invariants() == []


def test_negative_leftover_only_goes_on_adjustment_line(db, admin_user, tmp_path):
    # 300 x 0.3333 = 99.99 against 99.98, and no Zoho adjustment: the ADJ line is -0.01.
    row = _invoice_row("INV-000012", "51000000012", "Gift Wrap", "WRAP", 300, "5.00", "99.98", "99.98")
    _replay(admin_user, tmp_path, extra_rows={"invoices": [row]})
    invoice = Invoice.objects.get(number="INV-000012")
    assert _lines(invoice)["ADJ"].amount == Decimal("-0.01")
    assert invoice_total(invoice) == Decimal("99.98")
    assert _entry(invoice) == {"Accounts Receivable": Decimal("99.98"), "Sales": Decimal("-99.98")}
    assert ar_check() == []


def test_bill_negative_adjustment_on_adjustment_account(db, admin_user, tmp_path):
    from tests.zoho_sample import sample_rows
    row = dict(next(r for r in sample_rows()["bills"] if r["Bill Number"] == "BILL-003"))
    # 300 x 0.3333 = 99.99 against 99.98: -0.01 leftover, plus Zoho's -5.00 adjustment.
    row.update({"Bill ID": "53000000009", "Bill Number": "PW-9", "Bill Date": "2025-04-01",
                "Quantity": "300", "Rate": "0.33", "Item Total": "99.98", "Total": "94.98", "Adjustment": "-5.00",
                "Adjustment Account": "Discount", "Purchase Order Number": ""})
    r = _replay(admin_user, tmp_path, extra_rows={"bills": [row]})
    bill = Bill.objects.get(vendor_invoice_number="PW-9")
    expense = bill.expense_lines.get()
    assert (expense.account.name, expense.amount) == ("Discount", Decimal("-5.01"))
    assert bill.item_lines.get().amount == Decimal("99.99")
    # Stock posts through its own movement entry; the bill entry carries only the adjustment, reversed.
    assert _entry(bill) == {"Discount": Decimal("-5.01"), "Accounts Payable": Decimal("5.01")}
    from purchasing.balances import bill_total
    assert bill_total(bill) == Decimal("94.98")
    assert not [w for w in r.warnings if "PW-9" in w]
    assert ap_check() == [] and check_invariants() == []


def test_void_invoice_created_then_voided(db, admin_user, tmp_path):
    row = _invoice_row("INV-000013", "51000000013", "Gift Wrap", "WRAP", 2, "5.00", "10.00", "10.00")
    row["Invoice Status"] = "Void"
    _replay(admin_user, tmp_path, extra_rows={"invoices": [row]})
    invoice = Invoice.objects.get(number="INV-000013")
    assert invoice.status == "VOID" and invoice.void_reason == "Void in Zoho"
    assert invoice.voided_on == date(2026, 3, 1)
    assert ZohoRecord.objects.get(kind="invoice", zoho_id="51000000013").target == invoice
    receivable = JournalLine.objects.in_ledger().filter(
        entry__source_id="INV-000013", account__name="Accounts Receivable")
    assert sum((l.debit - l.credit for l in receivable), Decimal("0")) == Decimal("0")
    assert ar_check() == []


def test_shipping_on_named_account(db, admin_user, tmp_path):
    row = _invoice_row("INV-000014", "51000000014", "Gift Wrap", "WRAP", 2, "5.00", "10.00", "17.00")
    row.update({"Shipping Charge": "7.00", "Shipping Charge Account": "Other Charges"})
    r = _replay(admin_user, tmp_path, extra_rows={"invoices": [row]})
    ship = _lines(Invoice.objects.get(number="INV-000014"))["SHIP"]
    assert ship.item.name == "Shipping charge (Other Charges)" and ship.amount == Decimal("7.00")
    assert ship.item.income_account.name == "Other Charges" and not ship.item.is_stock
    # The sample's own shipping names the default account, so it uses the default SHIP item.
    assert _lines(Invoice.objects.get(number="INV-000007"))["SHIP"].item == r.items["SHIP"]


# ---- allow_negative_adjustment on the existing services (import-only deviation, ruling 7) ----
@pytest.fixture
def master(db, admin_user, tmp_path):
    r = Replay(read_export(build_sample_export(tmp_path)), admin_user)
    r.load_master()
    return r


def _invoice_lines(r, adj_price):
    from sales.posting.invoices import InvoiceLineSpec
    return [InvoiceLineSpec(r.items["WRAP"], 2, Decimal("5.00")), InvoiceLineSpec(r.items["ADJ"], 1, adj_price)]


def _one_line(r, sku, price):
    from sales.posting.invoices import InvoiceLineSpec
    return InvoiceLineSpec(r.items[sku], 1, price)


def test_negative_invoice_price_refused_by_default(master):
    from sales.errors import InvoiceError
    from sales.posting.invoices import InvoiceLineSpec, create_invoice
    customer = master.customers_by_name["Acme Hardware"]
    with pytest.raises(InvoiceError, match=r"^Line 2: price can't be negative\.$"):
        create_invoice(customer=customer, date=date(2026, 3, 1), lines=_invoice_lines(master, Decimal("-1.00")),
                       user=master.user)
    # Even with the flag, a stock line still can't be negative.
    with pytest.raises(InvoiceError, match=r"^Line 1: price can't be negative\.$"):
        create_invoice(customer=customer, date=date(2026, 3, 1), user=master.user, allow_negative_adjustment=True,
                       lines=[InvoiceLineSpec(master.items["WID-A"], 1, Decimal("-1.00"))], allow_negative=True)
    assert not Invoice.objects.exists()


def test_negative_invoice_price_allowed_with_flag(master):
    from sales.posting.invoices import create_invoice
    invoice = create_invoice(
        customer=master.customers_by_name["Acme Hardware"], date=date(2026, 3, 1), user=master.user,
        lines=_invoice_lines(master, Decimal("-1.00")), allow_negative_adjustment=True)
    assert invoice_total(invoice) == Decimal("9.00")
    assert _entry(invoice) == {"Accounts Receivable": Decimal("9.00"), "Sales": Decimal("-9.00")}
    other = create_invoice(
        customer=master.customers_by_name["Acme Hardware"], date=date(2026, 3, 1), user=master.user,
        lines=[*_invoice_lines(master, Decimal("0.00"))[:1],
               _one_line(master, "TAX", Decimal("1.00")),
               _one_line(master, "SHIP", Decimal("-2.00"))],
        allow_negative_adjustment=True)
    assert _entry(other) == {"Accounts Receivable": Decimal("9.00"), "Sales": Decimal("-10.00"),
                             "Tax Payable": Decimal("-1.00"), "Shipping Charge": Decimal("2.00")}
    assert ar_check() == [] and check_invariants() == []


def test_negative_bill_expense_refused_by_default_allowed_with_flag(master):
    from purchasing.errors import PurchasingError
    from purchasing.posting.bills import BillExpenseSpec, create_bill
    vendor, supplies = master.vendors["Summit Packaging"], master.accounts["Office Supplies"]
    lines = [BillExpenseSpec(supplies, Decimal("20.00"), "Boxes"),
             BillExpenseSpec(master.accounts["Discount"], Decimal("-1.50"), "Adjustment")]
    with pytest.raises(PurchasingError, match=r"^Line 2: enter an amount above zero\.$"):
        create_bill(vendor=vendor, date=date(2026, 3, 1), vendor_invoice_number="S-1", expense_lines=lines,
                    user=master.user)
    with pytest.raises(PurchasingError, match=r"^Line 1: enter an amount above zero\.$"):
        create_bill(vendor=vendor, date=date(2026, 3, 1), vendor_invoice_number="S-1", user=master.user,
                    expense_lines=[BillExpenseSpec(supplies, Decimal("0.00"))], allow_negative_adjustment=True)
    assert not Bill.objects.exists()
    bill = create_bill(vendor=vendor, date=date(2026, 3, 1), vendor_invoice_number="S-1", expense_lines=lines,
                       user=master.user, allow_negative_adjustment=True)
    assert _entry(bill) == {"Office Supplies": Decimal("20.00"), "Discount": Decimal("-1.50"),
                            "Accounts Payable": Decimal("-18.50")}
    assert ap_check() == []


def test_free_text_bill_line_becomes_an_expense_line(db, admin_user, tmp_path):
    from tests.zoho_sample import sample_rows

    row = dict(next(r for r in sample_rows()["bills"] if r["Bill Number"] == "BILL-005"))
    row.update({"Bill ID": "53000000077", "Bill Number": "PW-77", "SKU": "", "Description": "",
                "Item Name": "Shipping 2024-04-29 Kabayan Foods", "Account": "Shipping Expenses",
                "Payment Terms Label": "Custom", "Bill Date": "2025-05-20", "Due Date": "2025-06-19"})
    _replay(admin_user, tmp_path, extra_rows={"bills": [row]})
    bill = Bill.objects.get(vendor_invoice_number="PW-77")
    expense = bill.expense_lines.get()
    assert (expense.account.name, expense.description) == ("Shipping Expenses", "Shipping 2024-04-29 Kabayan Foods")
    assert not bill.item_lines.exists()
    assert bill.terms == "NET_30"


def test_same_name_customers_stay_apart_and_invoices_follow_the_contact_id(db, admin_user, tmp_path):
    from contacts.models import Customer
    from tests.zoho_sample import sample_rows

    twin = dict(sample_rows()["customers"][2])
    twin.update({"Contact ID": "4800000009999", "Display Name": "cedar ridge  Outfitters", "EmailID": "x@y.example"})
    invoice = _invoice_row("INV-T1", "61000000999", "Blue Widget", "WID-A", 1, "12.00", "12.00", "12.00")
    invoice["Customer ID"] = "4800000009999"
    r = _replay(admin_user, tmp_path, extra_rows={"customers": [twin], "invoices": [invoice]})
    second = Customer.objects.get(name="cedar ridge Outfitters (2)")
    assert Invoice.objects.get(number="INV-T1").customer == second
    assert Customer.objects.filter(name="Cedar Ridge Outfitters").exists()
    assert any("share a name" in w for w in r.warnings)


def test_item_less_lines_zero_skipped_and_shipping_kept(db, admin_user, tmp_path):
    """Zoho's Shopify orders carry $0.00 placeholder lines and shipping charges booked straight to Sales."""
    base = _invoice_row("INV-S1", "61000000888", "Blue Widget", "WID-A", 1, "12.00", "12.00", "17.99")
    blank = {**base, "Item Name": "", "SKU": "", "Quantity": "1", "Item Price": "0.00", "Item Total": "0.00",
             "Item Desc": ""}
    ship = {**base, "Item Name": "", "SKU": "", "Quantity": "1", "Item Price": "5.99", "Item Total": "5.99",
            "Item Desc": "Shipping Charges : Standard", "Account": "Sales"}
    _replay(admin_user, tmp_path, extra_rows={"invoices": [base, blank, ship]})
    invoice = Invoice.objects.get(number="INV-S1")
    assert invoice_total(invoice) == Decimal("17.99")
    shipping = invoice.lines.get(description="Shipping Charges : Standard")
    assert (shipping.amount, shipping.item.income_account.name) == (Decimal("5.99"), "Sales")
    assert invoice.lines.count() == 2


def test_free_goods_on_a_bill_become_a_zero_cost_adjustment(db, admin_user, tmp_path):
    from items.models import StockMovement
    from tests.zoho_sample import sample_rows

    base = dict(next(r for r in sample_rows()["bills"] if r["Bill Number"] == "BILL-003"))
    paid = {**base, "Bill ID": "53000000088", "Bill Number": "FREE-1", "Bill Date": "2025-04-02",
            "Quantity": "5", "Rate": "12.00", "Item Total": "60.00", "Total": "60.00", "Balance": "60.00"}
    free = {**paid, "Quantity": "40", "Rate": "0.00", "Item Total": "0.00"}
    all_free = {**free, "Bill ID": "53000000089", "Bill Number": "FREE-2", "Total": "0.00", "Balance": "0.00"}
    r = _replay(admin_user, tmp_path, extra_rows={"bills": [paid, free, all_free]})
    bill = Bill.objects.get(vendor_invoice_number="FREE-1")
    assert [l.quantity for l in bill.item_lines.all()] == [5]
    moves = StockMovement.objects.filter(source_type="migration.free_goods")
    assert sorted(m.quantity for m in moves) == [40, 40]
    assert all(m.total_cost == 0 and m.journal_entry_id is None for m in moves)
    assert not Bill.objects.filter(vendor_invoice_number="FREE-2").exists()
    assert any("FREE-2" in w and "$0.00" in w for w in r.warnings)
    assert check_invariants() == []


def test_empty_zero_invoice_is_skipped_and_reported(db, admin_user, tmp_path):
    base = _invoice_row("INV-E1", "61000000777", "", "", 1, "0.00", "0.00", "0.00")
    r = _replay(admin_user, tmp_path, extra_rows={"invoices": [base]})
    assert not Invoice.objects.filter(number="INV-E1").exists()
    assert r.skipped["invoices"] == {"61000000777"}
    assert any("INV-E1" in w for w in r.warnings)
    assert r.finish().skipped["invoices"] == {"61000000777"}
