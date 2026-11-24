"""Off-site copies through rclone. Only used when DNB_BACKUP_REMOTE is set."""
import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from django.conf import settings

TIMEOUT_SECONDS = 15 * 60

# rclone prints a JSON stats line every second. Rate-limit retries ("low level retry") are only
# logged at debug level (rclone fs/pacer.go), so the upload runs with --log-level=DEBUG to count them.
PROGRESS_FLAGS = ("--use-json-log", "--stats=1s", "--stats-log-level=NOTICE", "--log-level=DEBUG")
TIMESTAMP_RE = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ")
RETRY_RE = re.compile(r"low level retry \d+/\d+ \(error (.*)\)\s*$")


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


@dataclass
class UploadProgress:
    bytes_done: int = 0
    bytes_total: int = 0
    speed: float = 0.0  # bytes per second
    eta: int | None = None  # seconds
    retries: int = 0  # requests the remote refused and rclone retried (rate limits, server errors)
    last_retry: str = ""


def size_text(n: int) -> str:
    """Decimal units, the way Google Drive and most people count."""
    if n < 1000:
        return f"{n} bytes"
    for unit, size in (("KB", 1e3), ("MB", 1e6), ("GB", 1e9)):
        if n < size * 1000 or unit == "GB":
            return f"{n / size:.1f} {unit}"


def _timeout_message(p: UploadProgress) -> str:
    message = f"rclone took longer than {int(TIMEOUT_SECONDS // 60)} minutes."
    if p.bytes_total:
        message += f" It sent {size_text(p.bytes_done)} of {size_text(p.bytes_total)}."
    if p.retries:
        message += f" Google asked it to slow down {p.retries} time{'' if p.retries == 1 else 's'}."
    return message


def _plain_error(line: str) -> str:
    line = TIMESTAMP_RE.sub("", line.strip())
    return line.split("ERROR : ", 1)[-1]


def upload(path: Path, on_progress=None) -> None:
    """Copy one file to the remote. on_progress, when given, is called with an UploadProgress
    every time rclone reports its stats (about once a second)."""
    try:
        proc = subprocess.Popen(
            [*settings.DNB_RCLONE, "copy", str(path), _target(), *PROGRESS_FLAGS],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
    except FileNotFoundError:
        raise RemoteError("rclone is not installed.") from None
    lines: queue.Queue = queue.Queue()

    def read():
        for line in proc.stderr:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    state = UploadProgress()
    plain, errors = [], []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            proc.wait()
            raise RemoteError(_timeout_message(state))
        try:
            line = lines.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        try:
            entry = json.loads(line)
        except ValueError:
            if line.strip():
                plain.append(line)
            continue
        if not isinstance(entry, dict):
            continue
        msg = str(entry.get("msg", ""))
        if match := RETRY_RE.search(msg):
            state.retries += 1
            state.last_retry = match.group(1)[:300]
        elif entry.get("level") == "error":
            errors.append(msg)
        stats = entry.get("stats")
        if isinstance(stats, dict):
            state.bytes_done = int(stats.get("bytes") or 0)
            state.bytes_total = int(stats.get("totalBytes") or 0)
            state.speed = float(stats.get("speed") or 0)
            state.eta = stats.get("eta")
            if on_progress:
                on_progress(replace(state))
    proc.wait()
    if proc.returncode != 0:
        if plain:
            raise RemoteError(_plain_error(plain[-1]))
        raise RemoteError(errors[-1] if errors else f"rclone exited with code {proc.returncode}")


def delete(names: list[str]) -> None:
    for name in names:
        _rclone("deletefile", _target(name))


def download(name: str, dest: Path) -> None:
    _rclone("copyto", _target(name), str(dest))
