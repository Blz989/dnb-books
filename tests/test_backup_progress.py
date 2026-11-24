"""Manual backups with live progress (plan 2026-11-24-manual-backup-progress)."""
import sys
from pathlib import Path

import pytest

from backup import remote

FAKE_RCLONE = [sys.executable, str(Path(__file__).with_name("fake_rclone.py"))]


@pytest.fixture
def fake_remote(tmp_path, settings, monkeypatch):
    folder = tmp_path / "drive"
    folder.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(folder))
    settings.DNB_RCLONE = FAKE_RCLONE
    settings.DNB_BACKUP_REMOTE = "fake:dnb-backups"
    settings.DNB_BACKUP_DIR = local
    return folder / "dnb-backups"


# ---- Task 1: streamed rclone upload ----

def test_upload_reports_progress_and_retries(fake_remote, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_RETRIES", "3")
    src = tmp_path / "file.bin"
    src.write_bytes(b"x" * 4000)
    seen = []
    remote.upload(src, on_progress=seen.append)
    assert (fake_remote / "file.bin").exists()
    assert [p.bytes_done for p in seen] == [1000, 2000, 3000, 4000]
    assert all(p.bytes_total == 4000 for p in seen)
    assert seen[-1].retries == 3
    assert "Rate Limit Exceeded" in seen[-1].last_retry


def test_upload_without_callback_still_works(fake_remote, tmp_path):
    src = tmp_path / "file.bin"
    src.write_bytes(b"abc")
    remote.upload(src)
    assert (fake_remote / "file.bin").read_bytes() == b"abc"


def test_upload_timeout_says_how_far_it_got(fake_remote, tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "TIMEOUT_SECONDS", 1.5)
    monkeypatch.setenv("FAKE_RCLONE_SLOW", "0.6")
    monkeypatch.setenv("FAKE_RCLONE_RETRIES", "2")
    src = tmp_path / "file.bin"
    src.write_bytes(b"x" * 4_000_000)
    with pytest.raises(remote.RemoteError) as exc:
        remote.upload(src)
    message = str(exc.value)
    assert message.startswith("rclone took longer than ")
    assert "It sent 2.0 MB of 4.0 MB." in message
    assert message.endswith("Google asked it to slow down 2 times.")
    assert not (fake_remote / "file.bin").exists()


def test_upload_error_text_from_json_log(fake_remote, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "Failed to copy: couldn't connect to Google Drive")
    src = tmp_path / "file.bin"
    src.write_bytes(b"x")
    with pytest.raises(remote.RemoteError) as exc:
        remote.upload(src)
    assert str(exc.value) == "Failed to copy: couldn't connect to Google Drive"


def test_upload_missing_rclone(settings, tmp_path):
    settings.DNB_RCLONE = [str(tmp_path / "no-such-rclone")]
    settings.DNB_BACKUP_REMOTE = "fake:x"
    src = tmp_path / "file.bin"
    src.write_bytes(b"x")
    with pytest.raises(remote.RemoteError, match="rclone is not installed."):
        remote.upload(src)


@pytest.mark.parametrize("n,text", [(0, "0 bytes"), (900, "900 bytes"), (12_400_000, "12.4 MB"), (2_500_000_000, "2.5 GB")])
def test_size_text(n, text):
    assert remote.size_text(n) == text
