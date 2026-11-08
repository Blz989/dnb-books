# Attachments

- **Date:** 2026-11-08
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md`. Attachments are new. Where they differ, this document wins.
- **Builds on:** Phase 1 (backups and the restore drill), Phase 3 (customer payments), Phase 4 (bills and vendor payments), and the MCP server (`docs/superpowers/specs/2026-11-04-mcp-server-design.md`).
- **Visual reference:** `docs/design/items-mockup.html` ("Ledger paper", with the Auto/Light/Dark toggle).

## 1. Goal

Keep the source document with the record it supports: the vendor's bill or receipt on a bill, the check image or remittance on a payment. Files can be added in the app, or from Claude through a one-time upload link.

### Success criteria (done when)

1. Every uploaded file is in the nightly off-site backup, and the monthly restore drill checks that the files restore and their checksums match. The company logo is covered too.
2. Bills, customer payments, and vendor payments each have an Attachments panel: add files, view them, and (admins only) remove one with a reason.
3. From Claude, `create_bill`, `receive_payment`, and `pay_bills` previews include an upload link. A file uploaded through it is attached to the document when the change is confirmed. A new `attach_file` tool gives a link for a document that already exists.
4. Files are served only to signed-in users, with the same role checks as the document's page.
5. Tests pass, and screenshots of every new or changed screen (section 8) have been sent to the owner.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Documents | Bills (stock, drop-ship, and expense-only bills) and payments (customer payments and vendor payments). |
| From Claude | A one-time upload link the person taps. Claude doesn't carry the file. (The MCP standard has no file upload yet; see section 12.) |
| Where the link opens | The main app site (`DNB_APP_URL`, for example `https://books.lopezhome.casa`), behind its Cloudflare Access policy and the DNB Books sign-in. |
| Backups | Attachments and the logo are backed up off-site with the database. |

## 3. What is stored

New Django app `attachments`.

| Model | Fields |
|---|---|
| `Attachment` | id, target (content type and object id: a `Bill`, `sales.Payment`, or `VendorPayment`; empty while held by a link), file (stored under `attachments/YYYY/MM/<sha256>.<ext>`), original_name, kind (PDF, JPEG, PNG), size_bytes, sha256, source (APP, CLAUDE_LINK), uploaded_by, uploaded_at, upload_link (nullable), removed_at, removed_by, removed_reason |
| `UploadLink` | token (random, 32 bytes, URL-safe), user, pending_change (nullable, `mcpserver.PendingChange`), target (nullable, for `attach_file`), created_at, expires_at |

- Files live in `data/media/` (`MEDIA_ROOT`), next to the database, on the existing `./data` volume. They are not stored in SQLite, so the database stays small and backups can send only new files.
- A file is named by its SHA-256, so it never changes after upload and is never overwritten.
- `Attachment` is registered with the audit log.

## 4. Rules

### 4.1 Files

- **Types:** PDF, JPEG, PNG. The type is checked from the file's first bytes (`%PDF`, `FF D8 FF`, `89 50 4E 47`), not from its name or the browser's label. Anything else: "This file type can't be attached. Use PDF, JPEG, or PNG."
- **Size:** up to 10 MB each: "This file is larger than 10 MB. Save a smaller copy and try again."
- **Count:** up to 10 files on one document: "This document already has 10 files attached."
- **Duplicates:** the same file (same SHA-256) on the same document is refused: "This file is already attached to {number}."

### 4.2 Who can do what

| Action | Who |
|---|---|
| View and download | Anyone who can open the document's page |
| Add | Anyone who can open the document's page, including on a voided document |
| Remove | Admin only, with a reason |

- **Removing** doesn't delete anything. The attachment is marked removed with who, when, and why. It disappears from the panel and shows under "Removed files" (admins only). The file stays on disk and in the backup.
- **Voiding or editing** a document leaves its attachments as they are. A bill edit (new revision) keeps them.
- Attachments never touch the ledger. Nothing posts.

### 4.3 Serving files

