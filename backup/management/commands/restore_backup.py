from pathlib import Path

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backup.service import decrypt_file


class Command(BaseCommand):
    help = "Decrypt a backup file to a new database file. Never overwrites an existing file."

    def add_arguments(self, parser):
        parser.add_argument("file", help="Backup file (full path, or a file name in the backup folder)")
        parser.add_argument("--to", required=True, help="Where to write the restored database")

    def handle(self, *args, file, to, **options):
        source = Path(file)
        if not source.exists():
            source = Path(settings.DNB_BACKUP_DIR) / file
        if not source.exists():
            raise CommandError(f"Backup file {file} not found.")
        target = Path(to)
        if target.exists():
            raise CommandError(f"{target} already exists. Choose a new file name; nothing was overwritten.")
        try:
            decrypt_file(source, target, settings.DNB_BACKUP_KEY)
        except (InvalidToken, ValueError):
            target.unlink(missing_ok=True)
            raise CommandError(
                "Couldn't decrypt the backup. The key in .env (DNB_BACKUP_KEY) is not the one it was made with."
            ) from None
        self.stdout.write(f"Restored to {target}.")
        self.stdout.write("To use it:")
        self.stdout.write("  1. Stop the app.")
        self.stdout.write(f"  2. Rename the current database ({settings.DNB_DB_PATH}) so you keep it.")
        self.stdout.write(f"  3. Move {target} to {settings.DNB_DB_PATH}.")
        self.stdout.write("  4. Start the app.")
