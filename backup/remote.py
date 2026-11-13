"""Off-site copies through rclone. Only used when DNB_BACKUP_REMOTE is set."""
import subprocess
import time
from pathlib import Path

from django.conf import settings

# A healthy 130 MB upload takes seconds, so a stalled attempt is killed after 3 minutes and retried.
UPLOAD_ATTEMPT_SECONDS = 3 * 60
UPLOAD_ATTEMPTS = 3
UPLOAD_PAUSE_SECONDS = 20
# Other calls (delete, download) move almost no data; one attempt, generous limit.
TIMEOUT_SECONDS = 5 * 60
# rclone's own network limits, so a stalled connection fails fast instead of crawling.
UPLOAD_FLAGS = ("--timeout", "60s", "--contimeout", "30s", "--low-level-retries", "3", "--retries", "1")


class RemoteError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.DNB_BACKUP_REMOTE)


def _target(name: str = "") -> str:
    remote = settings.DNB_BACKUP_REMOTE.rstrip("/")
    return f"{remote}/{name}" if name else remote


def _rclone(*args: str, timeout: int = TIMEOUT_SECONDS) -> None:
    try:
        result = subprocess.run([*settings.DNB_RCLONE, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RemoteError("rclone is not installed.") from None
    except subprocess.TimeoutExpired:
        raise RemoteError(f"rclone took longer than {_duration(timeout)}.") from None
    if result.returncode != 0:
        lines = [l for l in result.stderr.strip().splitlines() if l.strip()]
        # rclone prefixes errors with "YYYY/MM/DD HH:MM:SS ERROR : "; keep only the message.
        message = lines[-1].split("ERROR : ", 1)[-1] if lines else f"rclone exited with code {result.returncode}"
        raise RemoteError(message)


def _duration(seconds: int) -> str:
    return f"{seconds // 60} minutes" if seconds % 60 == 0 else f"{seconds} seconds"


def upload(path: Path) -> None:
    """Copy one backup to the remote, retrying a stalled or failed attempt.

    rclone's Drive backend only shows a file once its upload completes, so a killed attempt leaves no
    partial file behind; the next attempt (or the next night) simply uploads the file again.
    """
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            _rclone("copy", *UPLOAD_FLAGS, str(path), _target(), timeout=UPLOAD_ATTEMPT_SECONDS)
            return
        except RemoteError:
            if attempt == UPLOAD_ATTEMPTS:
                raise
            time.sleep(UPLOAD_PAUSE_SECONDS)


def delete(names: list[str]) -> None:
    for name in names:
        _rclone("deletefile", _target(name))


def download(name: str, dest: Path) -> None:
    _rclone("copyto", _target(name), str(dest))
