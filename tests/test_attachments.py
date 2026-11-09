"""Attachments: file checks, storage by SHA-256, removal, and signed-in serving."""
import hashlib
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile

from attachments.files import MAX_BYTES, sniff
from attachments.models import Attachment, Kind
from attachments.service import AttachmentError, active, add_files, remove, removed
from ledger.models import JournalEntry, JournalLine

PDF = b"%PDF-1.4\n% a bill\n"
JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"image" * 10


def upload(name, data, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def test_sniff_each_type():
    assert sniff(PDF) == Kind.PDF
    assert sniff(JPEG) == Kind.JPEG
    assert sniff(PNG) == Kind.PNG
    assert sniff(b"MZ\x90\x00") is None
    assert sniff(b"hello") is None


def test_type_comes_from_content_not_name(attachment_docs, admin_user):
    result = add_files(admin_user, attachment_docs.bill, [upload("bill.pdf", PNG, "application/pdf")])
    (a,) = result.added
    assert a.kind == Kind.PNG and a.original_name == "bill.pdf"
    assert a.file.name.endswith(f"{hashlib.sha256(PNG).hexdigest()}.png")


def test_other_types_are_refused(attachment_docs, admin_user):
    result = add_files(admin_user, attachment_docs.bill, [
        upload("setup.pdf", b"MZ\x90\x00program"), upload("notes.txt", b"plain text")])
    assert not result.added
    assert result.refused == [
        ("setup.pdf", "This file type can't be attached. Use PDF, JPEG, or PNG."),
        ("notes.txt", "This file type can't be attached. Use PDF, JPEG, or PNG."),
    ]


def test_size_limit(attachment_docs, admin_user):
    big = PDF + b"0" * (MAX_BYTES - len(PDF) + 1)
    result = add_files(admin_user, attachment_docs.bill, [upload("big.pdf", big)])
    assert result.refused == [("big.pdf", "This file is larger than 10 MB. Save a smaller copy and try again.")]
    exact = PDF + b"0" * (MAX_BYTES - len(PDF))
    assert add_files(admin_user, attachment_docs.bill, [upload("ok.pdf", exact)]).added


def test_ten_files_per_document(attachment_docs, admin_user):
    files = [upload(f"p{i}.pdf", PDF + bytes([i])) for i in range(11)]
    result = add_files(admin_user, attachment_docs.bill, files)
    assert len(result.added) == 10
    assert result.refused == [("p10.pdf", "This document already has 10 files attached.")]


def test_same_file_twice_is_refused(attachment_docs, admin_user):
    add_files(admin_user, attachment_docs.bill, [upload("a.pdf", PDF)])
    result = add_files(admin_user, attachment_docs.bill, [upload("again.pdf", PDF)])
    assert result.refused == [("again.pdf", f"This file is already attached to {attachment_docs.bill.number}.")]


def test_documents_sharing_a_file_share_one_disk_file(attachment_docs, admin_user, settings):
    (a,) = add_files(admin_user, attachment_docs.bill, [upload("a.pdf", PDF)]).added
    (b,) = add_files(admin_user, attachment_docs.payment, [upload("b.pdf", PDF)]).added
    assert a.file.name == b.file.name
    assert len([p for p in Path(settings.MEDIA_ROOT).rglob("*") if p.is_file()]) == 1
    assert (Path(settings.MEDIA_ROOT) / a.file.name).read_bytes() == PDF
    assert a.size_bytes == len(PDF) and a.sha256 == hashlib.sha256(PDF).hexdigest()


def test_each_target_works_and_others_are_refused(attachment_docs, admin_user, customer):
    for target in (attachment_docs.bill, attachment_docs.payment, attachment_docs.vendor_payment):
        assert add_files(admin_user, target, [upload("x.jpg", JPEG)]).added
        assert active(target).count() == 1
    with pytest.raises(AttachmentError):
        add_files(admin_user, customer, [upload("x.jpg", JPEG)])


def test_remove_rules(attachment_docs, admin_user, standard_user, settings):
    (a,) = add_files(standard_user, attachment_docs.bill, [upload("a.pdf", PDF)]).added
    with pytest.raises(PermissionDenied):
        remove(standard_user, a, "wrong bill")
    with pytest.raises(AttachmentError, match="Enter a reason for removing this file."):
        remove(admin_user, a, "  ")
    remove(admin_user, a, "Wrong bill")
    a.refresh_from_db()
    assert a.removed_by == admin_user and a.removed_reason == "Wrong bill" and a.removed_at is not None
    assert list(active(attachment_docs.bill)) == [] and list(removed(attachment_docs.bill)) == [a]
    assert (Path(settings.MEDIA_ROOT) / a.file.name).exists()


def test_attachments_post_nothing(attachment_docs, admin_user):
    entries, lines = JournalEntry.objects.count(), JournalLine.objects.count()
    (a,) = add_files(admin_user, attachment_docs.bill, [upload("a.pdf", PDF)]).added
    remove(admin_user, a, "Wrong bill")
    assert (JournalEntry.objects.count(), JournalLine.objects.count()) == (entries, lines)


# ---- serving ----

def _added(user, target, name="a.pdf", data=PDF, content_type="application/pdf"):
    return add_files(user, target, [upload(name, data, content_type)]).added[0]


def test_serving_needs_sign_in(client, attachment_docs, admin_user):
    a = _added(admin_user, attachment_docs.bill)
    response = client.get(f"/attachments/{a.pk}/")
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_serving_uses_the_stored_kind_and_nosniff(client, attachment_docs, admin_user, standard_user):
    a = _added(admin_user, attachment_docs.bill, "photo.pdf", JPEG, "application/pdf")
    client.force_login(standard_user)
    response = client.get(f"/attachments/{a.pk}/")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Disposition"].startswith("inline")
    assert b"".join(response.streaming_content) == JPEG


def test_removed_file_is_for_admins_only(client, attachment_docs, admin_user, standard_user):
    a = _added(admin_user, attachment_docs.bill)
    remove(admin_user, a, "Wrong bill")
    client.force_login(standard_user)
    assert client.get(f"/attachments/{a.pk}/").status_code == 404
    client.force_login(admin_user)
    assert client.get(f"/attachments/{a.pk}/").status_code == 200


def test_no_plain_media_url(client, attachment_docs, admin_user):
    a = _added(admin_user, attachment_docs.bill)
    client.force_login(admin_user)
    assert client.get(f"/media/{a.file.name}").status_code == 404


def test_unknown_attachment_is_404(client, admin_user, db):
    client.force_login(admin_user)
    assert client.get("/attachments/999/").status_code == 404


def test_attachment_changes_are_audited(attachment_docs, admin_user):
    from auditlog.models import LogEntry

    a = _added(admin_user, attachment_docs.bill)
    assert LogEntry.objects.get_for_object(a).exists()
    assert Attachment.objects.count() == 1
