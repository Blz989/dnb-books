import re
from html import unescape
from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from contacts.models import Customer, Terms, Vendor
from handling.billing import create_run
from handling.models import HandlingRate, HandlingRun, HandlingSettings, RunStatus
from handling.periods import Period
from ledger.models import Account, AccountSubtype, AccountType
from purchasing.posting.bills import bill_from_po
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from sales.models import Fulfillment
from sales.posting.invoices import InvoiceLineSpec, create_invoice, invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import CreditLineSpec, apply_credit, create_credit_note

D = Decimal
DS = dict(fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")
OCT_1 = Period(date(2026, 10, 1), date(2026, 10, 15))
LATER = date(2026, 12, 31)
REPORT = "/reports/order-profitability/"
SETTINGS = "/handling/settings/"


def d(month, day):
    return date(2026, month, day)


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def setup(handling_setup, inv_settings, purchasing_settings, sales_settings):
    Vendor.objects.create(name="Acme Mfg")
    return handling_setup


@pytest.fixture
def gadget(setup, make_item):
    return make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")


@pytest.fixture
def part(setup, make_item):
    return make_item("PART", sale_price="20.0000", purchase_cost="7.0000")


def receive(item, qty, price, day, vendor, user):
    po = create_po(vendor=vendor, date=day, user=user, lines=[POLineSpec(item, qty, D(price))])
    line = po.lines.get()
    return bill_from_po(po, date=day, vendor_invoice_number=f"R-{po.number}", quantities={line.pk: qty}, user=user)


@pytest.fixture
def stocked(gadget, part, vendor, admin_user):
    receive(gadget, 30, "4.00", d(9, 29), vendor, admin_user)
    receive(part, 30, "7.00", d(9, 29), vendor, admin_user)
    return gadget


def stock_sale(customer, user, item, order_day, invoice_day, qty=3, price="9.50"):
    so = create_order(customer=customer, date=order_day, user=user, lines=[OrderLineSpec(item, qty, D(price))])
    line = so.lines.get()
    inv = invoice_from_order(so, date=invoice_day, quantities={line.pk: qty}, user=user)
    return so, inv


@pytest.fixture
def other_customer(db):
    return Customer.objects.create(name="Harbor Plumbing", terms=Terms.NET_15)


def flag(text):
    return f'<span class="tag flag">{text}</span>'


# ---- report ----

def test_report_lists_orders_with_totals(client, admin_user, customer, stocked, part):
    so, _ = stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    direct = create_invoice(customer=customer, date=d(10, 6), user=admin_user,
                            lines=[InvoiceLineSpec(part, 1, D("20.00"))])
    client.force_login(admin_user)
    r = client.get(REPORT, {"from": "2026-10-01", "to": "2026-10-27"})
    assert r.status_code == 200
    html = r.content.decode()
    assert [o.key for o in r.context["rows"]] == [so.number, direct.number]
    for header in ["Order", "Customer", "Date", "Sales", "Cost", "Profit", "Margin", "Handling owed", "Billed",
                   "Balance"]:
        assert f">{header}</th>" in html
    assert f'href="/sales/orders/{so.number}/"' in html
    assert f'href="/sales/invoices/{direct.number}/"' in html
    # SO: 28.50 - 12.00 = 16.50, 57.9%, owed 9.08. Direct: 20.00 - 7.00 = 13.00, 65.0%, owed 7.15.
    for value in ["28.50", "12.00", "16.50", "57.9%", "9.08", "20.00", "7.00", "13.00", "65.0%", "7.15"]:
        assert value in html
    t = r.context["totals"]
    assert (t["sales"], t["cost"], t["profit"], t["owed"], t["billed"], t["balance"]) == (
        D("48.50"), D("19.00"), D("29.50"), D("16.23"), D("0.00"), D("16.23"))
    assert t["margin"] == D("60.8")
    assert 'class="total"' in html and "48.50" in html and "16.23" in html and "60.8%" in html


def test_report_filters_customer_dates_unbilled(client, admin_user, customer, other_customer, stocked):
    early, _ = stock_sale(customer, admin_user, stocked, d(10, 3), d(10, 5))
    late, _ = stock_sale(customer, admin_user, stocked, d(10, 20), d(10, 21))
    harbor, _ = stock_sale(other_customer, admin_user, stocked, d(10, 4), d(10, 6))
    client.force_login(admin_user)

    def keys(**params):
        return [o.key for o in client.get(REPORT, params).context["rows"]]

    full = {"from": "2026-10-01", "to": "2026-10-27"}
    assert keys(**full) == [early.number, harbor.number, late.number]
    assert keys(**full, customer=other_customer.pk) == [harbor.number]
    assert keys(**{"from": "2026-10-10", "to": "2026-10-27"}) == [late.number]
    assert keys(**{"from": "2026-10-01", "to": "2026-10-04"}) == [early.number, harbor.number]

    create_run(OCT_1, user=admin_user, today=LATER)  # bills both orders dated in Oct 1-15
    r = client.get(REPORT, {**full, "unbilled": "1"})
    assert [o.key for o in r.context["rows"]] == [late.number]
    assert keys(**full) == [early.number, harbor.number, late.number]
    assert r.context["totals"]["billed"] == D("0.00")

    # Defaults: the first of this month through today.
    r = client.get(REPORT)
    today = timezone.localdate()
    assert (r.context["date_from"], r.context["date_to"]) == (today.replace(day=1), today)
    assert f'value="{today.replace(day=1).isoformat()}"' in r.content.decode()

    r = client.get(REPORT, {"from": "2026-10-20", "to": "2026-10-01"})
    assert "The start date must be on or before the end date." in r.content.decode()


def test_report_shows_flags(client, admin_user, customer, stocked, part):
    pending = create_order(customer=customer, date=d(10, 2), user=admin_user,
                           lines=[OrderLineSpec(part, 1, D("20.00"), **DS)])
    invoice_from_order(pending, date=d(10, 3), quantities={pending.lines.get().pk: 1}, user=admin_user)
    loss, _ = stock_sale(customer, admin_user, stocked, d(10, 4), d(10, 5), qty=2, price="1.00")
    early, _ = stock_sale(customer, admin_user, stocked, d(9, 20), d(10, 2))
    fine, _ = stock_sale(customer, admin_user, stocked, d(10, 6), d(10, 7))
    client.force_login(admin_user)
    html = client.get(REPORT, {"from": "2026-09-01", "to": "2026-10-27"}).content.decode()
    assert html.count(flag("Drop-ship bill not entered")) == 1
    assert html.count(flag("Below zero, no handling")) == 1
    assert html.count(flag("Before handling start")) == 1
    # Each flag sits in its order's cell.
    for so, text in [(pending, "Drop-ship bill not entered"), (loss, "Below zero, no handling"),
                     (early, "Before handling start")]:
        cell = html.split(f">{so.number}</a>", 1)[1].split("</td>", 1)[0]
        assert flag(text) in cell
    cell = html.split(f">{fine.number}</a>", 1)[1].split("</td>", 1)[0]
    assert "tag flag" not in cell


def test_standard_user_can_view_report(client, standard_user, customer, stocked):
    stock_sale(customer, standard_user, stocked, d(10, 2), d(10, 3))
    client.force_login(standard_user)
    r = client.get(REPORT, {"from": "2026-10-01", "to": "2026-10-27"})
    assert r.status_code == 200 and len(r.context["rows"]) == 1
    home = client.get("/").content.decode()
    assert f'href="{REPORT}">Order profitability</a>' in home


def test_report_requires_login(client):
    r = client.get(REPORT)
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]


