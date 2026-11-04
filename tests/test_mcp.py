"""Claude (MCP) service: Access token check, read tools, preview and confirm, the call log."""
import time
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.utils import timezone
from mcp.server.mcpserver.exceptions import ToolError

from contacts.models import Customer, Vendor
from items.models import Item, ItemType
from ledger.models import JournalEntry
from ledger.periods import close_period
from mcpserver import access, read, write
from mcpserver.access import Identity
from mcpserver.models import McpAccess, McpCall, McpSettings, PendingChange
from mcpserver.runtime import run
from purchasing.models import Bill, PurchaseOrder, VendorPayment
from purchasing.posting.bills import BillExpenseSpec, create_bill
from sales.models import Invoice, Payment
from sales.posting.invoices import InvoiceLineSpec, create_invoice

D = Decimal
AUD = "aud-test-tag"
TEAM = "lopezhometest.cloudflareaccess.com"


@pytest.fixture
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def cf(settings, keypair, monkeypatch):
    settings.DNB_CF_ACCESS_AUD = AUD
    settings.DNB_CF_ACCESS_TEAM_DOMAIN = TEAM
    monkeypatch.setattr(access, "signing_key", lambda token: keypair.public_key())
    return keypair


def token(key, email="owner@example.com", aud=AUD, iss=f"https://{TEAM}", exp_in=3600, **extra):
    now = int(time.time())
    claims = {"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in, "sub": "abc", **extra}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def owner(admin_user):
    admin_user.email = "Owner@Example.com"
    admin_user.save()
    McpAccess.objects.create(user=admin_user, enabled=True)
    return admin_user


@pytest.fixture
def clerk(standard_user):
    standard_user.email = "clerk@example.com"
    standard_user.save()
    McpAccess.objects.create(user=standard_user, enabled=True)
    return standard_user


def ctx_for(user, email=None, error=""):
    identity = Identity(email or user.email, user_id=user.pk if user else None, error=error)
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(state=SimpleNamespace(dnb_identity=identity))))


@pytest.fixture
def books(sales_settings, purchasing_settings, inv_settings, make_item, customer, vendor, cash, freight):
    widget = make_item("WID-1", sale_price=D("25.00"), purchase_cost="10.00")
    service = make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None,
                        inventory_account=None, sale_price=D("100.00"))
    return SimpleNamespace(widget=widget, service=service, customer=customer, vendor=vendor, bank=cash, freight=freight)


# ---- Access token ----

def test_valid_token_maps_to_user_case_insensitively(cf, owner):
    identity = access.identify(token(cf, email="owner@EXAMPLE.com"))
    assert identity.user_id == owner.pk and identity.error == ""


@pytest.mark.parametrize("change", [
    {"aud": "someone-else"}, {"iss": "https://evil.cloudflareaccess.com"}, {"exp_in": -3600},
])
def test_bad_audience_issuer_or_expiry_is_refused(cf, owner, change):
    with pytest.raises(access.TokenError):
        access.identify(token(cf, **change))


def test_wrong_signature_is_refused(cf, owner):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(access.TokenError):
        access.identify(token(other))


def test_missing_token_and_missing_setup_are_refused(cf, settings, owner):
    with pytest.raises(access.TokenError, match="Sign in"):
        access.identify("")
    settings.DNB_CF_ACCESS_AUD = ""
    with pytest.raises(access.TokenError, match="isn't set up"):
        access.identify(token(cf))


def test_team_domain_accepts_bare_or_https(settings):
    settings.DNB_CF_ACCESS_TEAM_DOMAIN = "lopezhomenet.cloudflareaccess.com"
    assert access.team_domain() == "https://lopezhomenet.cloudflareaccess.com"
    settings.DNB_CF_ACCESS_TEAM_DOMAIN = "https://lopezhomenet.cloudflareaccess.com/"
    assert access.team_domain() == "https://lopezhomenet.cloudflareaccess.com"


