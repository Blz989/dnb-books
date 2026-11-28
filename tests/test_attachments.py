from datetime import date
from decimal import Decimal

import pytest
from auditlog.models import LogEntry
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core import attachments
from core.attachments import AttachmentError
from core.models import Attachment

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


def upload(name="bill.pdf", data=PDF):
    return SimpleUploadedFile(name, data)


def test_sniff_reads_first_bytes():
    assert attachments.sniff(PDF) == "application/pdf"
    assert attachments.sniff(PNG) == "image/png"
    assert attachments.sniff(JPEG) == "image/jpeg"
    assert attachments.sniff(b"MZ\x90\x00") is None
    assert attachments.sniff(b"") is None


def test_add_stores_file_with_original_name(vendor, standard_user, media):
    a = attachments.add(vendor, upload("W-9 2026.pdf"), user=standard_user)
    assert a.name == "W-9 2026.pdf"
    assert a.size == len(PDF)
    assert a.mime == "application/pdf"
    assert a.uploaded_by == standard_user
    assert a.file.name.startswith("attachments/")
    assert "W-9" not in a.file.name  # stored under a random name
    assert (media / a.file.name).read_bytes() == PDF
    assert list(attachments.for_object(vendor)) == [a]


@pytest.mark.parametrize("name,data,message", [
    ("empty.pdf", b"", "Choose a file to add."),
    ("evil.pdf", b"MZ\x90\x00 not a pdf", "Only PDF, PNG, and JPEG files can be added."),
    ("notes.txt", b"hello", "Only PDF, PNG, and JPEG files can be added."),
])
def test_add_refuses(vendor, standard_user, name, data, message):
    with pytest.raises(AttachmentError) as exc:
        attachments.add(vendor, upload(name, data), user=standard_user)
    assert str(exc.value) == message
    assert not Attachment.objects.exists()


def test_add_refuses_none(vendor, standard_user):
    with pytest.raises(AttachmentError, match="Choose a file to add."):
        attachments.add(vendor, None, user=standard_user)


def test_add_refuses_over_10_mb(vendor, standard_user, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_BYTES", 100)
    with pytest.raises(AttachmentError, match="That file is over 10 MB. Save it smaller and try again."):
        attachments.add(vendor, upload(data=PDF + b"x" * 200), user=standard_user)


def test_only_admin_removes(vendor, standard_user, admin_user, media):
    a = attachments.add(vendor, upload(), user=standard_user)
    with pytest.raises(AttachmentError, match="Only an admin can remove an attachment."):
        attachments.remove(a, user=standard_user)
    path = media / a.file.name
    attachments.remove(a, user=admin_user)
    assert not Attachment.objects.exists()
    assert not path.exists()


def test_add_and_remove_are_audited(vendor, admin_user):
    a = attachments.add(vendor, upload("receipt.png", PNG), user=admin_user)
    attachments.remove(a, user=admin_user)
    entries = LogEntry.objects.get_for_model(Attachment)
    assert entries.count() == 2
    assert "receipt.png" in str(entries.order_by("timestamp").first().changes)


# ---- views ----

def test_upload_view_and_download(client, vendor, standard_user):
    client.force_login(standard_user)
    page = f"/vendors/{vendor.pk}/"
    url = reverse("attachments:add", args=["vendor", vendor.pk])
    resp = client.post(url, {"file": upload("w9.pdf"), "next": page})
    assert resp.status_code == 302 and resp["Location"] == page
    a = Attachment.objects.get()
    resp = client.get(reverse("attachments:open", args=[a.pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/pdf"
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["Content-Disposition"].startswith("inline")
    assert b"".join(resp.streaming_content) == PDF


def test_upload_error_shows_on_record_page(client, vendor, standard_user):
    client.force_login(standard_user)
    page = f"/vendors/{vendor.pk}/"
    resp = client.post(reverse("attachments:add", args=["vendor", vendor.pk]),
                       {"file": upload("x.txt", b"hi"), "next": page}, follow=True)
    assert "Only PDF, PNG, and JPEG files can be added." in resp.content.decode()


def test_next_must_be_local(client, vendor, standard_user):
    client.force_login(standard_user)
    resp = client.post(reverse("attachments:add", args=["vendor", vendor.pk]),
                       {"file": upload(), "next": "https://evil.example/"})
    assert resp.status_code == 302 and resp["Location"] == "/"


def test_download_needs_sign_in(client, vendor, standard_user):
    a = attachments.add(vendor, upload(), user=standard_user)
    resp = client.get(reverse("attachments:open", args=[a.pk]))
    assert resp.status_code == 302 and "/accounts/login/" in resp["Location"]


def test_unknown_kind_is_404(client, standard_user):
    client.force_login(standard_user)
    assert client.post(reverse("attachments:add", args=["nope", 1]), {"file": upload()}).status_code == 404


def test_remove_view_admin_only(client, vendor, standard_user, admin_user):
    a = attachments.add(vendor, upload(), user=standard_user)
    url = reverse("attachments:remove", args=[a.pk])
    client.force_login(standard_user)
    assert client.post(url).status_code == 403
    client.force_login(admin_user)
    assert client.get(url).status_code == 200
    resp = client.post(url, {"next": f"/vendors/{vendor.pk}/"})
    assert resp.status_code == 302
    assert not Attachment.objects.exists()


def test_card_on_vendor_page(client, vendor, standard_user, admin_user):
    attachments.add(vendor, upload("W-9.pdf"), user=standard_user)
    client.force_login(standard_user)
    html = client.get(f"/vendors/{vendor.pk}/").content.decode()
    assert "Attachments" in html and "W-9.pdf" in html and 'type="file"' in html
    assert ">Remove<" not in html
    client.force_login(admin_user)
    assert ">Remove<" in client.get(f"/vendors/{vendor.pk}/").content.decode()


def test_card_on_bill_and_journal_pages(client, admin_user, vendor, purchasing_settings, expense, posted_entry):
    from purchasing.posting.bills import BillExpenseSpec, create_bill

    bill = create_bill(vendor=vendor, date=date(2026, 3, 1), vendor_invoice_number="A1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, Decimal("50.00"))])
    attachments.add(bill, upload("vendor-bill.pdf"), user=admin_user)
    attachments.add(posted_entry, upload("support.png", PNG), user=admin_user)
    client.force_login(admin_user)
    assert "vendor-bill.pdf" in client.get(f"/purchasing/bills/{bill.number}/").content.decode()
    assert "support.png" in client.get(reverse("books:journal-detail", args=[posted_entry.pk])).content.decode()


def test_every_kind_resolves():
    from django.apps import apps

    for kind, spec in attachments.KINDS.items():
        assert apps.get_model(spec.model)._meta.label == spec.model
        assert attachments.kind_of(apps.get_model(spec.model)()) == kind
