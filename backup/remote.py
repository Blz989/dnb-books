"""Off-site copies through rclone. Only used when DNB_BACKUP_REMOTE is set."""
import subprocess
from pathlib import Path

from django.conf import settings

TIMEOUT_SECONDS = 15 * 60


class RemoteError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.DNB_BACKUP_REMOTE)


def _target(name: str = "") -> str:
    remote = settings.DNB_BACKUP_REMOTE.rstrip("/")
    return f"{remote}/{name}" if name else remote


def _rclone(*args: str) -> None:
    try:
        result = subprocess.run([*settings.DNB_RCLONE, *args], capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise RemoteError("rclone is not installed.") from None
    except subprocess.TimeoutExpired:
        raise RemoteError(f"rclone took longer than {TIMEOUT_SECONDS // 60} minutes.") from None
    if result.returncode != 0:
        lines = [l for l in result.stderr.strip().splitlines() if l.strip()]
        # rclone prefixes errors with "YYYY/MM/DD HH:MM:SS ERROR : "; keep only the message.
        message = lines[-1].split("ERROR : ", 1)[-1] if lines else f"rclone exited with code {result.returncode}"
        raise RemoteError(message)


def upload(path: Path) -> None:
    _rclone("copy", str(path), _target())


def delete(names: list[str]) -> None:
    for name in names:
        _rclone("deletefile", _target(name))


def download(name: str, dest: Path) -> None:
    _rclone("copyto", _target(name), str(dest))
