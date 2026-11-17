"""Owner-managed payment terms: due-date rules, the settings page, dropdowns, imports, tools, and the migration."""
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from contacts.models import Customer, PaymentTerm, Terms, Vendor, due_date
from contacts.terms import default_term_code, find_term
from mcpserver import write
from mcpserver.runtime import Refused
from migration.zoho import mapping
from sales.posting.invoices import InvoiceLineSpec, create_invoice

D = Decimal

OLD_DAYS = {"DUE_ON_RECEIPT": 0, "NET_15": 15, "NET_30": 30, "NET_45": 45, "NET_60": 60}


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


def test_the_five_original_terms_are_seeded_with_the_same_days(db):
    for code, days in OLD_DAYS.items():
        term = PaymentTerm.objects.get(code=code)
        assert term.days == days and not term.end_of_month and term.is_active
        assert due_date(code, date(2026, 10, 7)) == date.fromordinal(date(2026, 10, 7).toordinal() + days)
    assert PaymentTerm.objects.get(code="DUE_EOM_NEXT").end_of_month


@pytest.mark.parametrize("months,days,on,expected", [
    (0, 0, date(2026, 10, 7), date(2026, 10, 31)),
    (1, 0, date(2026, 10, 7), date(2026, 11, 30)),
    (1, 0, date(2026, 12, 20), date(2027, 1, 31)),
    (0, 0, date(2028, 2, 3), date(2028, 2, 29)),
    (0, 10, date(2026, 10, 7), date(2026, 11, 10)),
])
def test_end_of_month_rules(db, months, days, on, expected):
    term = PaymentTerm.objects.create(code="X", name="X", end_of_month=True, months_after=months, days=days)
    assert due_date("X", on) == expected and term.due_on(on) == expected


def test_unknown_terms_are_refused(db):
    with pytest.raises(ValueError):
        due_date("NOPE", date(2026, 10, 7))


def test_owner_can_add_a_term_and_use_it_on_an_invoice(adm, customer, inv_settings, sales_settings, make_item, admin_user):
    resp = adm.post(reverse("settings:term-new"), {"name": "Net 10", "rule": "days", "days": "10", "is_active": "on"})
    assert resp.status_code == 302
    term = PaymentTerm.objects.get(name="Net 10")
    assert term.code == "NET_10" and term.days == 10 and term.is_active
    item = make_item("W", item_type="NON_STOCK", costing_method="", cogs_account=None, inventory_account=None)
    inv = create_invoice(customer=customer, date=date(2026, 10, 7), user=admin_user, terms="NET_10",
                         lines=[InvoiceLineSpec(item=item, quantity=1, price=D("5.00"))])
    assert inv.due_date == date(2026, 10, 17) and inv.get_terms_display() == "Net 10"


def test_new_term_validation(adm):
    PaymentTerm.objects.create(code="A", name="Net 10", days=10)
    page = adm.post(reverse("settings:term-new"), {"name": "net 10", "rule": "days", "days": "10"})
    assert "A term named net 10 already exists." in page.content.decode()
    page = adm.post(reverse("settings:term-new"), {"name": "", "rule": "days", "days": "1"})
    assert "Enter a name." in page.content.decode()


def test_standard_user_cannot_reach_the_terms_page(standard_user, client):
    client.force_login(standard_user)
    assert client.get(reverse("settings:terms")).status_code == 403
    assert client.post(reverse("settings:term-new"), {"name": "x", "rule": "days"}).status_code == 403


def test_a_term_in_use_is_locked_not_deletable_and_never_moves_due_dates(adm, customer, make_item, inv_settings, sales_settings, admin_user):
    item = make_item("W", item_type="NON_STOCK", costing_method="", cogs_account=None, inventory_account=None)
    inv = create_invoice(customer=customer, date=date(2026, 10, 7), user=admin_user,
                         lines=[InvoiceLineSpec(item=item, quantity=1, price=D("5.00"))])
    assert inv.due_date == date(2026, 11, 6)
    term = PaymentTerm.objects.get(code="NET_30")
    page = adm.get(reverse("settings:term-edit", args=[term.pk])).content.decode()
    assert "rule is locked" in page
    # Tampering with the locked fields is ignored; the name and active flag still change.
    adm.post(reverse("settings:term-edit", args=[term.pk]),
             {"name": "Net 30 days", "rule": "eom", "days": "5", "months_after": "1", "is_active": ""})
    term.refresh_from_db()
    assert term.name == "Net 30 days" and term.days == 30 and not term.end_of_month and not term.is_active
    inv.refresh_from_db()
    assert inv.due_date == date(2026, 11, 6) and inv.get_terms_display() == "Net 30 days"


