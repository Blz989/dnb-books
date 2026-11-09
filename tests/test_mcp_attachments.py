"""Upload links in Claude previews, attaching on confirm, attach_file, and attachments in get_bill."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from mcp.server.mcpserver.exceptions import ToolError

from attachments.models import Attachment, Source, UploadLink
from attachments.service import active, add_files, remove
from ledger.periods import close_period
from mcpserver import read, write
from mcpserver.runtime import run
from purchasing.models import Bill

from .test_mcp import books, clerk, ctx_for, owner  # noqa: F401  (fixtures)

PDF = b"%PDF-1.4\n% receipt\n"
BILL = {"vendor": "Acme Supply", "date": "2026-10-02", "vendor_invoice_number": "R-1",
        "expense_lines": [{"account": "6150", "amount": "40.00"}]}


@pytest.fixture
def app_url(settings):
    settings.DNB_APP_URL = "https://books.example.com"


def token_of(preview):
    return preview["upload_link"].rstrip("/").rsplit("/", 1)[-1]


def upload_to(client, user, preview_or_url, data=PDF, name="receipt.pdf"):
    url = preview_or_url if isinstance(preview_or_url, str) else preview_or_url["upload_link"]
    client.force_login(user)
    return client.post(url.replace("https://books.example.com", ""), {"files": [SimpleUploadedFile(name, data)]})


def test_bill_preview_includes_the_link(books, owner, app_url):
    p = write.preview(owner, "create_bill", BILL)
    assert p["upload_link"].startswith("https://books.example.com/attach/")
    assert p["attach_note"] == f"To attach the bill or receipt, open this link: {p['upload_link']}"
    link = UploadLink.objects.get(token=token_of(p))
    assert link.pending_change_id == p["confirmation_id"] and link.user == owner
    assert p["upload_link_expires"] == link.expires_at.isoformat()


def test_payment_previews_include_the_link(books, owner, app_url):
    pay = write.preview(owner, "receive_payment", {"customer": "Acme Builders", "amount": "10.00",
                                                   "method": "check", "date": "2026-10-03"})
    assert "upload_link" in pay
    write.confirm(owner, write.preview(owner, "create_bill", BILL)["confirmation_id"])
    vp = write.preview(owner, "pay_bills", {"vendor": "Acme Supply", "method": "ach", "paid_from": "1000", "amount": "40.00",
                                            "date": "2026-10-04"})
    assert "upload_link" in vp


def test_other_previews_have_no_link(books, owner, app_url):
    p = write.preview(owner, "create_invoice", {"customer": "Acme Builders", "date": "2026-10-05",
                                                "lines": [{"sku": "SVC", "quantity": 1}]})
    assert "upload_link" not in p and "attach_note" not in p


def test_links_off_without_app_url(books, owner, settings):
    settings.DNB_APP_URL = ""
    p = write.preview(owner, "create_bill", BILL)
    assert "upload_link" not in p
    assert p["attach_note"] == "Upload links are off. An admin can set DNB_APP_URL."
    assert write.confirm(owner, p["confirmation_id"])["done"].endswith(".") and not UploadLink.objects.exists()


def test_upload_then_confirm_attaches_the_file(client, books, owner, app_url):
    p = write.preview(owner, "create_bill", BILL)
    assert upload_to(client, owner, p).status_code == 200
    done = run(ctx_for(owner), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    bill = Bill.objects.get(number=done["document"])
    (a,) = active(bill)
    assert a.original_name == "receipt.pdf" and a.source == Source.CLAUDE_LINK
    assert done["done"] == f"Saved {bill.number} with 1 file attached."


def test_two_files_say_files(client, books, owner, app_url):
    p = write.preview(owner, "create_bill", BILL)
    upload_to(client, owner, p)
    upload_to(client, owner, p, data=b"\xff\xd8\xffphoto", name="photo.jpg")
    done = write.confirm(owner, p["confirmation_id"])
    assert done["done"] == f"Saved {done['document']} with 2 files attached."


def test_confirm_without_upload_then_upload_after(client, books, owner, app_url):
    p = write.preview(owner, "create_bill", BILL)
    done = write.confirm(owner, p["confirmation_id"])
    link = UploadLink.objects.get(token=token_of(p))
    t = timezone.localtime(link.expires_at)
    assert done["done"] == (f"Saved {done['document']}. No files attached yet; the upload link works until "
                            f"{t.hour % 12 or 12}:{t:%M} {t:%p}.")
    upload_to(client, owner, p)
    assert active(Bill.objects.get(number=done["document"])).count() == 1


def test_failed_confirm_leaves_files_held(client, books, owner, app_url):
    p = write.preview(owner, "create_bill", {**BILL, "date": "2026-09-20"})
    upload_to(client, owner, p)
    close_period(2026, 9, owner)
    with pytest.raises(ToolError, match="September 2026 is closed"):
        run(ctx_for(owner), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    assert Attachment.objects.get().target_id is None


def test_attach_file_for_each_document(client, books, owner, clerk, app_url, attachment_docs):
    for doc in (attachment_docs.bill, attachment_docs.payment, attachment_docs.vendor_payment):
        r = run(ctx_for(owner), "attach_file", write.attach_file, document=doc.number)
        assert r["note"] == f"Open this link to attach files to {doc.number}."
        assert UploadLink.objects.get(token=token_of(r)).target == doc
        upload_to(client, owner, r)
        assert active(doc).count() == 1
    r = write.attach_file(owner, attachment_docs.bill.number)
    response = upload_to(client, clerk, r, data=b"\xff\xd8\xffother", name="other.jpg")
    assert "This upload link belongs to someone else." in response.content.decode()


def test_attach_file_refusals(books, owner, settings, attachment_docs):
    settings.DNB_APP_URL = "https://books.example.com"
    with pytest.raises(ToolError, match="^No bill or payment numbered BILL-99999.$"):
        run(ctx_for(owner), "attach_file", write.attach_file, document="BILL-99999")
    settings.DNB_APP_URL = ""
    with pytest.raises(ToolError, match="^Upload links are off. An admin can set DNB_APP_URL.$"):
        run(ctx_for(owner), "attach_file", write.attach_file, document=attachment_docs.bill.number)


def test_get_bill_lists_active_attachments(books, owner, attachment_docs):
    (a,) = add_files(owner, attachment_docs.bill, [SimpleUploadedFile("keep.pdf", PDF)]).added
    (b,) = add_files(owner, attachment_docs.bill, [SimpleUploadedFile("gone.jpg", b"\xff\xd8\xffx")]).added
    remove(owner, b, "Wrong bill")
    result = read.get_bill(owner, attachment_docs.bill.number)
    assert [f["name"] for f in result["attachments"]] == ["keep.pdf"]
    f = result["attachments"][0]
    assert f["kind"] == "PDF" and f["uploaded_by"] == owner.get_username() and f["size"].endswith("KB")


def test_held_files_never_reach_another_change(client, books, owner, app_url):
    first = write.preview(owner, "create_bill", BILL)
    upload_to(client, owner, first)
    second = write.preview(owner, "create_bill", {**BILL, "vendor_invoice_number": "R-2"})
    done = write.confirm(owner, second["confirmation_id"])
    assert "No files attached yet" in done["done"]
    assert Attachment.objects.get().target_id is None


def test_attach_file_is_a_listed_tool(db):
    import asyncio

    from mcpserver.server import build_mcp

    tools = asyncio.run(build_mcp().list_tools())
    names = {t.name for t in tools}
    assert "attach_file" in names
    bill_tool = next(t for t in tools if t.name == "create_bill")
    assert "upload link for the bill or receipt" in bill_tool.description
