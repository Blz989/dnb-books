import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from core.report_catalog import catalog
from items.lots import NO_INVOICE, SOLD_BEFORE, STILL_ESTIMATED, fifo_lots
from items.models import CostingMethod, FifoLayer
from items.reports import inventory_valuation
from purchasing.posting.bills import BillItemSpec, create_bill
from sales.posting.invoices import InvoiceLineSpec, create_invoice, void_invoice

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    return purchasing_settings


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", purchase_cost="1.0000")


def bill(vendor, user, item, qty, price, day, ref):
    return create_bill(vendor=vendor, date=day, vendor_invoice_number=ref, user=user,
                       item_lines=[BillItemSpec(item, qty, D(price))])


def invoice(customer, user, item, qty, day, **kw):
    return create_invoice(customer=customer, date=day, user=user, lines=[InvoiceLineSpec(item, qty, D("9.00"))], **kw)


@pytest.fixture
def traded(widget, vendor, customer, admin_user):
    b1 = bill(vendor, admin_user, widget, 10, "2.00", d(3, 1), "V1")
    b2 = bill(vendor, admin_user, widget, 10, "3.00", d(3, 5), "V2")
    i1 = invoice(customer, admin_user, widget, 4, d(3, 10))   # lot 1: 4
    i2 = invoice(customer, admin_user, widget, 8, d(3, 12))   # lot 1: 6, lot 2: 2
    i3 = invoice(customer, admin_user, widget, 3, d(3, 15))   # lot 2: 3
    return {"b1": b1, "b2": b2, "i1": i1, "i2": i2, "i3": i3}


def test_rows_pair_each_lot_with_the_invoices_that_used_it(traded, widget, customer, vendor):
    r = fifo_lots(widget, d(3, 1), d(3, 31))
    got = [(row.lot.label, row.first, row.out_label, row.out_quantity) for row in r.rows]
    b1, b2 = traded["b1"].number, traded["b2"].number
    i1, i2, i3 = traded["i1"].number, traded["i2"].number, traded["i3"].number
    assert got == [
        (f"Bill {b1}", True, f"Invoice {i1}", 4), (f"Bill {b1}", False, f"Invoice {i2}", 6),
        (f"Bill {b2}", True, f"Invoice {i2}", 2), (f"Bill {b2}", False, f"Invoice {i3}", 3),
    ]
    lot1, lot2 = r.lots
    assert lot1.party == vendor.name and lot1.unit_cost == D("2.0000") and lot1.total == D("20.00")
    assert lot1.url == reverse("purchasing:bill-detail", args=[b1])
    assert r.rows[0].out_url == reverse("sales:invoice-detail", args=[i1]) and r.rows[0].out_party == customer.name
    assert lot1.remaining_quantity == 0 and lot1.age_days == 11          # Mar 1 to Mar 12, when it ran out
    assert lot2.remaining_quantity == 5 and lot2.age_days == 26          # Mar 5 to Mar 31
    assert (r.received, r.sold) == (20, 15)


def test_closing_matches_inventory_valuation_and_fifo_layers(traded, widget):
    r = fifo_lots(widget, d(3, 1), d(3, 31))
    (row,) = [v for v in inventory_valuation(d(3, 31)).rows if v.item == widget]
    assert (r.on_hand, r.value) == (row.quantity, row.value) == (5, D("15.00"))
    assert (r.lots_quantity, r.lots_value, r.difference) == (5, D("15.00"), D("0.00"))
    layers = FifoLayer.objects.filter(item=widget)
    assert sum(l.remaining_quantity for l in layers) == r.lots_quantity
    assert sum(l.remaining_value for l in layers) == r.lots_value


def test_bill_with_no_invoice_is_flagged(widget, vendor, admin_user):
    bill(vendor, admin_user, widget, 6, "2.00", d(4, 1), "V9")
    (row,) = fifo_lots(widget, d(4, 1), d(4, 30)).rows
    assert row.tag == NO_INVOICE and row.out_quantity is None


def test_sold_before_received_then_filled(widget, vendor, customer, admin_user):
    inv = invoice(customer, admin_user, widget, 3, d(5, 1), allow_negative=True)
    b = bill(vendor, admin_user, widget, 5, "2.00", d(5, 4), "V3")
    r = fifo_lots(widget, d(5, 1), d(5, 31))
    (short,) = r.shorts
    assert short.label == f"Invoice {inv.number}" and short.quantity == 3 and short.filled
    assert short.resolved == f"Filled by Bill {b.number} on 2026-05-04"
    (row,) = r.rows
    assert row.tag == SOLD_BEFORE and row.out_quantity == 3 and row.lot.remaining_quantity == 2
    assert r.difference == D("0.00")