def test_dropdowns_list_active_terms_and_keep_a_records_own_inactive_term(adm, customer):
    PaymentTerm.objects.filter(code="NET_60").update(is_active=False)
    PaymentTerm.objects.create(code="NET_10", name="Net 10", days=10)
    from contacts.forms import CustomerForm
    from sales.forms import InvoiceHeaderForm
    names = [name for _, name in InvoiceHeaderForm().fields["terms"].choices]
    assert "Net 10" in names and "Net 60" not in names and names[0] == "Customer's default"
    customer.terms = "NET_60"
    customer.save()
    form = CustomerForm(instance=customer)
    assert "Net 60" in [name for _, name in form.fields["terms"].choices]
    assert "Net 60" not in [name for _, name in CustomerForm().fields["terms"].choices]


def test_a_deactivated_term_is_refused_for_new_selection(adm, customer):
    from contacts.forms import CustomerForm
    PaymentTerm.objects.filter(code="NET_15").update(is_active=False)
    form = CustomerForm({"name": "Z", "terms": "NET_15"})
    assert not form.is_valid() and "terms" in form.errors


def test_zoho_labels_map_to_terms_including_new_ones(db):
    PaymentTerm.objects.create(code="NET_10", name="Net 10", days=10)
    PaymentTerm.objects.create(code="NET_90", name="Net 90", days=90)
    assert mapping.terms("Net 10") == "NET_10"
    assert mapping.terms("Net 90") == "NET_90"
    assert mapping.terms("Net 30") == "NET_30"
    assert mapping.terms("Due on Receipt") == "DUE_ON_RECEIPT"
    assert mapping.terms("Due end of the month") == "DUE_EOM"
    assert mapping.terms("Due end of next month") == "DUE_EOM_NEXT"
    assert mapping.terms("Net 20") == "NET_30"  # no Net 20: the next longer term, as before
    assert mapping.terms("Custom", "2026-10-01", "2026-10-11") == "NET_10"
    assert mapping.terms("") == "NET_30"
    with pytest.raises(ValueError):
        mapping.terms("Whenever")


def test_mcp_tools_accept_terms_by_name_or_code_including_new_ones(db, admin_user, customer, make_item, inv_settings):
    from mcpserver.models import McpAccess
    McpAccess.objects.create(user=admin_user, enabled=True)
    PaymentTerm.objects.create(code="NET_90", name="Net 90", days=90)
    p = write.preview(admin_user, "save_customer", {"name": "Birch Co", "fields": {"terms": "net 90"}})
    write.confirm(admin_user, p["confirmation_id"])
    assert Customer.objects.get(name="Birch Co").terms == "NET_90"
    p = write.preview(admin_user, "save_customer", {"name": "Cedar Co", "fields": {"terms": "NET_15"}})
    write.confirm(admin_user, p["confirmation_id"])
    assert Customer.objects.get(name="Cedar Co").terms == "NET_15"
    with pytest.raises(Refused) as err:
        write.preview(admin_user, "save_customer", {"name": "Dune Co", "fields": {"terms": "Net 7"}})
    assert "Choose a terms:" in str(err.value) and "Net 90" in str(err.value)
    # A new customer without terms gets Net 30, or the first active term when Net 30 is off.
    assert default_term_code() == "NET_30"
    PaymentTerm.objects.filter(code="NET_30").update(is_active=False)
    assert default_term_code() != "NET_30"


def test_migration_keeps_existing_terms_and_due_dates(transactional_db):
    """Roll the term tables back, store records under the old codes, migrate forward: nothing moves."""
    executor = MigrationExecutor(connection)
    executor.migrate([("contacts", "0002_vendor")])
    old = executor.loader.project_state([("contacts", "0002_vendor")]).apps
    OldCustomer, OldVendor = old.get_model("contacts", "Customer"), old.get_model("contacts", "Vendor")
    OldCustomer.objects.create(name="Old Customer", terms="NET_45")
    OldVendor.objects.create(name="Old Vendor", terms="DUE_ON_RECEIPT")
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert Customer.objects.get(name="Old Customer").terms == "NET_45"
    assert Customer.objects.get(name="Old Customer").get_terms_display() == "Net 45"
    assert Vendor.objects.get(name="Old Vendor").terms == "DUE_ON_RECEIPT"
    for code, days in OLD_DAYS.items():
        assert PaymentTerm.objects.get(code=code).days == days
    assert find_term("Net 45").code == "NET_45"
