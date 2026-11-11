from pathlib import Path

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backup.service import BackupError, restore_archive


class Command(BaseCommand):
    help = (
        "Decrypt a backup to a new database file and, with --media-to, a new folder of uploaded files. "
        "Never overwrites anything that exists."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="Backup file (full path, or a file name in the backup folder)")
        parser.add_argument("--to", required=True, help="Where to write the restored database")
        parser.add_argument("--media-to", help="Where to write the restored uploaded files (a new or empty folder)")

    def handle(self, *args, file, to, media_to=None, **options):
        source = Path(file)
        if not source.exists():
            source = Path(settings.DNB_BACKUP_DIR) / file
        if not source.exists():
            raise CommandError(f"Backup file {file} not found.")
        target = Path(to)
        media_target = Path(media_to) if media_to else None
        try:
            had_media = restore_archive(source, target, media_target, settings.DNB_BACKUP_KEY)
        except (InvalidToken, ValueError):
            target.unlink(missing_ok=True)
            raise CommandError(
                "Couldn't decrypt the backup. The key in .env (DNB_BACKUP_KEY) is not the one it was made with."
            ) from None
        except BackupError as exc:
            raise CommandError(f"Couldn't restore: {exc}") from None
        self.stdout.write(f"Restored the database to {target}.")
        if media_target and had_media:
            self.stdout.write(f"Restored uploaded files to {media_target}.")
        elif media_target:
            self.stdout.write("This is an old backup with no uploaded files; the folder was not created.")
        elif had_media:
            self.stdout.write("This backup also holds uploaded files. Add --media-to <new folder> to restore them.")
        media_root = Path(settings.MEDIA_ROOT)
        self.stdout.write("To use it:")
        self.stdout.write("  1. Stop the app.")
        self.stdout.write(f"  2. Rename the current database ({settings.DNB_DB_PATH}) so you keep it.")
        self.stdout.write(f"  3. Move {target} to {settings.DNB_DB_PATH}.")
        step = 4
        if media_target and had_media:
            self.stdout.write(f"  4. Rename the current uploaded files folder ({media_root}) so you keep it.")
            self.stdout.write(f"  5. Move {media_target} to {media_root}.")
            step = 6
        self.stdout.write(f"  {step}. Start the app.")