# ---- profit panel ----

def test_profit_panel_on_order_and_direct_invoice(client, admin_user, customer, stocked, part):
    Vendor.objects.create(name="Other Co")
    so = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[
        OrderLineSpec(stocked, 3, D("9.50")), OrderLineSpec(part, 2, D("20.00"), **DS),
        OrderLineSpec(part, 1, D("20.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Other Co")])
    stock_line, ds_line, pending_line = so.lines.all()
    inv = invoice_from_order(so, date=d(10, 3), quantities={stock_line.pk: 3, ds_line.pk: 2, pending_line.pk: 1},
                             user=admin_user)
    (po,) = [p for p in create_drop_ship_pos(so, user=admin_user, date=d(10, 3)) if p.vendor.name == "Acme Mfg"]
    bill_from_po(po, date=d(10, 5), vendor_invoice_number="V-1", quantities={po.lines.get().pk: 2},
                 prices={po.lines.get().pk: D("7.00")}, user=admin_user)
    note = create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D("5.00"))])
    apply_credit(note, inv, D("5.00"), date=d(10, 6), user=admin_user)
    run = create_run(OCT_1, user=admin_user, today=LATER)
    client.force_login(admin_user)

    html = client.get(f"/sales/orders/{so.number}/").content.decode()
    assert 'id="profit-panel"' in html
    panel = html.split('id="profit-panel"', 1)[1]
    # Sales 28.50 + 40.00 + 20.00 = 88.50, credits 5.00, cost 12.00 + 14.00 + pending = 26.00.
    # Profit 88.50 - 5.00 - 26.00 = 57.50; 55% = 31.625 -> 31.63.
    assert inv.number in panel
    for value in ["88.50", "5.00", "12.00", "14.00", "26.00", "57.50", "55%", "31.63"]:
        assert value in panel, value
    assert "Drop-ship bill not entered" in panel
    assert run.label in panel and "31.63" in panel

    # A direct invoice gets its own panel; an invoice that belongs to an order doesn't.
    direct = create_invoice(customer=customer, date=d(10, 7), user=admin_user,
                            lines=[InvoiceLineSpec(stocked, 2, D("9.50"))])
    html = client.get(f"/sales/invoices/{direct.number}/").content.decode()
    assert 'id="profit-panel"' in html
    panel = html.split('id="profit-panel"', 1)[1]
    for value in ["19.00", "8.00", "11.00", "6.05"]:  # 19.00 - 8.00 = 11.00, 55% = 6.05
        assert value in panel, value
    assert "Not billed yet." in panel
    assert 'id="profit-panel"' not in client.get(f"/sales/invoices/{inv.number}/").content.decode()


