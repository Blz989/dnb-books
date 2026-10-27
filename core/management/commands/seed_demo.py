"""Fill an EMPTY database with realistic sample data for screenshots and manual trials."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.roles import ADMIN_GROUP
from ledger.models import Account, AccountSubtype, AccountType, JournalEntry
from ledger.posting import LineSpec, post_entry

D = Decimal

ACCOUNTS = [
    ("1000", "Checking", AccountType.ASSET, AccountSubtype.BANK),
    ("1200", "Accounts Receivable", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE),
    ("1300", "Inventory Asset", AccountType.ASSET, AccountSubtype.INVENTORY),
    ("2000", "Accounts Payable", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE),
    ("3000", "Owner's Equity", AccountType.EQUITY, AccountSubtype.EQUITY),
    ("3900", "Opening Balance Equity", AccountType.EQUITY, AccountSubtype.EQUITY),
    ("4000", "Sales", AccountType.INCOME, AccountSubtype.INCOME),
    ("5000", "Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD),
    ("6000", "Rent", AccountType.EXPENSE, AccountSubtype.EXPENSE),
    ("6100", "Utilities", AccountType.EXPENSE, AccountSubtype.EXPENSE),
    ("6500", "Inventory Shrinkage", AccountType.EXPENSE, AccountSubtype.EXPENSE),
    ("6510", "Inventory Write-down", AccountType.EXPENSE, AccountSubtype.EXPENSE),
]


class Command(BaseCommand):
    help = "Create demo data (user demo / demo-pass-123). Only runs on an empty database."

    def handle(self, *args, **options):
        if JournalEntry.objects.exists():
            raise CommandError("seed_demo only runs on an empty database.")
        call_command("setup_roles", verbosity=0)
        User = get_user_model()
        user, _ = User.objects.get_or_create(username="demo", defaults={"is_staff": True})
        user.set_password("demo-pass-123")
        user.is_staff = True
        user.save()
        user.groups.add(Group.objects.get(name=ADMIN_GROUP))

        acct = {n: Account.objects.get_or_create(number=n, defaults={"name": name, "type": t, "subtype": s})[0]
                for n, name, t, s in ACCOUNTS}
        today = timezone.localdate()
        first = today.replace(day=1)

        def post(d, debit, credit, amount, memo):
            post_entry(date=d, user=user, memo=memo, lines=[
                LineSpec(acct[debit], debit=D(amount)), LineSpec(acct[credit], credit=D(amount))])

        post(first - timedelta(days=60), "1000", "3000", "40000.00", "Owner investment")
        post(first - timedelta(days=20), "1000", "4000", "9200.00", "Sales deposits")
        post(first - timedelta(days=15), "1200", "4000", "3150.00", "Invoices")
        post(first - timedelta(days=10), "6000", "1000", "2400.00", "Rent")
        post(first - timedelta(days=5), "6100", "1000", "310.45", "Utilities")
        from items.demo import seed_items

        seed_items(user)
        self.stdout.write("Demo data ready. Log in as demo / demo-pass-123.")
