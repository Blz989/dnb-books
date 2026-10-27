from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from banking import plaid_client
from banking.models import PlaidItem
from banking.sync import sync_item

SANDBOX_INSTITUTION = "ins_109508"  # First Platypus Bank


class Command(BaseCommand):
    help = "Manual check of the Plaid integration against Plaid Sandbox: link, exchange, sync. Not run by pytest."

    def handle(self, *args, **options):
        if settings.PLAID_ENV != "sandbox":
            raise CommandError("This check only runs with PLAID_ENV=sandbox.")
        if not plaid_client.plaid_configured():
            raise CommandError("Plaid isn't set up. Add PLAID_CLIENT_ID and PLAID_SECRET to .env.")
        client = plaid_client.get_client()
        public_token = client.create_sandbox_public_token(SANDBOX_INSTITUTION)
        access_token, item_id = client.exchange_public_token(public_token)
        accounts = client.get_accounts(access_token)
        item = PlaidItem(institution_name="First Platypus Bank (Sandbox)", plaid_item_id=item_id)
        item.set_access_token(access_token)
        item.save()
        self.stdout.write(f"Linked {len(accounts)} sandbox accounts. Map them to bank accounts to sync.")
        result = sync_item(item)
        self.stdout.write(
            f"Sync: {result.added} added, {result.modified} modified, {result.removed} removed. "
            f"Item status: {item.status}.")