def test_unknown_email_inactive_user_or_access_off_get_the_plain_message(cf, owner, standard_user):
    expected = ("This Cloudflare login (stranger@example.com) isn't linked to a DNB Books user. "
                "Ask an admin to turn it on in Settings → Claude connections.")
    assert access.identify(token(cf, email="stranger@example.com")).error == expected
    standard_user.email = "std@example.com"
    standard_user.save()  # no McpAccess row: off by default
    assert "isn't linked" in access.identify(token(cf, email="std@example.com")).error
    McpAccess.objects.filter(user=owner).update(enabled=False)
    assert "isn't linked" in access.identify(token(cf)).error
    McpAccess.objects.filter(user=owner).update(enabled=True)
    owner.is_active = False
    owner.save()
    assert "isn't linked" in access.identify(token(cf)).error


def test_service_switch_off(cf, owner):
    s = McpSettings.load()
    s.enabled = False
    s.save()
    assert access.identify(token(cf)).error == (
        "Claude access to DNB Books is turned off. An admin can turn it on in Settings.")


def test_refused_identity_reaches_claude_as_the_message_and_is_logged(owner):
    with pytest.raises(ToolError, match="turned off"):
        run(ctx_for(None, email="owner@example.com", error=access.TURNED_OFF), "home_summary", read.home_summary)
    call = McpCall.objects.get()
    assert not call.ok and call.email == "owner@example.com" and call.user is None


# ---- read tools ----

def test_lookup_suggests_close_names(books, owner):
    with pytest.raises(ToolError) as e:
        run(ctx_for(owner), "get_customer", read.get_customer, name="Acme Bulders")
    assert str(e.value) == "No customer named Acme Bulders. Did you mean Acme Builders?"


def test_customer_and_invoice_reads(books, owner):
    inv = create_invoice(customer=books.customer, date=date(2026, 10, 1), user=owner,
                         lines=[InvoiceLineSpec(books.service, 2, D("100.00"))])
    c = run(ctx_for(owner), "get_customer", read.get_customer, name="acme builders")
    assert c["balance"] == "200.00" and c["open_invoices"][0]["number"] == inv.number
    got = run(ctx_for(owner), "get_invoice", read.get_invoice, number=inv.number.lower())
    assert got["total"] == "200.00" and got["lines"][0]["sku"] == "SVC"
    listed = run(ctx_for(owner), "list_invoices", read.list_invoices, customer="Acme Builders", status="open")
    assert [r["number"] for r in listed["invoices"]] == [inv.number]
    assert run(ctx_for(owner), "search_customers", read.search_customers, query="acme")["customers"][0]["balance"] == "200.00"


def test_vendor_read_masks_tax_id(books, owner):
    books.vendor.tax_id = "12-3456789"
    books.vendor.save()
    v = run(ctx_for(owner), "get_vendor", read.get_vendor, name="Acme Supply")
    assert "3456789" not in str(v) and v["tax_id"].endswith("6789")


def test_reports_run(books, owner):
    create_invoice(customer=books.customer, date=date(2026, 10, 1), user=owner,
                   lines=[InvoiceLineSpec(books.service, 1, D("100.00"))])
    pnl = run(ctx_for(owner), "run_report", read.run_report, report="profit_and_loss", start="2026-01-01",
              end="2026-12-31", compare_years=1)
    assert pnl["columns"][0].startswith("Jan 1") and len(pnl["columns"]) == 2
    net = next(r for r in pnl["rows"] if r.get("account") == "Net income")
    assert net["amounts"][0] == "100.00"
    for report in ["trial_balance", "balance_sheet", "ar_aging", "ap_aging", "open_invoices", "bills_due",
                   "low_stock", "inventory_valuation", "order_profitability", "cash_forecast"]:
        assert isinstance(run(ctx_for(owner), "run_report", read.run_report, report=report), dict)
    assert run(ctx_for(owner), "home_summary", read.home_summary)["open_receivables"] == "100.00"
    with pytest.raises(ToolError, match="Choose a report"):
        run(ctx_for(owner), "run_report", read.run_report, report="nope")


def test_item_and_journal_reads(books, owner):
    item = run(ctx_for(owner), "get_item", read.get_item, sku="wid-1")
    assert item["sku"] == "WID-1" and item["on_hand"] == 0
    create_invoice(customer=books.customer, date=date(2026, 10, 1), user=owner,
                   lines=[InvoiceLineSpec(books.service, 1, D("100.00"))])
    found = run(ctx_for(owner), "search_journal_entries", read.search_journal_entries, query="INV")
    assert found["entries"][0]["lines"]


