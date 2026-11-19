"""Item picker on PO and bill lines: name or SKU accepted, search endpoint, labels on edit."""
import html
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from contacts.models import Vendor
from purchasing.models import PurchaseOrder
from purchasing.posting.orders import POLineSpec, create_po
from tests.test_po_bill_views import adm, po_post, setup  # noqa: F401  (shared fixtures and helpers)

D = Decimal


@pytest.fixture
def stock(setup, make_item):
    return (make_item("WID-1", name="Blue Widget", purchase_cost="4.00"),
            make_item("WID-2", name="Red widget", purchase_cost="5.00"),
            make_item("SC-9", name="Screw box", purchase_cost="1.00"))


def search(client, q):
    return client.get(reverse("items:search"), {"q": q}).json()["results"]


def test_search_matches_name_or_sku_case_insensitively_and_ranks_sku_first(adm, stock):
    assert [r["sku"] for r in search(adm, "widget")] == ["WID-1", "WID-2"]
    assert [r["sku"] for r in search(adm, "sc-9")] == ["SC-9"]
    assert [r["sku"] for r in search(adm, "SCREW")] == ["SC-9"]
    row = search(adm, "blue")[0]
    assert row["label"] == "Blue Widget (WID-1)" and row["cost"] == "4.00"
    assert search(adm, "zzz") == []


def test_search_hides_inactive_items_and_is_capped(adm, stock, make_item):
    stock[2].is_active = False
    stock[2].save()
    assert search(adm, "screw") == []
    for n in range(40):
        make_item(f"BULK-{n:03d}", name=f"Bulk thing {n}")
    assert len(search(adm, "bulk")) == 15


def test_search_needs_a_login(stock):
    assert Client().get(reverse("items:search"), {"q": "wid"}).status_code == 302


def test_po_accepts_a_sku_a_name_or_the_picker_label(adm, stock):
    vendor = Vendor.objects.create(name="Acme")
    for n, typed in enumerate(["wid-1", "red WIDGET", "Screw box (SC-9)"]):
        r = adm.post(reverse("purchasing:po-new"), po_post(vendor, (typed, 2, ""), token=f"t{n}"))
        assert r.status_code == 302, html.unescape(r.content.decode())
    skus = [PurchaseOrder.objects.get(number=n).lines.get().item.sku for n in
            PurchaseOrder.objects.order_by("id").values_list("number", flat=True)]
    assert skus == ["WID-1", "WID-2", "SC-9"]


def test_unknown_and_ambiguous_names_keep_the_error_strings(adm, stock, make_item):
    vendor = Vendor.objects.create(name="Acme")
    r = adm.post(reverse("purchasing:po-new"), po_post(vendor, ("NOPE", 1, "")))
    assert "Line 1: no item has SKU NOPE." in html.unescape(r.content.decode()) and 'value="NOPE"' in r.content.decode()
    make_item("DUP-A", name="Twin"), make_item("DUP-B", name="twin")
    r = adm.post(reverse("purchasing:po-new"), po_post(vendor, ("Twin", 1, ""), token="t2"))
    assert "Line 1: more than one item is named Twin. Pick one from the list." in html.unescape(r.content.decode())
    r = adm.post(reverse("purchasing:po-new"), po_post(vendor, ("", 1, ""), token="t3"))
    assert "Line 1: choose an item." in html.unescape(r.content.decode())


def test_edit_form_shows_name_with_sku_and_the_search_url(adm, stock, admin_user):
    vendor = Vendor.objects.create(name="Acme")
    po = create_po(vendor=vendor, date="2026-10-05", user=admin_user, lines=[POLineSpec(stock[0], 3, D("4.00")),
                                                                    POLineSpec(stock[2], 1, D("1.00"))])
    page = html.unescape(adm.get(reverse("purchasing:po-edit", args=[po.number])).content.decode())
    assert 'value="Blue Widget (WID-1)"' in page and 'value="Screw box (SC-9)"' in page
    assert reverse("items:search") in page and 'placeholder="Blue Widget"' in page
    assert "<datalist" not in page
    # Saving the label back works unchanged.
    data = po_post(vendor, ("Blue Widget (WID-1)", 4, "4.00", "", str(po.lines.first().pk)))
    assert adm.post(reverse("purchasing:po-edit", args=[po.number]), data).status_code == 302
    assert po.lines.get().quantity == 4
