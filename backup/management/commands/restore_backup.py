from pathlib import Path

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backup.service import (
    FILENAME_RE,
    MEDIA_DIRNAME,
    decrypt_file,
    manifest_filename,
    read_manifest,
    restore_media,
)


class Command(BaseCommand):
    help = "Decrypt a backup file to a new database file. Never overwrites an existing file."

    def add_arguments(self, parser):
        parser.add_argument("file", help="Backup file (full path, or a file name in the backup folder)")
        parser.add_argument("--to", required=True, help="Where to write the restored database")
        parser.add_argument("--media-to", help="Also restore the uploaded files (attachments, logo) to this new folder")

    def handle(self, *args, file, to, media_to=None, **options):
        source = Path(file)
        if not source.exists():
            source = Path(settings.DNB_BACKUP_DIR) / file
        if not source.exists():
            raise CommandError(f"Backup file {file} not found.")
        target = Path(to)
        if target.exists():
            raise CommandError(f"{target} already exists. Choose a new file name; nothing was overwritten.")
        media_folder = Path(media_to) if media_to else None
        if media_folder is not None and media_folder.exists():
            raise CommandError(f"{media_folder} already exists. Choose a new folder; nothing was overwritten.")
        try:
            decrypt_file(source, target, settings.DNB_BACKUP_KEY)
        except (InvalidToken, ValueError):
            target.unlink(missing_ok=True)
            raise CommandError(
                "Couldn't decrypt the backup. The key in .env (DNB_BACKUP_KEY) is not the one it was made with."
            ) from None
        if media_folder is not None:
            m = FILENAME_RE.match(source.name)
            manifest = source.parent / manifest_filename(f"{m.group(1)}-{m.group(2)}") if m else None
            if manifest is None or not manifest.exists():
                raise CommandError(f"No file list was found next to {source.name}. The database was restored to "
                                   f"{target}, but the files were not.")
            try:
                files = read_manifest(manifest, settings.DNB_BACKUP_KEY)
                count = restore_media(files, source.parent / MEDIA_DIRNAME, media_folder, settings.DNB_BACKUP_KEY)
            except (InvalidToken, ValueError, OSError) as exc:
                raise CommandError(f"Couldn't restore the files: {exc}") from None
            self.stdout.write(f"Restored {count} files to {media_folder}.")
        self.stdout.write(f"Restored to {target}.")
        self.stdout.write("To use it:")
        self.stdout.write("  1. Stop the app.")
        self.stdout.write(f"  2. Rename the current database ({settings.DNB_DB_PATH}) so you keep it.")
        self.stdout.write(f"  3. Move {target} to {settings.DNB_DB_PATH}.")
        if media_folder is not None:
            self.stdout.write(f"     Move {media_folder} to {settings.MEDIA_ROOT}.")
        self.stdout.write("  4. Start the app.")