# ---- preview and confirm ----

def test_invoice_preview_posts_nothing_and_confirm_posts_it(books, owner):
    entries = JournalEntry.objects.count()
    p = run(ctx_for(owner), "create_invoice", write.preview, operation="create_invoice", payload={
        "customer": "Acme Builders", "date": "2026-10-05", "lines": [{"sku": "SVC", "quantity": 3}]})
    assert p["preview"]["total"] == "300.00" and p["warnings"] == []
    assert any(l["debit"] == "300.00" and "Accounts Receivable" in l["account"] for l in p["preview"]["posts"])
    assert Invoice.objects.count() == 0 and JournalEntry.objects.count() == entries
    done = run(ctx_for(owner), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    inv = Invoice.objects.get()
    assert done["document"] == inv.number == p["preview"]["invoice"]
    assert done["result"]["total"] == "300.00" and inv.created_by == owner
    assert McpCall.objects.filter(tool="confirm_change", document=inv.number, ok=True).exists()


def test_confirmation_is_single_use_expires_and_belongs_to_its_user(books, owner, clerk):
    payload = {"customer": "Acme Builders", "date": "2026-10-05", "lines": [{"sku": "SVC", "quantity": 1}]}
    p = write.preview(owner, "create_invoice", payload)
    with pytest.raises(ToolError, match="No preview has that confirmation_id"):
        run(ctx_for(clerk), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    write.confirm(owner, p["confirmation_id"])
    with pytest.raises(ToolError, match="^That change was already made.$"):
        run(ctx_for(owner), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    p2 = write.preview(owner, "create_invoice", payload)
    PendingChange.objects.filter(pk=p2["confirmation_id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ToolError, match="^That preview expired. Ask for it again.$"):
        run(ctx_for(owner), "confirm_change", write.confirm, confirmation_id=p2["confirmation_id"])
    assert Invoice.objects.count() == 1


def test_month_closed_between_preview_and_confirm_blocks_it(books, owner):
    p = write.preview(owner, "create_invoice", {"customer": "Acme Builders", "date": "2026-09-15",
                                                "lines": [{"sku": "SVC", "quantity": 1}]})
    close_period(2026, 9, owner)
    with pytest.raises(ToolError, match="September 2026 is closed"):
        run(ctx_for(owner), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    assert Invoice.objects.count() == 0
    assert PendingChange.objects.get(pk=p["confirmation_id"]).used_at is None


def test_closed_month_fails_at_preview_with_the_app_message(books, owner):
    close_period(2026, 9, owner)
    with pytest.raises(ToolError, match="September 2026 is closed"):
        run(ctx_for(owner), "create_invoice", write.preview, operation="create_invoice", payload={
            "customer": "Acme Builders", "date": "2026-09-15", "lines": [{"sku": "SVC", "quantity": 1}]})
    assert not PendingChange.objects.exists()


def test_negative_stock_shows_as_a_warning_and_confirm_allows_it(books, owner):
    p = write.preview(owner, "create_invoice", {"customer": "Acme Builders", "date": "2026-10-05",
                                                "lines": [{"sku": "WID-1", "quantity": 2}]})
    assert p["warnings"] and "WID-1" in " ".join(p["warnings"])
    write.confirm(owner, p["confirmation_id"])
    assert Invoice.objects.count() == 1


def test_payment_applies_oldest_first_by_default(books, owner):
    a = create_invoice(customer=books.customer, date=date(2026, 9, 1), user=owner,
                       lines=[InvoiceLineSpec(books.service, 1, D("100.00"))])
    b = create_invoice(customer=books.customer, date=date(2026, 10, 1), user=owner,
                       lines=[InvoiceLineSpec(books.service, 1, D("100.00"))])
    p = write.preview(owner, "receive_payment", {"customer": "Acme Builders", "amount": "150.00", "method": "check",
                                                 "date": "2026-10-10"})
    assert [x["invoice"] for x in p["preview"]["applied"]] == [a.number, b.number]
    assert Payment.objects.count() == 0
    write.confirm(owner, p["confirmation_id"])
    assert Payment.objects.get().amount == D("150.00")


def test_ach_payment_needs_bank_account(books, owner):
    with pytest.raises(ToolError, match="Choose the bank account the ACH went to."):
        run(ctx_for(owner), "receive_payment", write.preview, operation="receive_payment", payload={
            "customer": "Acme Builders", "amount": "10.00", "method": "ach"})


def test_bill_duplicate_warning_then_vendor_payment(books, owner):
    create_bill(vendor=books.vendor, date=date(2026, 10, 1), vendor_invoice_number="A-77", user=owner,
                expense_lines=[BillExpenseSpec(books.freight, D("40.00"))])
    p = write.preview(owner, "create_bill", {"vendor": "Acme Supply", "date": "2026-10-02",
                                             "vendor_invoice_number": "A-77",
                                             "expense_lines": [{"account": "6150", "amount": "60.00"}]})
    assert p["warnings"] == [f"Acme Supply already has bill {Bill.objects.get().number} with invoice # A-77."]
    write.confirm(owner, p["confirmation_id"])
    new = Bill.objects.order_by("-id").first()
    pay = write.preview(owner, "pay_bills", {"vendor": "Acme Supply", "method": "ach", "paid_from": "1000",
                                             "date": "2026-10-20", "bills": [{"bill": new.number}]})
    assert pay["preview"]["amount"] == "60.00"
    write.confirm(owner, pay["confirmation_id"])
    assert VendorPayment.objects.get().amount == D("60.00")


def test_purchase_order_then_bill_against_it(books, owner):
    p = write.preview(owner, "create_purchase_order", {"vendor": "Acme Supply", "date": "2026-10-01",
                                                       "lines": [{"sku": "WID-1", "quantity": 10}]})
    assert p["preview"]["total"] == "100.00"
    po_number = write.confirm(owner, p["confirmation_id"])["document"]
    b = write.preview(owner, "create_bill", {"vendor": "Acme Supply", "date": "2026-10-03", "purchase_order": po_number,
                                             "vendor_invoice_number": "S-1", "item_lines": [{"sku": "WID-1", "quantity": 4}]})
    assert b["preview"]["total"] == "40.00" and b["preview"]["purchase_order"] == po_number
    write.confirm(owner, b["confirmation_id"])
    po = read.get_purchase_order(owner, po_number)
    assert po["lines"][0]["billed"] == 4 and po["lines"][0]["remaining"] == 6
    assert PurchaseOrder.objects.count() == 1


def test_shared_drop_ship_po_lists_its_orders(books, owner, admin_user):
    from purchasing.posting.orders import create_drop_ship_pos
    from sales.posting.orders import OrderLineSpec, create_order

    ds = dict(fulfillment="DROP_SHIP", drop_ship_vendor="Acme Supply")
    harbor = Customer.objects.create(name="Harbor Plumbing")
    first = create_order(customer=books.customer, date=date(2026, 10, 1), user=admin_user,
                         lines=[OrderLineSpec(books.widget, 6, D("25.00"), **ds)])
    second = create_order(customer=harbor, date=date(2026, 10, 2), user=admin_user,
                          lines=[OrderLineSpec(books.widget, 4, D("25.00"), **ds)])
    (po,) = create_drop_ship_pos([first, second], user=admin_user, date=date(2026, 10, 3))
    got = read.get_purchase_order(owner, po.number)
    assert got["drop_ship_for"] == f"{first.number}, {second.number}"
    (line,) = got["lines"]
    assert line["quantity"] == 10 and line["allocations"] == [
        {"sales_order": first.number, "customer": "Acme Builders", "quantity": 6},
        {"sales_order": second.number, "customer": "Harbor Plumbing", "quantity": 4}]


def test_save_customer_vendor_item(books, owner):
    p = write.preview(owner, "save_customer", {"name": "Birch Co", "fields": {"email": "ap@birch.test", "terms": "Net 15"}})
    assert p["preview"]["action"] == "create" and not Customer.objects.filter(name="Birch Co").exists()
    write.confirm(owner, p["confirmation_id"])
    assert Customer.objects.get(name="Birch Co").email == "ap@birch.test"
    u = write.preview(owner, "save_customer", {"name": "birch co", "fields": {"phone": "555-0100"}})
    assert u["preview"]["action"] == "update" and u["preview"]["changed"] == ["phone"]
    write.confirm(owner, u["confirmation_id"])
    assert Customer.objects.get(name="Birch Co").phone == "555-0100"

    v = write.preview(owner, "save_vendor", {"name": "Acme Supply", "fields": {"tax_id": "98-7654321"}})
    assert "7654321" not in str(v)
    write.confirm(owner, v["confirmation_id"])
    assert Vendor.objects.get(name="Acme Supply").tax_id == "98-7654321"

    from items.models import InventorySettings

    s = InventorySettings.load()
    s.sku_prefix, s.sku_next, s.sku_digits = "DNB-", 7, 4
    s.save()
    i = write.preview(owner, "save_item", {"fields": {"name": "Gasket", "item_type": "NON_STOCK",
                                                      "income_account": "4000", "sale_price": "3.50"}})
    assert i["preview"]["record"]["sku"] == "DNB-0007"
    assert write.confirm(owner, i["confirmation_id"])["document"] == "DNB-0007"
    assert Item.objects.get(sku="DNB-0007").sale_price == D("3.50")
    assert InventorySettings.load().sku_next == 8


def test_standard_user_without_permission_gets_role_message(books, clerk):
    from django.contrib.auth.models import Permission

    for codename in ("add_customer", "change_customer"):
        clerk.groups.first().permissions.remove(Permission.objects.get(codename=codename))
    clerk = type(clerk).objects.get(pk=clerk.pk)  # drop the permission cache
    with pytest.raises(ToolError, match=r"^Your role \(Standard\) can't do this. Ask an admin.$"):
        run(ctx_for(clerk), "save_customer", write.preview, operation="save_customer", payload={"name": "New Co", "fields": {}})


def test_standard_user_can_invoice_like_in_the_app(books, clerk):
    p = run(ctx_for(clerk), "create_invoice", write.preview, operation="create_invoice", payload={
        "customer": "Acme Builders", "date": "2026-10-05", "lines": [{"sku": "SVC", "quantity": 1}]})
    run(ctx_for(clerk), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    assert Invoice.objects.get().created_by == clerk


def test_writes_are_audited_under_the_person(books, owner):
    from auditlog.models import LogEntry

    p = run(ctx_for(owner), "save_customer", write.preview, operation="save_customer",
            payload={"name": "Audit Co", "fields": {}})
    assert not LogEntry.objects.filter(object_repr__contains="Audit Co").exists()
    run(ctx_for(owner), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    assert LogEntry.objects.filter(object_repr__contains="Audit Co", actor=owner).exists()


def test_call_log_hides_tax_ids(books, owner):
    run(ctx_for(owner), "save_vendor", write.preview, operation="save_vendor",
        payload={"name": "Acme Supply", "fields": {"tax_id": "98-7654321"}})
    call = McpCall.objects.get(tool="save_vendor")
    assert "7654321" not in str(call.arguments)


# ---- end to end: the SDK client against the real HTTP server, no network beyond localhost ----

@pytest.fixture
def purge_all():
    """Empty every table in one transaction with the immutability triggers off (as test_handling_runs does), so the
    end-of-test flush doesn't trip over posted entries and the documents that point to them."""
    import importlib

    from django.core.management.color import no_style
    from django.db import connection, transaction

    yield
    migs = [importlib.import_module(f"{app}.migrations.0002_immutability_triggers") for app in ("ledger", "items")]
    tables = connection.introspection.django_table_names(only_existing=True, include_views=False)
    with transaction.atomic(), connection.cursor() as c:
        for mig in migs:
            for sql in mig.REVERSE:
                c.execute(sql)
        for sql in connection.ops.sql_flush(no_style(), tables):
            c.execute(sql)
        for mig in migs:
            for sql in mig.FORWARD:
                c.execute(sql)


@pytest.fixture
def live_mcp(settings, cf):
    import socket
    import threading

    import uvicorn

    from mcpserver.server import build_app

    settings.DNB_MCP_ALLOWED_HOSTS = ["127.0.0.1"]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def _call(url, tok, steps):
    import asyncio

    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        async with httpx2.AsyncClient(headers={"Cf-Access-Jwt-Assertion": tok}, timeout=30) as http:
            async with Client(streamable_http_client(url, http_client=http)) as client:
                return [await step(client) for step in steps]

    return asyncio.run(go())


@pytest.mark.django_db(transaction=True)
def test_end_to_end_list_report_preview_confirm(live_mcp, cf, books, owner, purge_all):
    import json

    tok = token(cf)

    async def tools(c):
        return [t.name for t in (await c.list_tools()).tools]

    async def report(c):
        return await c.call_tool("run_report", {"report": "trial_balance"})

    async def preview(c):
        return await c.call_tool("create_invoice", {"customer": "Acme Builders", "date": "2026-10-05",
                                                    "lines": [{"sku": "SVC", "quantity": 2}]})

    names, rep, prev = _call(live_mcp, tok, [tools, report, preview])
    assert {"run_report", "create_invoice", "confirm_change", "get_customer"} <= set(names)
    assert not rep.is_error and not prev.is_error
    cid = (prev.structured_content or json.loads(prev.content[0].text))["confirmation_id"]
    assert Invoice.objects.count() == 0

    async def confirm(c):
        return await c.call_tool("confirm_change", {"confirmation_id": cid})

    (done,) = _call(live_mcp, tok, [confirm])
    assert not done.is_error
    inv = Invoice.objects.get()
    assert inv.created_by == owner
    assert list(McpCall.objects.order_by("id").values_list("tool", flat=True)) == [
        "run_report", "create_invoice", "confirm_change"]


@pytest.mark.django_db(transaction=True)
def test_end_to_end_refusals(live_mcp, cf, owner):
    import httpx2

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    assert httpx2.post(live_mcp, json=body, headers=headers).status_code == 401
    bad = token(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    assert httpx2.post(live_mcp, json=body, headers={**headers, "Cf-Access-Jwt-Assertion": bad}).status_code == 401
    wrong_host = httpx2.post(live_mcp, json=body,
                             headers={**headers, "Cf-Access-Jwt-Assertion": token(cf), "Host": "evil.example.com"})
    assert wrong_host.status_code == 421

    McpAccess.objects.filter(user=owner).update(enabled=False)

    async def call(c):
        return await c.call_tool("home_summary", {})

    (result,) = _call(live_mcp, token(cf), [call])
    assert result.is_error and "isn't linked to a DNB Books user" in result.content[0].text


# ---- Settings → Claude connections ----

def test_connections_page_is_admin_only(client, standard_user):
    client.force_login(standard_user)
    assert client.get("/settings/claude/").status_code == 403


def test_admin_turns_service_and_people_on_and_off(client, owner, standard_user):
    client.force_login(owner)
    page = client.get("/settings/claude/")
    assert page.status_code == 200 and b"https://mcp.lopezhome.casa/mcp" in page.content
    client.post("/settings/claude/", {"action": "service", "enabled": "0"})
    assert McpSettings.load().enabled is False
    client.post("/settings/claude/", {"action": "service", "enabled": "1"})
    assert McpSettings.load().enabled is True

    r = client.post("/settings/claude/", {"action": "user", "user": standard_user.pk, "enabled": "1"}, follow=True)
    assert b"Add an email for standard in Users and roles first." in r.content
    assert not McpAccess.objects.filter(user=standard_user, enabled=True).exists()
    standard_user.email = "std@example.com"
    standard_user.save()
    client.post("/settings/claude/", {"action": "user", "user": standard_user.pk, "enabled": "1"})
    assert McpAccess.objects.get(user=standard_user).enabled is True
    client.post("/settings/claude/", {"action": "user", "user": standard_user.pk, "enabled": "0"})
    assert McpAccess.objects.get(user=standard_user).enabled is False


def test_call_log_shows_on_the_page(client, owner):
    McpCall.objects.create(user=owner, email=owner.email, tool="create_invoice", ok=False, error="September 2026 is closed.")
    client.force_login(owner)
    page = client.get("/settings/claude/").content.decode()
    assert "create_invoice" in page and "September 2026 is closed." in page
