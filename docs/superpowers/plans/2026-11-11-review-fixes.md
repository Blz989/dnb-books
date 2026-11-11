# Review fixes: implementation plan

Source: the code review of 2026-11-11 (five bugs and four smaller items). No separate spec; the design decisions are in "Decisions for the owner" below and need a yes before Task 1 starts. Executed inline, task by task, test first.

Status: approved 2026-11-11 and built (PR 48). Card CSV signs in Task 7 is waiting for the owner.

## What is being fixed

| # | Bug | Severity | Task |
|---|-----|----------|------|
| 1 | A document's journal entry or stock movement can be voided directly, leaving the document open, out of step with the ledger, and impossible to void or edit | High | 1, 2 |
| 2 | Bank inbox shows a server error for a closed month or an inventory account | Medium | 3 |
| 3 | Claude `save_item` is refused for every item with stock history; `is_active` as text is misread | Medium | 4 |
| 4 | Backups leave out uploaded files (reconciliation PDFs, handling statements, logo) | Medium | 6 |
| 5 | Purchasing documents and bank rows can be changed or deleted in the Django admin | Medium | 5 |
| 6 to 9 | Smaller items: Claude invoice edit drops drop-ship, stale Claude instructions, card CSV signs, duplicate pairing by exact date | Low | 4, 7 |

## Mitigation until the fixes are merged

The system is not yet the system of record, so nothing here needs a data repair today. Until Task 1 and Task 5 are merged:

- Void a document only from its own screen (the invoice, bill, payment, deposit, credit, refund, or transfer page). Do not use Books, Journal, Void on an entry whose source is a document, and do not use Void on an item page for a movement that came from an invoice, bill, or vendor credit.
- In Claude, do not use `void_journal_entry` or `void_stock_movement` on anything that belongs to a document.
- Do not edit or delete bills, vendor payments, vendor credits, purchase orders, or bank transactions in `/admin/`.
- Before cutover, copy `data/media/` somewhere safe by hand whenever a reconciliation is finished or a handling statement is uploaded.

## Decisions for the owner

1. **Bug 1, how to block.** Recommended: block in the posting service with a message that names the document, and show that message on the entry and movement pages in place of the Void button. This is how handling bills are already protected, and it covers the screens and Claude with one rule. Alternative: only hide the buttons, which leaves Claude and hand-made requests open. Not recommended.
2. **Bug 5, the admin.** Recommended: keep the purchasing documents, bank transactions, and transfers visible in `/admin/` but read-only. Bank accounts and rules stay editable there. Alternative: remove them from `/admin/` entirely.
3. **Bug 4, backup format.** Recommended: each backup becomes one encrypted archive holding the database snapshot and the `media` folder. The file name changes from `dnb-…sqlite3.enc` to `dnb-….tar.enc`. Old backups stay restorable. The restore steps in the README change by one step.
4. **Card CSV signs (Task 7).** Only needed if one of your cards exports purchases as positive numbers. Say which bank and card files you plan to import, or skip this part.

## Global constraints

- No change to the model guards or SQLite triggers. All ledger writes stay in `ledger/posting.py`; all stock writes stay in `items/stock.py`.
- No new dependencies. Money is `Decimal`.
- Error text is plain language and matches the strings in each task exactly. Service errors pass through unchanged.
- Never skip, xfail, or delete a failing test. Existing tests that void a document's entry directly are changed to void the document, and each such change is listed in the PR.
- Screens that change get screenshots sent to the owner as each task finishes (demo database only), and are listed in the PR.
- American English. Comments only where the rule is not obvious.

## Review focus

- Every document service (create, edit, void) still works after the lock, including edits by a Standard user and voids of documents imported from Zoho.
- No path, screen, Claude, or admin, can void a document's entry or movement except the document's own service.
- No inbox action can return a server error for a posting rule the user can fix.
- A backup made before this change still passes the restore drill.

## Tasks

### Task 1: Lock document entries and movements

Files: `ledger/locks.py`, `ledger/posting.py`, `items/stock.py`, `sales/checks.py` or a new `sales/locks.py`, `purchasing/checks.py` or a new `purchasing/locks.py`, `banking/apps.py`, `sales/apps.py`, `purchasing/apps.py`, the posting services that void (`sales/posting/invoices.py`, `receipts.py`, `deposits.py`, `purchasing/posting/bills.py`, `common.py`, `credits.py`, `banking/transfers.py`, `handling/billing.py`), `items/views.py`, `items/templates/items/item_detail.html`, `adjustment_list.html`, `tests/test_document_locks.py`.

Interfaces:

