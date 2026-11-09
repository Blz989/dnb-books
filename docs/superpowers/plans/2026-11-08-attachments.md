# Attachments: implementation plan

Spec: `docs/superpowers/specs/2026-11-08-attachments-design.md` (binding).
PR title: "Attachments on bills and payments, upload links from Claude, and file backups"
Branch: a new branch from `main` named after the work (for example `claude/attachments`).

## Tech Stack
Django 5.2, SQLite, `cryptography` (Fernet, existing), rclone (existing, off-site), the `mcp` SDK (existing). No new dependencies. File types are checked from the first bytes in plain Python; no image library.

## Global Constraints
- Attachments never touch the ledger. Nothing in this plan calls `post_entry`, `post_draft`, or `void_entry`, and no posting service changes. `confirm_change` keeps calling the same operations; attaching held files happens in the same transaction, after the operation returns its number.
- Files are written once, named by SHA-256, and never overwritten or deleted, except held files of expired previews (Task 4 cleanup), which were never attached.
- Files are served only by `attachments.views.serve`. Do not add a `/media/` URL or `static()` media serving.
- The served content type comes from `Attachment.kind`, never from the upload's `content_type`.
- User-visible strings are exactly those in the spec and this plan. American English. Plain, short UI text.
- Screens follow the existing look (`core/static/core/app.css` classes: `card`, `data`, `banner`, `tag`, `num`, `section`). Both light and dark themes must work; CSS variables only, no hard-coded colors.
- Tests in `tests/`, pytest style, file names as given. TDD: write the test, see it fail, implement, see it pass.
- Never skip, xfail, or delete a failing test. Do not weaken model guards or triggers.
- One commit per task minimum with the commit message given. Tick this plan's checkboxes for the task in the same commit.
- Tests that write files set `settings.MEDIA_ROOT` and `settings.DNB_BACKUP_DIR` to `tmp_path` subfolders. No test writes under `data/`.
- Screenshots (owner's standing rule): for each task that changes a screen, run the app against a throwaway demo DB (`DNB_DB_PATH=<scratch>/demo.sqlite3 uv run python manage.py migrate && ... seed_demo`, with `MEDIA_ROOT` under the same scratch folder), add sample data through the normal services, start `uv run python manage.py serve` on a free port, log in as demo / demo-pass-123, and capture full-page PNGs at deviceScaleFactor 2 with the pre-installed Playwright (`require('playwright')` in Node, `chromium.launch()`), in light and dark (`localStorage dnb-theme`), plus a 390px-wide shot where the task says so. Sample files: a one-page PDF and a JPEG photo of a printed bill made in the scratch folder. Never use `data/`. Do not commit screenshots.

## Review Focus
- No path serves a file without the sign-in check; content type and `nosniff` on every file response.
- A confirm that fails leaves held files unattached; a confirm that succeeds attaches every held file in the same transaction.
- An upload link works only for its own user, only until it expires, and never for an expired, unconfirmed preview.
- Backups: each media file is encrypted and uploaded once; nothing under `media/` is ever deleted from the backup; the drill fails when a file is missing or altered.
- `confirm_change` results and ledger effects for every existing tool are unchanged (existing `tests/test_mcp.py` passes untouched).

---

## Task 1: Back up media files

Files: `backup/service.py`, `backup/remote.py`, `backup/models.py` (+ migration), `backup/management/commands/restore_backup.py`, `tests/fake_rclone.py`, `tests/test_backup_media.py` (new).

Interfaces:
```python
# backup/models.py  (BackupRun additions)
media_manifest = models.CharField(max_length=200, blank=True)   # dnb-YYYYMMDD-HHMMSS.media.json.enc
media_files = models.PositiveIntegerField(null=True, blank=True) # files listed in the manifest
media_new = models.PositiveIntegerField(null=True, blank=True)   # files encrypted this run
media_bytes = models.BigIntegerField(null=True, blank=True)      # total size of listed files

# backup/service.py
MEDIA_DIRNAME = "media"            # DNB_BACKUP_DIR/media/<sha256>.enc, remote <remote>/media/<sha256>.enc

@dataclass(frozen=True)
class MediaFile:
    path: str      # relative to MEDIA_ROOT, forward slashes
    size: int
    sha256: str

def list_media() -> list[MediaFile]:
    """Every regular file under MEDIA_ROOT, sorted by path. Missing MEDIA_ROOT -> []."""

def backup_media(stamp: str, key: str) -> tuple[str, int, int, int]:
    """Encrypt files not yet in DNB_BACKUP_DIR/media, write the encrypted manifest for `stamp`
    ("YYYYMMDD-HHMMSS"), and with off-site on, upload new files and the manifest.
    Returns (manifest filename, files listed, files new, total bytes). Raises BackupError."""

def manifest_filename(stamp: str) -> str     # f"dnb-{stamp}.media.json.enc"
def read_manifest(path: Path, key: str) -> list[MediaFile]
def restore_media(manifest: list[MediaFile], source_dir: Path, dest_root: Path, key: str) -> int

# backup/remote.py
def upload_dir(local_dir: Path, remote_subdir: str) -> None   # rclone copy <dir> <remote>/<subdir> (sends only missing)
def list_dir(remote_subdir: str) -> set[str]                   # rclone lsf <remote>/<subdir>
```
- Manifest JSON: `{"files": [{"path", "size", "sha256"}, ...]}`, encrypted with the backup key.
- `run_backup` calls `backup_media` after the database file is written and before retention. Retention (`apply_retention`) also removes `.media.json.enc` manifests on the same days it removes database files (same timestamp rule), and never touches `media/`.
- A failure in `backup_media` fails the run: `"Backup saved the database but not the files: {reason}."`
- `restore_drill`, after the ledger check passes: get the manifest matching `run.filename` (download when off-site is on), check every `sha256` is in `list_dir("media")` (or the local folder when off-site is off), then decrypt and hash the files whose `sha256` was not in the previous passing drill's manifest plus 5 others at random (`random.Random(run.pk)` so tests are repeatable). Failure: `"Restore drill failed: {n} files in {manifest} are missing or don't match."` A run with no manifest (made before this task) skips the media check and says so in the pass message: `"Restore drill passed: {file} restores and its ledger matches. It was made before file backups, so files weren't checked."`
- `restore_backup` gains `--media-to <folder>` (must not exist). With it, the manifest matching the backup file is read and every file is restored under that folder at its `path`. The printed steps add: `"Move {folder} to {MEDIA_ROOT}."`
- `tests/fake_rclone.py`: `copy` accepts a directory source (copies files not already present); add `lsf <remote-dir>` (prints file names).

Tests (`tests/test_backup_media.py`):
- [x] `list_media` finds the logo and nested files with correct size and SHA-256; empty or missing `MEDIA_ROOT` gives `[]`.
- [x] First backup encrypts every file; a second backup with one new file encrypts only that one (`media_new == 1`), and existing `.enc` files are unchanged (mtime and bytes).
- [x] The manifest decrypts to every file; `BackupRun` media fields are set.
- [x] With the fake remote, new files and the manifest are uploaded; nothing under remote `media/` is deleted by retention.
- [x] A write failure in the media step fails the run with the message above.
- [x] Drill passes on a good backup; fails with the message when a remote file is deleted, and when one is replaced with other bytes; a pre-feature run passes with the "weren't checked" message.
- [x] `restore_backup --media-to` restores the files byte for byte; refuses an existing folder.
- [x] Existing `tests/test_backup.py`, `tests/test_restore.py`, `tests/test_docker_backup.py` pass untouched.
- [x] Commit: `Back up uploaded files with the database and check them in the restore drill`

## Task 2: Attachments app, file checks, and serving

Files: `attachments/__init__.py`, `attachments/apps.py`, `attachments/models.py` (+ migration), `attachments/files.py`, `attachments/service.py`, `attachments/views.py`, `attachments/urls.py`, `dnb/settings.py` (INSTALLED_APPS), `dnb/urls.py`, `tests/test_attachments.py` (new).

Interfaces:
```python
# attachments/models.py
class Kind(models.TextChoices):
    PDF = "PDF", "PDF"
    JPEG = "JPEG", "JPEG"
    PNG = "PNG", "PNG"

class Source(models.TextChoices):
    APP = "APP", "App"
    CLAUDE_LINK = "CLAUDE_LINK", "From Claude"

class Attachment(models.Model):
    target_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.PROTECT)
    target_id = models.PositiveBigIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_type", "target_id")
    file = models.FileField(upload_to=...)          # attachments/YYYY/MM/<sha256>.<ext>, set by service
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=4, choices=Kind.choices)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.APP)
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # upload_link (FK to UploadLink, null, SET_NULL, related_name="attachments") is added in Task 4.
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    removed_reason = models.CharField(max_length=200, blank=True)
# auditlog.register(Attachment)

# attachments/files.py
MAX_BYTES = 10 * 1024 * 1024
MAX_PER_DOCUMENT = 10
EXT = {Kind.PDF: "pdf", Kind.JPEG: "jpg", Kind.PNG: "png"}
def sniff(head: bytes) -> Kind | None       # b"%PDF", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"

# attachments/service.py
TARGETS = (Bill, sales.Payment, VendorPayment)   # the only allowed targets

class AttachmentError(Exception):
    """Plain-language message shown as is."""

@dataclass
class AddResult:
    added: list[Attachment]
    refused: list[tuple[str, str]]   # (file name, message)

def add_files(user, target, files, *, source=Source.APP, link=None) -> AddResult:
    """Check and store each file on its own (spec 4.1). target None only with a link (held file)."""
def active(target) -> QuerySet[Attachment]      # removed_at is null, ordered by uploaded_at, id
def removed(target) -> QuerySet[Attachment]
def remove(user, attachment, reason: str) -> None   # admin only (require_admin); reason required
def document_url(target) -> str                     # bill-detail / sales payment-detail / purchasing payment-detail
def label(target) -> str                            # "BILL-00045", the payment number
def can_view(user, attachment) -> bool              # signed in and active; held files: only the uploader
```
- Messages (exact): `"This file type can't be attached. Use PDF, JPEG, or PNG."`, `"This file is larger than 10 MB. Save a smaller copy and try again."`, `"This document already has 10 files attached."`, `"This file is already attached to {number}."`, `"Enter a reason for removing this file."`.
- The count and duplicate checks run per file against active attachments on the target, including those added earlier in the same call. For held files (no target) they run against the link's held files.
- Reading: read the first 8 bytes for `sniff`, hash while streaming to a temp file in `MEDIA_ROOT/tmp/`, then `os.replace` to the final path. If the final path already exists (same file on another document), reuse it and don't rewrite it.
- `serve(request, pk)`: `login_required`; 404 unless `can_view`; `FileResponse` with `content_type` from kind (`application/pdf`, `image/jpeg`, `image/png`), `Content-Disposition: inline; filename="<original_name>"` (Django's `FileResponse(as_attachment=False, filename=...)`), and `X-Content-Type-Options: nosniff`. Removed attachments are still served to admins (for the Removed files list), 404 for others.
- Route: `path("attachments/", include("attachments.urls"))` with `<int:pk>/` named `attachments:serve`.

Tests (`tests/test_attachments.py`):
- [x] `sniff` for each type; a PNG named `bill.pdf` is stored as PNG; a renamed `.exe` and a text file are refused with the message.
- [x] 10 MB plus one byte refused; the eleventh file refused; the same file twice refused with the number in the message; the stored name is the SHA-256 and two documents sharing a file share one disk file.
- [x] `remove` as Standard raises the role error; without a reason raises the message; as Admin sets removed fields and leaves the disk file.
- [x] `serve`: signed out redirects to login; content type from kind even when uploaded with a false label; `nosniff` header; a removed file is 404 for Standard and served for Admin; `/media/...` is 404.
- [x] Attaching to each of the three targets works; any other model raises.
- [x] No `JournalEntry` or `JournalLine` rows are created by any attachment action.
- [x] Commit: `Attachments app: file checks, storage, and signed-in serving`

## Task 3: Attachments panels on bills and payments

Files: `attachments/templates/attachments/_panel.html` (new), `attachments/views.py` (add, remove), `attachments/urls.py`, `attachments/templatetags/attachments.py` (new, `{% attachments_panel target %}`), `purchasing/templates/purchasing/bill_detail.html`, `purchasing/templates/purchasing/payment_detail.html`, `sales/templates/sales/payment_detail.html` (check the template name in `sales/views/receipts.py`), `core/static/core/app.css` (only if needed), `tests/test_attachment_views.py` (new).

Interfaces:
```python
# attachments/urls.py
path("add/<str:kind>/<str:number>/", views.add, name="add")              # kind in {"bill", "payment", "vendor-payment"}
path("<int:pk>/remove/", views.remove_view, name="remove")              # GET confirm page, POST with reason

# attachments/templatetags/attachments.py
@register.inclusion_tag("attachments/_panel.html", takes_context=True)
def attachments_panel(context, target): ...   # active, removed (admin only), is_admin, add URL
```
- Panel per spec 5.1: heading "Attachments"; table columns File, Size, Uploaded by, Date, and a "From Claude" tag on CLAUDE_LINK rows; a Remove link per row for admins; empty state "No files attached."; the file input with `accept="application/pdf,image/jpeg,image/png" multiple` and button "Add files"; for admins with removed files, a collapsible "Removed files (n)" section listing name, removed by, date, reason.
- `add` (POST, `login_required`): looks up the document by number (404 if none), calls `add_files`, then redirects back to the document's page with one success message per added file ("Added {name}.") and one error message per refused file ("{name}: {message}"). An empty submission: "Choose a file to add."
- `remove_view`: admin only; GET shows a small confirm page with a required reason field (button "Remove file"); POST calls `remove` and redirects to the document with "Removed {name}."
- Sizes print as "240 KB" or "3.1 MB".

Tests (`tests/test_attachment_views.py`):
- [x] Each of the three pages shows the panel with the empty state, then the file after adding; a refused file shows its message and the good files in the same submission are still added.
- [x] Standard sees no Remove links and gets 403 on the remove URL; Admin removes with a reason and the file moves to "Removed files"; a missing reason shows the message.
- [x] A voided bill and a voided payment still accept files; a bill edit (new revision) keeps its attachments.
- [x] Unknown document number gives 404.
- [x] Screenshots: bill page with no files, with three files (one tagged From Claude), the remove confirm page, Removed files as an admin, each refusal message; customer payment and vendor payment pages with files; a PDF and a photo opened from the panel; the bill page at 390px wide.
- [x] Commit: `Attachments panel on bills, customer payments, and vendor payments`

## Task 4: Upload links

Files: `attachments/models.py` (+ migration: `UploadLink`, the `Attachment.upload_link` FK), `attachments/links.py` (new), `attachments/views.py` (link page), `attachments/templates/attachments/link.html` (new), `attachments/urls.py`, `attachments/management/commands/clean_uploads.py` (new), `backup/management/commands/run_scheduler.py`, `dnb/settings.py` (`DNB_APP_URL`), `.env.example`, `tests/test_upload_links.py` (new).

Interfaces:
```python
# attachments/models.py
class UploadLink(models.Model):
    token = models.CharField(max_length=64, unique=True)       # secrets.token_urlsafe(32)
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    pending_change = models.ForeignKey("mcpserver.PendingChange", null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name="upload_links")
    target_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.PROTECT)
    target_id = models.PositiveBigIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_type", "target_id")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

# attachments/links.py
LINK_LIFETIME = timedelta(minutes=60)
def enabled() -> bool                                      # bool(settings.DNB_APP_URL)
def make_link(user, *, pending_change=None, target=None) -> UploadLink   # exactly one of the two
def url(link) -> str                                       # f"{DNB_APP_URL}/attach/{token}/"
def resolve_target(link):
    """The document files go to now: link.target; else the document confirmed from link.pending_change
    (PendingChange.document looked up by number for its tool); else None (held)."""
def attach_held(pending_change, document) -> int
    """Set the target of every held file on this change's links. Called inside confirm's transaction."""
def clean_expired(now=None) -> int
    """Delete held files (no target) whose link's pending change expired unconfirmed, and their disk files
    when no other Attachment uses the same file. Never touches attached or removed files."""
```
- `DNB_APP_URL = os.environ.get("DNB_APP_URL", "").rstrip("/")`; `.env.example` gets `DNB_APP_URL=` with a comment: "Main app address for Claude upload links, like https://books.lopezhome.casa".
- Link page `GET/POST /attach/<token>/` (route at the site root: `path("attach/<str:token>/", ...)` in `dnb/urls.py`), `login_required`. Unknown token: 404. Checks in order, each with the spec 5.2 message: belongs to someone else; expired; preview expired unconfirmed (pending change past `expires_at` with `used_at` null). Heading per spec: "Attach files to the bill for {vendor} (preview, not saved yet)", "Attach files to the payment from {customer} (preview, not saved yet)", "Attach files to the payment to {vendor} (preview, not saved yet)", or "Attach files to {number}". Read the vendor or customer name from the pending change's stored input. After upload: "Added {name}. Go back to Claude and confirm the change." for a held file, or "Added {name} to {number}." when the target is known. Uploads use source CLAUDE_LINK and `link=`.
- `clean_uploads` command runs `clean_expired` and prints "Removed {n} files from expired previews."; the scheduler runs it daily at `DNB_BACKUP_TIME` minus 15 minutes (compute from `parse_time`; add to `upcoming_jobs` and the start-up line).

Tests (`tests/test_upload_links.py`):
- [x] Another user's link, an expired link, and an unknown token give the message or 404; the link page needs sign-in.
- [x] Before confirm, uploads are held (no target) and the page says to go back to Claude; after the pending change has a document, uploads go straight to it.
- [x] A preview that expired unconfirmed shows its message and accepts nothing.
- [x] `attach_held` sets the target for every held file of that change and none of another change's.
- [x] `clean_expired` deletes held files of expired, unconfirmed previews and their disk files; keeps held files of a change still in time, every attached file, and a disk file shared with an attached copy.
- [x] Scheduler lists `clean_uploads` once a day at the computed time.
- [x] Screenshots: link page for a bill preview, for an existing bill, after an upload, and each error page; link page at 390px wide.
- [x] Commit: `Upload links for adding files from Claude`

## Task 5: Upload links in the MCP

Files: `mcpserver/write.py`, `mcpserver/tools.py`, `mcpserver/read.py`, `tests/test_mcp_attachments.py` (new).

Interfaces:
```python
# mcpserver/write.py
LINK_TOOLS = {"create_bill", "receive_payment", "pay_bills"}
DOCUMENT_MODEL = {"create_bill": Bill, "receive_payment": sales.Payment, "pay_bills": VendorPayment}
# preview(): for LINK_TOOLS, after PendingChange is created, add to the result:
#   with links enabled: "upload_link": url, "upload_link_expires": ISO time,
#     "attach_note": "To attach the bill or receipt, open this link: {url}"
#   with links off: "attach_note": "Upload links are off. An admin can set DNB_APP_URL."
# confirm(): inside the existing atomic block, after the operation returns `number`, for LINK_TOOLS look up
#   the document by number and call attachments.links.attach_held(change, document). Result "done" becomes
#   "Saved {number} with {n} file attached." / "... with {n} files attached." when n > 0, or, when the change
#   had a link and n == 0, "Saved {number}. No files attached yet; the upload link works until {HH:MM}."
#   Changes with no link (other tools) keep "Saved {number}." exactly.

def attach_file(user, document: str) -> dict:
    """document: a bill number, a customer payment number, or a vendor payment number (tried in that order).
    Returns {"upload_link", "upload_link_expires", "note": "Open this link to attach files to {number}."}.
    Unknown: Refused("No bill or payment numbered {document}."). Links off: Refused("Upload links are off.
    An admin can set DNB_APP_URL.")"""

# mcpserver/read.py get_bill(): add "attachments": [{"name", "kind", "size", "uploaded_at", "uploaded_by"}]
#   from attachments.service.active(bill).
```
- `tools.py`: new `attach_file(ctx, document: str)` with the PREVIEW annotation (it changes no figures), docstring: "Get a one-time link for attaching a PDF or photo to an existing bill or payment. The person opens the link to upload; nothing else changes."
- Update the `create_bill`, `receive_payment`, and `pay_bills` docstrings to end with: "The preview includes an upload link for the bill or receipt; pass it on to the person."
- `McpCall` logs `attach_file` like any tool. No file bytes or names go into `McpCall.arguments` beyond the document number.

Tests (`tests/test_mcp_attachments.py`), using the `books`, `owner`, `clerk`, and `ctx_for` helpers from `tests/test_mcp.py` (move shared fixtures to `tests/conftest.py` if needed without changing them):
- [ ] The three previews include the link and note; other previews don't; with `DNB_APP_URL` empty the note says links are off.
- [ ] End to end in-process: preview a bill, upload a PDF to the link as the same user through the test client, confirm, and find the file on the bill with source CLAUDE_LINK; "done" says 1 file attached.
- [ ] Confirm with no upload gives the "No files attached yet" message; an upload after confirm lands on the bill.
- [ ] A confirm that fails (month closed in between) leaves held files without a target.
- [ ] `attach_file` for a bill, a customer payment, and a vendor payment; unknown number refused with the message; the link works only for the caller.
- [ ] `get_bill` lists active attachments and not removed ones.
- [ ] Existing `tests/test_mcp.py` passes untouched.
- [ ] Commit: `Upload links in Claude previews and an attach_file tool`

## Task 6: README and final checks

Files: `README.md`, `docs/superpowers/specs/2026-11-08-attachments-design.md` (only the status line if it changes), `CLAUDE.md` (Done list).

- [ ] README: under Purchasing and Sales tables, rows for "Keep the bill or receipt with a bill" and "Keep a check image or remittance with a payment" (where: open the bill or payment, Attachments, Add files; removal is admin only with a reason). Under "Use DNB Books from Claude": the upload link flow and `DNB_APP_URL`. Under backups: files are backed up with the database and checked by the restore drill; `restore_backup --media-to`. Set-up steps from spec section 10. Update the Status line.
- [ ] CLAUDE.md: add "Attachments" to Done once the PR is ready.
- [ ] Run `uv run pytest -q`; paste the output in the PR body.
- [ ] Open the PR with the title above, listing each task with its status, the test output, deviations, screenshot sets sent, and the manual checks: set `DNB_APP_URL`, add other users to the `books.lopezhome.casa` Access policy if they will use links, run `backup_now` once and check the home screen, and upload a photo from the owner's iPhone to check whether it arrives as JPEG.
- [ ] Commit: `Attachments: README and status`