# ---- settings ----

@pytest.fixture
def handling_choices(db):
    v = Vendor.objects.create(name="Harbor Handling LLC", terms=Terms.NET_30)
    a = Account.objects.create(number="6400", name="Handling", type=AccountType.EXPENSE,
                               subtype=AccountSubtype.EXPENSE)
    return v, a


def test_settings_admin_only(client, standard_user, admin_user, handling_choices):
    client.force_login(standard_user)
    assert client.get(SETTINGS).status_code == 403
    assert client.post(SETTINGS, {"action": "settings"}).status_code == 403
    assert client.post(SETTINGS, {"action": "rate", "effective_from": "2026-11-01", "percent": "50"}).status_code == 403
    client.force_login(admin_user)
    r = client.get(SETTINGS)
    assert r.status_code == 200
    html = r.content.decode()
    assert "Harbor Handling LLC" in html and "6400" in html
    # Expense accounts only.
    Account.objects.create(number="1000", name="Checking", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    assert "Checking" not in client.get(SETTINGS).content.decode()


def test_settings_first_save_creates_55_percent_rate(client, admin_user, handling_choices):
    v, a = handling_choices
    client.force_login(admin_user)
    r = client.post(SETTINGS, {"action": "settings", "vendor": v.pk, "expense_account": a.pk,
                               "start_date": "2026-10-01"})
    assert r.status_code == 302 and r["Location"] == SETTINGS
    s = HandlingSettings.load()
    assert (s.vendor, s.expense_account, s.start_date) == (v, a, d(10, 1))
    (rate,) = HandlingRate.objects.all()
    assert (rate.effective_from, rate.rate, rate.created_by) == (d(10, 1), D("0.5500"), admin_user)
    assert s.is_configured
    # A second save doesn't add another rate.
    client.post(SETTINGS, {"action": "settings", "vendor": v.pk, "expense_account": a.pk, "start_date": "2026-10-01"})
    assert HandlingRate.objects.count() == 1
    html = client.get(SETTINGS).content.decode()
    assert "Oct 1, 2026" in html and "55%" in html
    # Purchasing settings shows the summary and links here.
    html = client.get("/purchasing/settings/").content.decode()
    assert "Harbor Handling LLC" in html and "6400" in html and "Oct 1, 2026" in html and "55%" in html
    assert f'href="{SETTINGS}"' in html


def test_settings_require_every_field(client, admin_user, handling_choices):
    client.force_login(admin_user)
    r = client.post(SETTINGS, {"action": "settings", "vendor": "", "expense_account": "", "start_date": ""})
    assert r.status_code == 200
    html = r.content.decode()
    assert "Choose a vendor." in html and "Choose an account." in html and "Enter a date like 2026-01-31." in html
    assert not HandlingRate.objects.exists()


def test_add_rate_from_settings(client, admin_user, setup):
    client.force_login(admin_user)
    r = client.post(SETTINGS, {"action": "rate", "effective_from": "2026-11-01", "percent": "52.5"})
    assert r.status_code == 302 and r["Location"] == SETTINGS
    assert list(HandlingRate.objects.values_list("effective_from", "rate")) == [
        (d(11, 1), D("0.5250")), (d(10, 1), D("0.5500"))]
    html = client.get(SETTINGS).content.decode()
    assert "Nov 1, 2026" in html and "52.5%" in html

    r = client.post(SETTINGS, {"action": "rate", "effective_from": "2026-11-01", "percent": "50"})
    assert "The new rate must start after Nov 1, 2026." in r.content.decode()
    r = client.post(SETTINGS, {"action": "rate", "effective_from": "2026-12-01", "percent": "120"})
    assert "Enter a rate between 0 and 100%." in r.content.decode()
    r = client.post(SETTINGS, {"action": "rate", "effective_from": "2026-12-01", "percent": "abc"})
    assert "Enter a rate between 0 and 100%." in r.content.decode()
    assert HandlingRate.objects.count() == 2


def test_start_date_locked_after_first_run(client, admin_user, setup):
    s = HandlingSettings.load()
    client.force_login(admin_user)
    post = {"action": "settings", "vendor": s.vendor.pk, "expense_account": s.expense_account.pk}
    r = client.post(SETTINGS, {**post, "start_date": "2026-09-16"})
    assert r.status_code == 302  # no run yet, so it can move
    client.post(SETTINGS, {**post, "start_date": "2026-10-01"})
    create_run(OCT_1, user=admin_user, today=LATER)
    r = client.post(SETTINGS, {**post, "start_date": "2026-10-16"})
    assert r.status_code == 200
    assert "The start date can't change after the first handling bill." in unescape(r.content.decode())
    assert HandlingSettings.load().start_date == d(10, 1)
    # The other fields still save with the same start date.
    other = Vendor.objects.create(name="Second Handling Co")
    r = client.post(SETTINGS, {**post, "vendor": other.pk, "start_date": "2026-10-01"})
    assert r.status_code == 302 and HandlingSettings.load().vendor == other


def test_seed_demo_sets_up_handling(db):
    from django.core.management import call_command

    from sales.models import Invoice, SalesOrder

    call_command("seed_demo", verbosity=0)
    s = HandlingSettings.load()
    earliest = min([*SalesOrder.objects.values_list("date", flat=True),
                    *Invoice.objects.filter(sales_order__isnull=True).values_list("date", flat=True)])
    assert (s.vendor.name, s.expense_account.number, s.expense_account.name) == ("Harbor Handling LLC", "6400", "Handling")
    assert s.start_date == earliest.replace(day=1)
    assert list(HandlingRate.objects.values_list("effective_from", "rate")) == [(s.start_date, D("0.5500"))]
    assert s.is_configured


# ---- runs ----

RUNS = "/handling/"
PREVIEW_1 = "/handling/preview/2026-10-15/"
PREVIEW_2 = "/handling/preview/2026-10-31/"


@pytest.fixture
def on_day(monkeypatch):
    """Sets the date the screens treat as today."""
    def set_today(day):
        monkeypatch.setattr(timezone, "localdate", lambda *a, **k: day)
    return set_today


def page(r):
    return unescape(r.content.decode())


def token_of(r):
    return re.search(r'name="token" value="([^"]+)"', r.content.decode()).group(1)


def test_run_list_and_next_button(client, admin_user, customer, stocked, on_day):
    client.force_login(admin_user)
    on_day(d(10, 10))
    html = page(client.get(RUNS))
    for header in ["Period", "Total", "Bill or credit", "Status"]:
        assert f">{header}</th>" in html
    assert "No handling bills yet." in html
    assert '<button type="button" class="btn primary" disabled>Next: Oct 1–15, 2026</button>' in html
    assert "Oct 1–15, 2026 can't be billed until Oct 15, 2026." in html
    assert f'href="{PREVIEW_1}"' not in html
    assert 'href="/handling/" class="on">Handling</a>' in html

    on_day(d(10, 15))
    html = page(client.get(RUNS))
    assert f'<a class="btn primary" href="{PREVIEW_1}">Next: Oct 1–15, 2026</a>' in html
    assert "can't be billed until" not in html

    stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    run = create_run(OCT_1, user=admin_user, today=LATER)
    on_day(d(10, 20))
    html = page(client.get(RUNS))
    assert f'href="/handling/runs/{run.pk}/">Oct 1–15, 2026</a>' in html
    assert f'href="/purchasing/bills/{run.bill.number}/">{run.bill.number}</a>' in html
    assert "9.08" in html and "Posted" in html
    assert "Next: Oct 16–31, 2026" in html
    assert "Oct 16–31, 2026 can't be billed until Oct 31, 2026." in html

    s = HandlingSettings.load()
    s.vendor = None
    s.save()
    html = page(client.get(RUNS))
    assert "Set up handling in Purchasing settings first." in html and f'href="{SETTINGS}"' in html
    assert "Next:" not in html


def test_preview_then_create(client, admin_user, customer, stocked, on_day):
    on_day(LATER)
    so, _ = stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    client.force_login(admin_user)
    r = client.get(PREVIEW_1)
    assert r.status_code == 200
    html = page(r)
    for header in ["Order", "Customer", "Sales", "Cost", "Profit", "Owed to date", "Billed before", "This bill"]:
        assert f">{header}</th>" in html
    assert f'href="/sales/orders/{so.number}/"' in html and "Acme Builders" in html
    for value in ["28.50", "12.00", "16.50", "9.08", "0.00"]:
        assert value in html
    assert 'class="total"' in html
    assert ">Create bill</button>" in html
    assert client.get("/handling/preview/2026-10-14/").status_code == 404

    r = client.post(PREVIEW_1, {"token": token_of(r)})
    run = HandlingRun.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/handling/runs/{run.pk}/"
    assert run.bill is not None and run.bill.vendor.name == "Harbor Handling LLC" and run.total == D("9.08")
    assert run.created_by == admin_user
    html = page(client.get(r["Location"]))
    assert f'href="/purchasing/bills/{run.bill.number}/">{run.bill.number}</a>' in html
    assert f'href="/handling/runs/{run.pk}/statement.pdf"' in html
    assert so.number in html and "9.08" in html

    # Incomplete settings: the problem instead of the table.
    s = HandlingSettings.load()
    s.vendor = None
    s.save()
    html = page(client.get(PREVIEW_2))
    assert "Set up handling in Purchasing settings first." in html and f'href="{SETTINGS}"' in html
    assert "<table" not in html and "Create bill" not in html


def test_create_double_post_same_token_makes_one_run(client, admin_user, customer, stocked, on_day):
    on_day(LATER)
    stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    client.force_login(admin_user)
    tab_a, tab_b = token_of(client.get(PREVIEW_1)), token_of(client.get(PREVIEW_1))
    assert tab_a != tab_b
    assert client.post(PREVIEW_1, {"token": tab_a}).status_code == 302
    r = client.post(PREVIEW_1, {"token": tab_a}, follow=True)
    assert "That was already done." in page(r)
    r = client.post(PREVIEW_1, {"token": tab_b}, follow=True)
    assert "Oct 1–15, 2026 is already billed." in page(r)
    assert HandlingRun.objects.count() == 1
    # The preview of a billed period says so and offers no button.
    html = page(client.get(PREVIEW_1))
    assert "Oct 1–15, 2026 is already billed." in html and "Create bill" not in html


def test_create_credit_label_when_negative(client, admin_user, customer, stocked, on_day):
    on_day(LATER)
    _, inv = stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    create_run(OCT_1, user=admin_user, today=LATER)
    note = create_credit_note(customer=customer, date=d(10, 20), user=admin_user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D("10.00"))])
    apply_credit(note, inv, D("10.00"), date=d(10, 20), user=admin_user)
    client.force_login(admin_user)
    # Profit 16.50 - 10.00 = 6.50, owed 3.58, billed 9.08 before: -5.50.
    r = client.get(PREVIEW_2)
    html = page(r)
    assert ">Create credit</button>" in html and "Create bill" not in html and "-5.50" in html
    r = client.post(PREVIEW_2, {"token": token_of(r)})
    run = HandlingRun.objects.get(period_start=d(10, 16))
    assert run.vendor_credit is not None and run.bill is None and run.total == D("-5.50")
    html = page(client.get(r["Location"]))
    assert f'href="/purchasing/credits/{run.vendor_credit.number}/">{run.vendor_credit.number}</a>' in html

    # Nothing to bill: the period is recorded with no bill.
    html = page(client.get("/handling/preview/2026-11-15/"))
    assert ">Record period</button>" in html and "Create bill" not in html