- `ledger.posting.void_entry(..., from_document: bool = False)`. When False, an entry whose `source_type` belongs to a document is refused. Document services pass True. No view and no Claude tool passes it.
- `items.stock.void_movement(..., from_document: bool = False)`. When False, a movement whose `source_type` is not `items.movement` or `items.import` is refused.
- `ledger.locks.document_label(entry) -> str | None`, built from a registry (`register_document_source(source_type, label)`) so ledger does not import the other apps. Sources to register, confirmed by grep before coding: `sales.invoice`, `sales.payment`, `sales.credit_note`, `sales.refund`, `sales.deposit`, `purchasing.bill`, `purchasing.payment`, `purchasing.vendor_credit`, `banking.transfer`. Not locked: manual entries (blank source), `banking.entry` (voiding it from the journal is the intended way to send a bank row back to the inbox), and `items.*` (already has its own rule).

Messages:

- Entry: `This entry belongs to invoice INV-000123. Void the invoice instead.` (the label and number change per document: invoice, customer payment, credit note, refund, deposit, bill, vendor payment, vendor credit, transfer).
- Movement: `This movement belongs to invoice INV-000123. Void or edit the invoice instead.`

Steps:

- [x] Tests first, failing: for each of the nine sources, `void_entry` on the document's entry raises `EntryStateError` with the message, and the document's own void still works. `void_movement` on an invoice's, a bill's, and a vendor return's movement raises `ItemStateError` with the message. `edit_invoice` and `edit_bill` by a Standard user still work. A handling run's bill still gives the handling message.
- [x] Add the registry, the `from_document` argument, and the checks. Pass `from_document=True` from every document service.
- [x] Journal detail already shows `void_blocked` in place of the button; make it use the new message. Item detail and the adjustment list show no Void link for a document's movement.
- [x] Fix any existing test that voided a document's entry or movement directly; list them.
- [x] `uv run pytest -q` green.
- [x] Screenshots: journal entry of an invoice (banner, no button), item page with an invoice movement (no Void link), a manual entry (button still there).
- [x] Commit: `Lock document entries and movements to their documents`.

### Task 2: Detect a document out of step with its entry

Files: `sales/checks.py`, `purchasing/checks.py`, `tests/test_document_locks.py`.

A safety net for data that got into the bad state before Task 1 (for example in a dry run), reported at month close and in the Claude `close_checklist`.

- [x] Tests first: an open invoice whose journal entry is VOID produces `INV-000123 is open but its journal entry was voided. Ask for a repair.`; an open bill whose stock movement is voided produces the same line with the bill number; clean data produces nothing.
- [x] Add the check to the existing close checks for sales and purchasing (open documents only, one query per document type).
- [x] Commit: `Close check: documents whose entry or movement was voided`.

No repair command is included. If the check ever reports a line, stop and ask; the repair depends on the case.

### Task 3: Bank inbox errors

Files: `banking/views/inbox.py`, `banking/inbox.py`, `tests/test_inbox_views.py`.

- [x] Tests first, failing with a 500 today: Change, book to an account, on a row in a closed month shows `January 2026 is closed. Choose a date in an open month or ask an admin to reopen it.` on the page; Confirm of a rule suggestion in a closed month shows it as a message; Pay bills and Receive payment in a closed month show it in the form's errors; booking to an inactive account shows the ledger's message; bulk confirm with one row in a closed month confirms the others and reports `Confirmed 2 of 3 selected.`
- [x] `_run` and `_catchup` catch `PostingError` as well as `InboxError`. `inbox.bulk_confirm` skips a row on `PostingError` (each row already runs in its own nested transaction).
- [x] The Change screen's account list leaves out inventory accounts, and a hand-made post of one shows `Line 2: inventory accounts can only change through stock movements.` instead of a server error.
- [x] Screenshots: the Change screen and the Pay bills screen showing the closed-month message.
- [x] Commit: `Bank inbox: show posting errors instead of a server error`.

### Task 4: Claude tools

Files: `items/forms.py`, `mcpserver/write.py`, `mcpserver/write_void.py`, `mcpserver/server.py`, `tests/test_mcp_writes.py`, `tests/test_mcp_voids.py`.

- [x] Tests first: `save_item` changing the sale price of an item with stock history previews and saves; changing its inventory account is still refused with `The inventory account can't change after an item has stock history.`; `is_active` given as `false`, `"false"`, `"no"`, or `0` deactivates an item with nothing on hand, and the preview and the saved record agree.
- [x] `ItemForm.clean` compares the posted inventory account as text on both sides. `_item` reads `is_active` from `form.cleaned_data`, as `_customer` does.
- [x] Tests first: `edit_invoice` on an invoice with a drop-ship line, with no `drop_ship_vendor` given for that line, keeps the line drop-ship with its vendor; giving a vendor changes it; giving `drop_ship_vendor: ""` on a new line makes a stock line. Same for `edit_sales_order`.
- [x] A line matched to an existing line by SKU keeps that line's fulfillment and vendor unless the call sets them.
- [x] `edit_invoice` and `edit_bill` on a document with no journal entry (a zero total) return a preview instead of the generic error.
- [x] After Task 1, `void_journal_entry` and `void_stock_movement` on a document's entry or movement return the Task 1 messages. Add one test each.
- [x] Replace the last line of `INSTRUCTIONS` in `mcpserver/server.py` with: `Voids and edits need an admin and the document number typed back in confirm_change. Void a document with its own void tool, never through its journal entry or stock movement. Month close is done in the app.`
- [x] Commit per group: `MCP: save_item works on items with stock history`, `MCP: edits keep drop-ship lines`, `MCP: instructions match the tools`.

