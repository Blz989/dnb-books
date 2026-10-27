from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print a new backup encryption key."

    def handle(self, *args, **options):
        self.stdout.write(Fernet.generate_key().decode())
        self.stdout.write(
            "Add this to .env as DNB_BACKUP_KEY and store a copy in your password manager. "
            "Backups cannot be restored without it."
        )
