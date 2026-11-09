"""The Attachments panel on bills, customer payments, and vendor payments."""
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from attachments.models import Attachment
from attachments.service import active, add_files

PDF = b"%PDF-1.4\n% bill\n"
JPEG = b"\xff\xd8\xff\xe0photo"


def pages(docs):
    return [
        ("bill", docs.bill, f"/purchasing/bills/{docs.bill.number}/"),
        ("payment", docs.payment, f"/sales/payments/{docs.payment.number}/"),
        ("vendor-payment", docs.vendor_payment, f"/purchasing/payments/{docs.vendor_payment.number}/"),
    ]


def post_files(client, kind, number, *files):
    return client.post(f"/attachments/add/{kind}/{number}/", {"files": list(files)}, follow=True)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_panel_empty_then_with_a_file(client, attachment_docs, standard_user, index):
    kind, target, url = pages(attachment_docs)[index]
    client.force_login(standard_user)
    page = client.get(url).content.decode()
    assert "Attachments" in page and "No files attached." in page and "Add files" in page
    response = post_files(client, kind, target.number, SimpleUploadedFile("receipt.pdf", PDF))
    html = response.content.decode()
    assert response.redirect_chain[-1][0].endswith(url + "#attachments")
    assert "Added receipt.pdf." in html and "receipt.pdf" in html and "No files attached." not in html
    assert active(target).count() == 1


def test_refused_file_shows_its_message_and_good_files_still_go_in(client, attachment_docs, standard_user):
    client.force_login(standard_user)
    response = post_files(client, "bill", attachment_docs.bill.number,
                          SimpleUploadedFile("notes.txt", b"plain"), SimpleUploadedFile("photo.jpg", JPEG))
    html = response.content.decode()
    assert "notes.txt: This file type can&#x27;t be attached. Use PDF, JPEG, or PNG." in html
    assert "Added photo.jpg." in html
    assert [a.original_name for a in active(attachment_docs.bill)] == ["photo.jpg"]


def test_empty_submission(client, attachment_docs, standard_user):
    client.force_login(standard_user)
    response = client.post(f"/attachments/add/bill/{attachment_docs.bill.number}/", {}, follow=True)
    assert "Choose a file to add." in response.content.decode()


def test_unknown_document_is_404(client, attachment_docs, standard_user):
    client.force_login(standard_user)
    response = client.post("/attachments/add/bill/BILL-99999/", {"files": [SimpleUploadedFile("a.pdf", PDF)]})
    assert response.status_code == 404
    assert client.post("/attachments/add/invoice/INV-1/", {}).status_code == 404


def test_standard_cannot_remove(client, attachment_docs, standard_user):
    (a,) = add_files(standard_user, attachment_docs.bill, [SimpleUploadedFile("a.pdf", PDF)]).added
    client.force_login(standard_user)
    page = client.get(f"/purchasing/bills/{attachment_docs.bill.number}/").content.decode()
    assert f"/attachments/{a.pk}/remove/" not in page
    assert client.get(f"/attachments/{a.pk}/remove/").status_code == 403
    assert client.post(f"/attachments/{a.pk}/remove/", {"reason": "x"}).status_code == 403


def test_admin_removes_with_a_reason(client, attachment_docs, admin_user):
    (a,) = add_files(admin_user, attachment_docs.bill, [SimpleUploadedFile("a.pdf", PDF)]).added
    client.force_login(admin_user)
    page = client.get(f"/attachments/{a.pk}/remove/").content.decode()
    assert "Remove file" in page and "Reason" in page
    missing = client.post(f"/attachments/{a.pk}/remove/", {"reason": "  "})
    assert "Enter a reason for removing this file." in missing.content.decode()
    done = client.post(f"/attachments/{a.pk}/remove/", {"reason": "Wrong bill"}, follow=True)
    html = done.content.decode()
    assert "Removed a.pdf." in html and "Removed files (1)" in html and "Wrong bill" in html
    assert "No files attached." in html


def test_removed_files_list_is_for_admins(client, attachment_docs, admin_user, standard_user):
    from attachments.service import remove

    (a,) = add_files(admin_user, attachment_docs.bill, [SimpleUploadedFile("a.pdf", PDF)]).added
    remove(admin_user, a, "Wrong bill")
    client.force_login(standard_user)
    assert "Removed files" not in client.get(f"/purchasing/bills/{attachment_docs.bill.number}/").content.decode()


def test_voided_documents_still_take_files(client, attachment_docs, admin_user):
    from purchasing.posting.bills import void_bill
    from purchasing.posting.payments import void_vendor_payment
    from sales.posting.receipts import void_payment

    void_vendor_payment(attachment_docs.vendor_payment, user=admin_user, reason="Test", date=date(2026, 11, 5))
    void_bill(attachment_docs.bill, user=admin_user, reason="Test", date=date(2026, 11, 5))
    void_payment(attachment_docs.payment, user=admin_user, reason="Test", date=date(2026, 11, 5))
    client.force_login(admin_user)
    for kind, target, _url in pages(attachment_docs):
        post_files(client, kind, target.number, SimpleUploadedFile("a.pdf", PDF))
        assert active(target).count() == 1


def test_bill_edit_keeps_attachments(attachment_docs, admin_user, freight):
    from purchasing.posting.bills import BillExpenseSpec, edit_bill

    add_files(admin_user, attachment_docs.bill, [SimpleUploadedFile("a.pdf", PDF)])
    edit_bill(attachment_docs.bill, vendor=attachment_docs.bill.vendor, date=attachment_docs.bill.date,
              vendor_invoice_number="A-100", user=admin_user, reason="Typo", item_lines=[],
              expense_lines=[BillExpenseSpec(freight, Decimal("85.00"), "Freight")])
    attachment_docs.bill.refresh_from_db()
    assert attachment_docs.bill.revision == 2
    assert Attachment.objects.filter(target_id=attachment_docs.bill.pk).count() == 1
    assert active(attachment_docs.bill).count() == 1
