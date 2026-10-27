from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from core.roles import ADMIN_GROUP, STANDARD_GROUP

# model label -> actions Standard gets
STANDARD_PERMISSIONS = {
    "ledger.account": ("add", "change", "view"),
    "ledger.journalentry": ("add", "change", "view"),
    "ledger.journalline": ("add", "change", "view"),
    "ledger.accountingperiod": ("view",),
    "items.item": ("add", "change", "view"),
    "items.category": ("add", "change", "view"),
    "items.stockcount": ("add", "change", "view"),
    "items.stockcountline": ("add", "change", "view"),
    "items.stockmovement": ("view",),
    "items.inventorysettings": ("view",),
    "contacts.customer": ("add", "change", "view"),
    "contacts.vendor": ("add", "change", "view"),
    "banking.banktransaction": ("add", "change", "view"),
    "banking.match": ("add", "change", "view"),
    "banking.rule": ("add", "change", "view"),
    "banking.transfer": ("add", "change", "view"),
    "banking.checknumber": ("add", "change", "view"),
    "banking.bankaccount": ("view",),
    "banking.plaiditem": ("view",),
    "banking.reconciliation": ("view",),
    "banking.reconciliationitem": ("view",),
    "banking.checklayout": ("view",),
}
ADMIN_APPS = ("ledger", "backup", "items", "contacts", "sales", "purchasing", "banking")


class Command(BaseCommand):
    help = "Create the Admin and Standard groups and assign permissions. Safe to run again."

    def _perms(self, label, actions):
        try:
            model = apps.get_model(label)
        except LookupError:
            self.stderr.write(f"Warning: {label} does not exist yet, skipping.")
            return []
        return list(
            Permission.objects.filter(
                content_type__app_label=model._meta.app_label,
                content_type__model=model._meta.model_name,
                codename__in=[f"{a}_{model._meta.model_name}" for a in actions],
            )
        )

    def handle(self, *args, **options):
        admin, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
        standard, _ = Group.objects.get_or_create(name=STANDARD_GROUP)

        standard_perms = []
        for label, actions in STANDARD_PERMISSIONS.items():
            standard_perms += self._perms(label, actions)
        standard.permissions.set(standard_perms)

        admin_perms = list(
            Permission.objects.filter(content_type__app_label__in=ADMIN_APPS)
            | Permission.objects.filter(content_type__app_label="auth", content_type__model__in=("user", "group"))
        )
        admin.permissions.set(admin_perms)
        self.stdout.write("Roles set up: Admin, Standard.")
