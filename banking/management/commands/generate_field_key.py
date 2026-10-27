from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print a new key for encrypting Plaid access tokens."

    def handle(self, *args, **options):
        self.stdout.write(Fernet.generate_key().decode())
        self.stdout.write("Add this to .env as DNB_FIELD_KEY and store a copy in your password manager.")