- `GET /attachments/<id>/` needs a signed-in user who can open the target document, otherwise 404.
- The response uses the stored kind as its content type (never the uploader's), `Content-Disposition: inline` with the original name, and `X-Content-Type-Options: nosniff`.
- Files are never served from a plain `/media/` URL.

## 5. Screens

### 5.1 Attachments panel

On the bill page, the customer payment page, and the vendor payment page, below the existing content:

- A table: file name (link to view), size, uploaded by, date, and "From Claude" when the source is a link. Admins see a Remove button on each row.
- **Add files:** a file input (`accept="application/pdf,image/jpeg,image/png"`, several at once). On a phone, this offers the camera and photo library. Each file is checked on its own, and the panel lists what was added and what was refused, with the message from 4.1.
- **Remove:** a confirm step with a required reason ("Enter a reason for removing this file.").
- **Empty state:** "No files attached."
- Admins see a "Removed files (n)" section with who removed each file, when, and the reason.

Files are added on the document's page after it is saved. The bill and payment forms don't change.

### 5.2 Upload link page

`GET /attach/<token>/` on the main app. It needs the DNB Books sign-in, and the signed-in user must be the user the link was made for.

- Shows what the link is for: "Attach files to the bill for Acme Supply (preview, not saved yet)" or "Attach files to BILL-00045".
- The same file input and checks as the panel.
- After upload: "Added receipt.jpg. Go back to Claude and confirm the change." (or, for an existing document, "Added receipt.jpg to BILL-00045.")
- Errors:

| Case | Message |
|---|---|
| Expired | "This upload link expired. Ask Claude for a new one." |
| Another user's link | "This upload link belongs to someone else." |
| Preview expired without being confirmed | "That preview expired, so there's nothing to attach to. Ask Claude for it again." |
| Unknown token | 404 |

## 6. From Claude

### 6.1 With a new document

1. `create_bill`, `receive_payment`, and `pay_bills` previews include `upload_link`, its expiry, and a line for Claude to pass on: "To attach the bill or receipt, open this link: {url}".
2. The person taps the link and uploads. Files are held on the link (no target yet).
3. `confirm_change` posts the document and, in the same transaction, sets each held file's target to it. The result says "Saved BILL-00045 with 1 file attached." With no file yet, it says "Saved BILL-00045. No files attached yet; the upload link works until {time}."
4. A link stays usable for 60 minutes from when it was made. Files uploaded after the confirm go straight onto the posted document.
5. When a preview expires without being confirmed, the scheduler deletes its held files and their disk files (daily cleanup). These were never attached to anything.

Previewing again makes a new link. Files held on an earlier link that was never confirmed aren't carried over.

### 6.2 With an existing document

New tool `attach_file(document)`, where `document` is a bill or payment number. It checks that the user can open that document and returns a link like 6.1, already targeted to it. It doesn't need a confirm, since adding a file changes no figures.

### 6.3 Reading

`get_bill` lists the bill's attachments (name, kind, size, date, uploaded by). Claude can't read file contents through the MCP in this version.

### 6.4 Log

`McpCall` records the link's creation. The upload itself is recorded in the audit log under the person's name, with source CLAUDE_LINK. Neither log stores file contents.

## 7. Backups

### 7.1 Nightly backup

After the database backup, in the same `BackupRun`:

1. List every file under `MEDIA_ROOT` (attachments and the logo) with its SHA-256.
2. Encrypt each file not already backed up with the existing backup key (Fernet) into `DNB_BACKUP_DIR/media/<sha256>.enc`.
3. Write a manifest, `dnb-YYYYMMDD-HHMMSS.media.json.enc`, listing every file's path, size, and SHA-256 at that moment. It shares the database backup's timestamp and retention.
4. With off-site backups on, upload the new `.enc` files and the manifest (rclone `copy` sends only what's missing).

Media files are never deleted from the backup, since a removed attachment is still kept. `BackupRun` records how many files were new and the total size.

A failure here fails the run with a plain message (for example "Backup saved the database but not the files: {reason}."), which the home screen already shows.

### 7.2 Restore drill

After the ledger check, the drill also:

- downloads the matching manifest and checks every listed file exists off-site
- decrypts every file added since the last passing drill, plus 5 others chosen at random, and checks their SHA-256

On failure: "Restore drill failed: {n} files in {manifest} are missing or don't match."

### 7.3 Restoring

`restore_backup` restores the media files listed in the chosen backup's manifest along with the database.

## 8. Screenshots for the owner

- Bill page Attachments panel: empty, with files (including one from Claude), the remove step, "Removed files" as an admin, and each error from 4.1.
- Customer payment and vendor payment pages with files.
- Upload link page: for a preview, for an existing bill, after upload, and each error from 5.2.
- The viewer showing a PDF and a photo.
- Home or Backups status after a backup that included files, and after a failed one.

## 9. Testing

| Area | Tests |
|---|---|
| Files | Types checked by content (a `.pdf` name on a PNG is stored as PNG; a renamed `.exe` is refused); 10 MB limit; 10-file limit; duplicate refused; stored name is the SHA-256 |
| Permissions | Viewing needs sign-in and access to the document (404 otherwise); Standard can add but not remove; removal needs a reason and keeps the file |
| Documents | Void and bill edit keep attachments; nothing posts to the ledger |
| Serving | Content type from the stored kind; `nosniff`; no plain `/media/` access |
| Upload link | Wrong user, expired link, unknown token, preview expired; held files attach on confirm in the same transaction; a file uploaded after confirm goes to the document; cleanup deletes held files of expired previews only |
| MCP | Previews of the three tools include the link; `attach_file` refuses an unknown document or one the user can't open; `get_bill` lists attachments; the end-to-end test previews a bill, uploads to the link, confirms, and finds the file on the bill |
| Backup | New files encrypted and uploaded once; the manifest lists every file; the logo is included; a failure fails the run with the message; files are never deleted from the backup |
| Drill | Passes on a good backup; fails when a file is missing off-site or a checksum doesn't match |
| Restore | `restore_backup` brings back the files in the manifest |

## 10. What the owner sets up

1. In `.env` on the NAS: `DNB_APP_URL=https://books.lopezhome.casa`. Without it, previews say "Upload links are off. An admin can set DNB_APP_URL." and nothing else changes.
2. In Cloudflare Access: anyone besides the owner who will use upload links must be allowed by the `books.lopezhome.casa` policy (today: owner only).
3. After deploying, run `backup_now` once and check the home screen. The first run sends every existing file, including the logo.

## 11. Build order

1. Backups and restore drill cover `MEDIA_ROOT` (works before attachments exist, protecting the logo).
2. `attachments` app: models, file checks, serving view.
3. Attachments panels on bills and payments.
4. Upload links: the page, holding files, attach on confirm, cleanup.
5. MCP: links in the three previews, `attach_file`, attachments in `get_bill`.
6. README: what attachments cover and the set-up in section 10.

## 12. Out of scope

- Attachments on invoices, sales orders, purchase orders, credit notes, vendor credits, journal entries, deposits, refunds, customers, vendors, and items.
- Pulling files from Gmail or Google Drive, and Claude uploading a file itself from its code sandbox.
- Reading or searching file contents (in the app or through the MCP), and OCR in the app.
- HEIC and other image formats, and resizing photos (no new image library).
- Importing Zoho attachments (also out of scope in the migration spec).
- Emailing attachments.

The MCP file-upload standard is still being worked out (the File Uploads Working Group; SEP-2356 and SEP-2631 were both closed without landing in the spec as of 2026-11-08). If one lands and Claude supports it, a later change can accept files directly. The storage, panels, and backups here stay the same.

## 13. Open questions for the owner

1. **Card charges.** Card and bank charges booked from the bank inbox with "New entry" are journal entries, not bills. Under this spec their receipts can't be attached. Add journal entries to the first version, or enter receipts that need to be kept as expense bills?
2. **iPhone photos.** Some iPhones save photos as HEIC. Safari usually converts to JPEG when uploading, but this should be checked on the owner's phone before deciding whether HEIC needs support.