### Task 5: Read-only documents in the admin

Files: `purchasing/admin.py`, `banking/admin.py`, a shared `core/admin.py` holding `ReadOnlyAdmin` (moved from `migration/admin.py`, a mechanical change), `tests/test_admin_readonly.py`.

- [x] Tests first: a superuser gets 403 on the add, change-post, and delete pages for `Bill`, `VendorPayment`, `VendorCredit`, `PurchaseOrder`, `BankTransaction`, and `Transfer`, and can still open the list and the detail page. `PurchasingSettings`, `Threshold1099`, `BankAccount`, and `Rule` stay editable.
- [x] Register the six models with `ReadOnlyAdmin`.
- [x] Screenshot: a bill in `/admin/` with no Save or Delete button.
- [x] Commit: `Admin: purchasing documents and bank rows are read-only`.

### Task 6: Back up uploaded files

Files: `backup/service.py`, `backup/remote.py` (file name pattern only), the restore command or README steps, `README.md` ("Backups" and "Restore"), `tests/test_backup.py`.

Interfaces: a backup is `dnb-YYYYMMDD-HHMMSS.tar.enc`, a Fernet-encrypted tar with `snapshot.sqlite3` and `media/…`. `FILENAME_RE` accepts both the old and new names so retention and the drill keep working across the change.

- [x] Tests first: a backup made with a logo and a reconciliation PDF in `media` restores both files byte for byte; an empty or missing `media` folder still backs up; retention treats old and new names alike; the restore drill passes on a new archive and on an old `.sqlite3.enc` file; a drill on an archive with a missing `snapshot.sqlite3` fails with `Restore drill failed: the backup has no database in it.`
- [x] Build the archive in the temp folder, encrypt, write as today. The drill extracts the snapshot (plain `tarfile` with member-name checks, no extraction outside the temp folder) and compares the ledger fingerprint as today.
- [x] README: restore steps for the new archive (decrypt, untar, put `snapshot.sqlite3` and `media/` back), and a note that old backups are a bare database.
- [x] Finance check with `finance:reconciliation`: after a restore into a scratch folder, the reconciliation report PDF opens and its totals match the reconciliation on screen. Record the result in the commit body.
- [ ] Manual check for the owner on the server, listed in the PR: run `backup_now`, then `restore_drill`, and confirm the home screen shows the drill passed.
- [x] Commit: `Backups include uploaded files`.

### Task 7: Bank file imports (only the parts the owner approves)

Files: `banking/importers.py`, `banking/forms.py`, the import mapping template, `tests/test_importers.py`.

- [ ] Card CSV signs (decision 4): add `flip_sign` to the saved CSV mapping, a tick box labeled `Purchases show as positive numbers in this file` on the mapping screen, off by default. Tests: a card file with positive purchases imports them as money out when ticked; existing mappings are unchanged.
- [x] Duplicate pairing: a row from the other source with the same amount within one day either side counts as a possible duplicate, still paired one to one. Tests: Plaid on the 10th and a file row on the 11th pair up; two real charges of the same amount on the same day from one source do not.
- [ ] Screenshot: the mapping screen with the tick box.
- [ ] Commit: `Bank imports: sign option for card files, duplicates within a day`.

### Task 8: Wrap up

- [x] `README.md`: status line, the backup section from Task 6, and one line under "Use DNB Books from Claude" on voiding documents.
- [x] Final `uv run pytest -q`; paste the output in the PR.
- [x] Open a PR into `main` titled `Review fixes: document locks, inbox errors, Claude item saves, admin, backups`, with each task's status, deviations, screenshots sent, and the owner's manual check from Task 6.

## Order and size

Tasks 1 and 2 first (the only bug that can leave the books stuck), then 3 and 4 (small), then 5 (small), then 6 (the largest, changes the backup format), then 7. Each task is independent after Task 1, so the PR can ship after any of them if needed.

## Out of scope

- A repair tool for documents already out of step with the ledger (Task 2 only detects them).
- Speeding up the 11 minute test suite.
- Cookie and port hardening for the Cloudflare tunnel.
- Backing up `.env` or the rclone sign-in; those stay in the password manager as the README says.