def test_still_estimated(widget, customer, admin_user):
    invoice(customer, admin_user, widget, 2, d(6, 1), allow_negative=True)
    r = fifo_lots(widget, d(6, 1), d(6, 30))
    (short,) = r.shorts
    assert short.resolved == STILL_ESTIMATED and not short.filled and short.estimate == D("2.00")
    assert r.on_hand == -2 and r.difference == D("0.00")


def test_opening_lot_from_before_the_range(widget, vendor, customer, admin_user):
    b = bill(vendor, admin_user, widget, 10, "2.00", d(1, 5), "V4")
    invoice(customer, admin_user, widget, 4, d(1, 20))
    later = invoice(customer, admin_user, widget, 3, d(2, 10))
    r = fifo_lots(widget, d(2, 1), d(2, 28))
    (lot,) = r.lots
    assert lot.opening and lot.on_hand_at_start == 6 and lot.used_before_start == 4
    (row,) = r.rows
    assert row.out_label == f"Invoice {later.number}" and row.out_quantity == 3
    assert (r.received, r.sold) == (0, 3)


def test_lot_used_up_before_the_range_is_not_shown(widget, vendor, customer, admin_user):
    bill(vendor, admin_user, widget, 2, "2.00", d(1, 5), "V5")
    invoice(customer, admin_user, widget, 2, d(1, 20))
    assert fifo_lots(widget, d(2, 1), d(2, 28)).lots == []


def test_voided_invoice_is_left_out(traded, widget, admin_user):
    void_invoice(traded["i3"], user=admin_user, reason="wrong item")
    r = fifo_lots(widget, d(3, 1), d(3, 31))
    assert all(row.out_label != f"Invoice {traded['i3'].number}" for row in r.rows)
    assert r.on_hand == 8 and r.difference == D("0.00")


def test_query_count_does_not_grow_with_invoices(widget, vendor, customer, admin_user, django_assert_max_num_queries):
    bill(vendor, admin_user, widget, 100, "2.00", d(3, 1), "V6")
    for day in range(2, 4):
        invoice(customer, admin_user, widget, 1, d(3, day))
    with django_assert_max_num_queries(30) as small:
        fifo_lots(widget, d(3, 1), d(3, 31))
    for day in range(4, 24):
        invoice(customer, admin_user, widget, 1, d(3, day))
    with django_assert_max_num_queries(len(small.captured_queries)):
        fifo_lots(widget, d(3, 1), d(3, 31))


# ---- view ----

def test_page_needs_an_item(client, admin_user, widget):
    client.force_login(admin_user)
    page = client.get(reverse("reports:fifo-lots")).content.decode()
    assert "Pick an item to trace its cost lots." in page


def test_non_fifo_item_is_refused(client, admin_user, setup, make_item):
    avg = make_item("AVG", method=CostingMethod.WEIGHTED_AVERAGE)
    client.force_login(admin_user)
    page = client.get(reverse("reports:fifo-lots"), {"item": avg.pk}).content.decode()
    assert "Lot tracking needs an item that uses FIFO costing." in page


def test_page_csv_pdf_and_catalog(client, admin_user, traded, widget):
    client.force_login(admin_user)
    url = reverse("reports:fifo-lots")
    params = {"item": widget.pk, "start": "2026-03-01", "end": "2026-03-31"}
    page = client.get(url, params).content.decode()
    assert "FIFO cost lot tracking" in page and "PRODUCT IN" in page and "PRODUCT OUT" in page
    assert f'>{traded["b1"].number}</a>' in page and "On hand at 2026-03-31" in page
    response = client.get(url, {**params, "format": "csv"})
    assert response["Content-Disposition"] == 'attachment; filename="fifo-lots-WIDGET-2026-03-01_2026-03-31.csv"'
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0] == ["In date", "In transaction", "Received from", "In quantity", "Unit", "Age", "Cost per unit",
                       "In total", "Out date", "Out transaction", "Dispersed to", "Out quantity", "Note"]
    assert rows[1][:4] == ["2026-03-01", f"Bill {traded['b1'].number}", "Acme Supply", "10"]
    assert client.get(url, {**params, "format": "pdf"})["Content-Type"] == "application/pdf"
    assert "fifo-lots" in [r["slug"] for g in catalog() for r in g["reports"]]
