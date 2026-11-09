"""Upload links: the page Claude points to, files held until the change is confirmed, and the daily cleanup."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from attachments import links
from attachments.models import Attachment, Source
from attachments.service import active
from mcpserver.models import PendingChange

PDF = b"%PDF-1.4\n% receipt\n"
JPEG = b"\xff\xd8\xff\xe0photo"


@pytest.fixture(autouse=True)
def app_url(settings):
    settings.DNB_APP_URL = "https://books.example.com"


def pending(user, tool="create_bill", minutes=15, **payload):
    payload = payload or {"vendor": "Acme Supply"}
    return PendingChange.objects.create(
        id=f"pc-{PendingChange.objects.count() + 1}", user=user, tool=tool,
        payload={"input": payload, "flags": {}}, expires_at=timezone.now() + timedelta(minutes=minutes))


def upload(client, link, *files):
    return client.post(f"/attach/{link.token}/", {"files": list(files)})


def test_url_uses_the_app_address(admin_user, db):
    link = links.make_link(admin_user, pending_change=pending(admin_user))
    assert links.url(link) == f"https://books.example.com/attach/{link.token}/"
    assert len(link.token) >= 40
    assert link.expires_at - link.created_at == timedelta(minutes=60)


def test_links_off_without_app_url(settings):
    settings.DNB_APP_URL = ""
    assert not links.enabled()


def test_make_link_needs_exactly_one_purpose(admin_user, attachment_docs):
    with pytest.raises(ValueError):
        links.make_link(admin_user)
    with pytest.raises(ValueError):
        links.make_link(admin_user, pending_change=pending(admin_user), target=attachment_docs.bill)


def test_page_needs_sign_in(client, admin_user, db):
    link = links.make_link(admin_user, pending_change=pending(admin_user))
    response = client.get(f"/attach/{link.token}/")
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_unknown_token_is_404(client, admin_user):
    client.force_login(admin_user)
    assert client.get("/attach/not-a-token/").status_code == 404


def test_someone_elses_link(client, admin_user, standard_user, db):
    link = links.make_link(admin_user, pending_change=pending(admin_user))
    client.force_login(standard_user)
    html = client.get(f"/attach/{link.token}/").content.decode()
    assert "This upload link belongs to someone else." in html and 'type="file"' not in html
    upload(client, link, SimpleUploadedFile("a.pdf", PDF))
    assert not Attachment.objects.exists()


def test_expired_link(client, admin_user, db):
    link = links.make_link(admin_user, pending_change=pending(admin_user))
    type(link).objects.filter(pk=link.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    client.force_login(admin_user)
    html = client.get(f"/attach/{link.token}/").content.decode()
    assert "This upload link expired. Ask Claude for a new one." in html
    upload(client, link, SimpleUploadedFile("a.pdf", PDF))
    assert not Attachment.objects.exists()


def test_preview_expired_without_confirm(client, admin_user, db):
    change = pending(admin_user, minutes=-1)
    link = links.make_link(admin_user, pending_change=change)
    client.force_login(admin_user)
    html = client.get(f"/attach/{link.token}/").content.decode()
    assert "That preview expired, so there&#x27;s nothing to attach to. Ask Claude for it again." in html
    upload(client, link, SimpleUploadedFile("a.pdf", PDF))
    assert not Attachment.objects.exists()


@pytest.mark.parametrize("tool,payload,heading", [
    ("create_bill", {"vendor": "Acme Supply"}, "Attach files to the bill for Acme Supply (preview, not saved yet)"),
    ("receive_payment", {"customer": "Oak Builders"},
     "Attach files to the payment from Oak Builders (preview, not saved yet)"),
    ("pay_bills", {"vendor": "Acme Supply"}, "Attach files to the payment to Acme Supply (preview, not saved yet)"),
])
def test_heading_for_a_preview(client, admin_user, db, tool, payload, heading):
    link = links.make_link(admin_user, pending_change=pending(admin_user, tool=tool, **payload))
    client.force_login(admin_user)
    assert heading in client.get(f"/attach/{link.token}/").content.decode()


def test_before_confirm_files_are_held(client, admin_user, db):
    change = pending(admin_user)
    link = links.make_link(admin_user, pending_change=change)
    client.force_login(admin_user)
    html = upload(client, link, SimpleUploadedFile("receipt.pdf", PDF)).content.decode()
    assert "Added receipt.pdf. Go back to Claude and confirm the change." in html
    (a,) = Attachment.objects.all()
    assert a.target_id is None and a.upload_link == link and a.source == Source.CLAUDE_LINK


def test_after_confirm_files_go_to_the_document(client, admin_user, attachment_docs):
    change = pending(admin_user)
    link = links.make_link(admin_user, pending_change=change)
    PendingChange.objects.filter(pk=change.pk).update(used_at=timezone.now(), document=attachment_docs.bill.number)
    client.force_login(admin_user)
    html = upload(client, link, SimpleUploadedFile("receipt.pdf", PDF)).content.decode()
    assert f"Added receipt.pdf to {attachment_docs.bill.number}." in html
    assert [a.original_name for a in active(attachment_docs.bill)] == ["receipt.pdf"]


def test_link_for_an_existing_document(client, admin_user, attachment_docs):
    link = links.make_link(admin_user, target=attachment_docs.vendor_payment)
    client.force_login(admin_user)
    page = client.get(f"/attach/{link.token}/").content.decode()
    assert f"Attach files to {attachment_docs.vendor_payment.number}" in page
    html = upload(client, link, SimpleUploadedFile("remit.jpg", JPEG)).content.decode()
    assert f"Added remit.jpg to {attachment_docs.vendor_payment.number}." in html
    assert active(attachment_docs.vendor_payment).count() == 1


def test_refused_file_on_the_link_page(client, admin_user, db):
    link = links.make_link(admin_user, pending_change=pending(admin_user))
    client.force_login(admin_user)
    html = upload(client, link, SimpleUploadedFile("notes.txt", b"text")).content.decode()
    assert "notes.txt: This file type can&#x27;t be attached. Use PDF, JPEG, or PNG." in html


def test_attach_held_moves_only_that_changes_files(client, admin_user, attachment_docs):
    first, second = pending(admin_user), pending(admin_user)
    link_a = links.make_link(admin_user, pending_change=first)
    link_b = links.make_link(admin_user, pending_change=second)
    client.force_login(admin_user)
    upload(client, link_a, SimpleUploadedFile("a.pdf", PDF))
    upload(client, link_b, SimpleUploadedFile("b.jpg", JPEG))
    assert links.attach_held(first, attachment_docs.bill) == 1
    assert [a.original_name for a in active(attachment_docs.bill)] == ["a.pdf"]
    assert Attachment.objects.get(original_name="b.jpg").target_id is None


def test_clean_expired(client, admin_user, attachment_docs, settings):
    stale, fresh = pending(admin_user), pending(admin_user)
    stale_link = links.make_link(admin_user, pending_change=stale)
    fresh_link = links.make_link(admin_user, pending_change=fresh)
    client.force_login(admin_user)
    upload(client, stale_link, SimpleUploadedFile("stale.pdf", b"%PDF-stale"))
    upload(client, stale_link, SimpleUploadedFile("shared.jpg", JPEG))
    upload(client, fresh_link, SimpleUploadedFile("fresh.pdf", PDF))
    from attachments.service import add_files

    (kept,) = add_files(admin_user, attachment_docs.bill, [SimpleUploadedFile("same.jpg", JPEG)]).added
    stale_file = Path(settings.MEDIA_ROOT) / Attachment.objects.get(original_name="stale.pdf").file.name

    later = timezone.now() + timedelta(minutes=20)  # stale's preview (15 min) is over, unconfirmed
    PendingChange.objects.filter(pk=fresh.pk).update(expires_at=later + timedelta(minutes=5))
    assert links.clean_expired(now=later) == 2
    names = set(Attachment.objects.values_list("original_name", flat=True))
    assert names == {"fresh.pdf", "same.jpg"}
    assert not stale_file.exists()
    assert (Path(settings.MEDIA_ROOT) / kept.file.name).exists()  # shared with an attached copy


def test_clean_uploads_command(admin_user, db):
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("clean_uploads", stdout=out)
    assert out.getvalue().strip() == "Removed 0 files from expired previews."


def test_scheduler_runs_cleanup_daily(settings):
    from backup.management.commands.run_scheduler import upcoming_jobs

    settings.DNB_BACKUP_TIME = "01:15"
    jobs = upcoming_jobs(datetime(2026, 11, 9, 12, 0))
    cleanup = [when for when, command in jobs if command == "clean_uploads"]
    assert cleanup == [datetime(2026, 11, 10, 1, 0)]