def test_void_from_detail_admin_only(client, admin_user, standard_user, customer, stocked, on_day):
    on_day(LATER)
    stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    first = create_run(OCT_1, user=admin_user, today=LATER)
    run = create_run(OCT_1.next(), user=admin_user, today=LATER)  # nothing to bill, recorded at 0.00
    detail, void = f"/handling/runs/{run.pk}/", f"/handling/runs/{run.pk}/void/"

    client.force_login(standard_user)
    assert client.get(detail).status_code == 200
    assert 'name="reason"' not in page(client.get(detail))
    assert client.post(void, {"reason": "Wrong"}).status_code == 403
    run.refresh_from_db()
    assert run.status == RunStatus.POSTED

    client.force_login(admin_user)
    assert 'name="reason"' not in page(client.get(f"/handling/runs/{first.pk}/"))  # not the latest
    r = client.post(f"/handling/runs/{first.pk}/void/", {"reason": "Wrong"}, follow=True)
    assert "Only the most recent handling bill can be voided." in page(r)
    assert client.get(void).status_code == 405
    html = page(client.get(detail))
    assert f'action="{void}"' in html and 'name="reason"' in html
    r = client.post(void, {"reason": "  ", "token": "v1"}, follow=True)
    assert "Enter a reason." in page(r)
    run.refresh_from_db()
    assert run.status == RunStatus.POSTED

    r = client.post(void, {"reason": "Wrong period", "token": "v2"})
    assert r.status_code == 302 and r["Location"] == detail
    run.refresh_from_db()
    assert run.status == RunStatus.VOID and run.void_reason == "Wrong period"
    html = page(client.get(detail))
    assert "Wrong period" in html and 'class="tag void"' in html and 'name="reason"' not in html
    assert "That was already done." in page(client.post(void, {"reason": "Again", "token": "v2"}, follow=True))

    # Now the first run is the latest, and its bill is voided with it.
    r = client.post(f"/handling/runs/{first.pk}/void/", {"reason": "Rate was wrong", "token": "v3"})
    first.refresh_from_db()
    first.bill.refresh_from_db()
    assert first.status == RunStatus.VOID and first.bill.status == "VOID"
    r = client.get(f"/handling/runs/{first.pk}/statement.pdf")  # a void run's statement stays viewable
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_statement_served_inline(client, standard_user, admin_user, customer, stocked):
    stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    run = create_run(OCT_1, user=admin_user, today=LATER)
    client.force_login(standard_user)
    r = client.get(f"/handling/runs/{run.pk}/statement.pdf")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    assert r["Content-Disposition"] == 'inline; filename="handling-2026-10-15.pdf"'
    assert b"".join(r.streaming_content if r.streaming else [r.content]).startswith(b"%PDF")
    assert client.get("/handling/runs/999/statement.pdf").status_code == 404


def test_home_card_due_and_hidden(client, admin_user, standard_user, customer, stocked, on_day):
    card = f'href="{PREVIEW_1}">Handling bill due for Oct 1–15, 2026</a>'
    client.force_login(standard_user)
    on_day(d(10, 10))
    assert "Handling bill due" not in page(client.get("/"))  # no period has ended
    on_day(d(10, 20))
    assert card in page(client.get("/"))
    on_day(d(11, 5))  # two periods due: the first one is named
    assert card in page(client.get("/"))
    create_run(OCT_1, user=admin_user, today=LATER)
    assert 'href="/handling/preview/2026-10-31/">Handling bill due for Oct 16–31, 2026</a>' in page(client.get("/"))
    on_day(d(10, 20))
    assert "Handling bill due" not in page(client.get("/"))
    on_day(d(11, 5))
    s = HandlingSettings.load()
    s.expense_account = None
    s.save()
    assert "Handling bill due" not in page(client.get("/"))  # settings incomplete: no card


def test_standard_user_can_create_run(client, standard_user, customer, stocked, on_day):
    on_day(LATER)
    stock_sale(customer, standard_user, stocked, d(10, 2), d(10, 3))
    client.force_login(standard_user)
    assert client.get(RUNS).status_code == 200
    r = client.get(PREVIEW_1)
    assert r.status_code == 200
    r = client.post(PREVIEW_1, {"token": token_of(r)})
    run = HandlingRun.objects.get()
    assert r.status_code == 302 and run.created_by == standard_user and run.bill is not None
    # Incomplete settings: standard users get the message without the admin link.
    s = HandlingSettings.load()
    s.vendor = None
    s.save()
    html = page(client.get(PREVIEW_2))
    assert "Set up handling in Purchasing settings first." in html and f'href="{SETTINGS}"' not in html


def test_statement_redrawn_when_file_missing(client, admin_user, customer, stocked):
    # Backups copy the database, not media: after a restore the saved PDF can be gone. The rows are in the database.
    stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 3))
    run = create_run(OCT_1, user=admin_user, today=LATER)
    run.statement_pdf.storage.delete(run.statement_pdf.name)
    client.force_login(admin_user)
    r = client.get(f"/handling/runs/{run.pk}/statement.pdf")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
