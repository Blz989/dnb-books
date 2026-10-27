# Phase 5a: Banking

- **Date:** 2026-10-27
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md` (section 7). Where this document and the parent differ, this document wins. Zoho migration, which the parent's Phase 5 also covered, is split out as Phase 5b with its own spec.
- **Builds on:** Phases 1 to 4 and the Docker deployment.
- **Visual references:** `docs/design/items-mockup.html` (screens), `docs/design/check-mockup.html` (printed check).

## 1. Goal

Connect DNB's bank, card, and loan accounts to the books:
- Bring in their transactions through Plaid, with OFX, QFX, or CSV files as the fallback.
- Match each one to what is already booked, or book it.
- Record transfers between DNB's own accounts.
- Print vendor and refund checks on the existing voucher stock.
- Reconcile each account to its statement.

### Success criteria (Phase 5a done when)

1. A scripted month passes, with every ledger check (inventory, AR, Undeposited, A/P) at 0.00 throughout:
   1. Import a statement file for checking and one for the card.
   2. Match a customer ACH payment, a deposit, and a printed vendor check.
   3. Book a bank fee by rule.
   4. Record a card payoff transfer (both sides matched once).
   5. Record a loan payment with principal and interest.
   6. Reconcile checking and the card to their statement balances at 0.00 difference.
2. The Plaid integration's tests pass against recorded responses, and the manual command `plaid_sandbox_check` completes against Plaid Sandbox when run with Sandbox keys: link, exchange, sync, and import into the inbox.
3. The check PDF matches `docs/design/check-mockup.html`, and the calibration print shows the alignment marks.
4. Screenshots of every new screen, the check PDF, the calibration print, and the reconciliation report have been sent to the owner.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Accounts | Business checking, other checking and savings, company credit card, line of credit or loan |
| Feeds | Plaid (owner has a developer account; Production approval needed for live banks), OFX, QFX, CSV fallback |
| Plaid sync | Polled by the scheduler service 4 times a day; no webhooks (the server is not public). Accounts are linked from a desktop browser, so no HTTPS redirect page is needed. Per Plaid's OAuth guide, desktop web opens the bank's login in a pop-up without a redirect URI. |
| Matching | Suggest, then a person confirms. Nothing is booked automatically. |
| Checks | Voucher stock, check on top, signed by hand, MICR pre-printed. Printed one at a time. Both stubs list the bills paid. |
| Check numbers | Spoiled and voided numbers are never reused on that bank account (tightens the Phase 4 rule) |
| Dependencies | `plaid-python` (Plaid's official library) and `ofxtools` (OFX/QFX parsing) |

### Out of scope for Phase 5a

- Plaid webhooks.
- Positive pay files.
- Printing deposit slips.
- Bill pay through the bank.
- Batch check printing.
- Printed signatures.
- MICR printing.
- Investment accounts.
- Multiple currencies.
- Zoho migration (Phase 5b).

## 3. Modules

New app `banking`.

| File | Responsibility |
|---|---|
| `banking/models.py` | BankAccount, PlaidItem, BankTransaction, Rule, Match, Transfer, Reconciliation, ReconciliationItem, CheckLayout, CheckNumber |
| `banking/crypto.py` | Encrypt and decrypt secrets with `DNB_FIELD_KEY` (Fernet) |
| `banking/plaid_client.py` | The only code that calls Plaid: link token, public token exchange, `/transactions/sync`, accounts |
| `banking/importers.py` | OFX/QFX (ofxtools) and CSV (column mapping) parsing into the same transaction shape |
| `banking/inbox.py` | Candidate matching and scoring, rules, confirm, new entry, exclude |
| `banking/transfers.py` | Transfers and loan payments |
| `banking/reconcile.py` | Reconciliation and locking |
| `banking/checks.py` | Check numbers, amount in words, check and calibration PDFs (WeasyPrint) |
| `banking/sync.py` | The scheduled sync job |

All ledger writes go through `ledger.posting`, and the inbox only links to documents that Phases 3 and 4 already posted.

## 4. Data model

| Model | Key fields |
|---|---|
| `BankAccount` | ledger_account (one-to-one; subtype BANK, CREDIT_CARD, OTHER_CURRENT_LIABILITY, or LONG_TERM_LIABILITY), kind (CHECKING, SAVINGS, CARD, LOAN), plaid_item (nullable), plaid_account_id, mask (last 4), csv_mapping (JSON: column names for date, description, amount or debit and credit, date format), next_check_number (checking only), is_active |
| `PlaidItem` | institution_name, access_token (encrypted), cursor (for `/transactions/sync`), status (OK, NEEDS_LOGIN, ERROR), last_sync_at, last_error |
| `BankTransaction` | bank_account, external_id (Plaid transaction id, or a hash of date, amount, and description for files), date, description, amount (signed from the account's point of view: money in positive), check_number (when the bank provides it), source (PLAID, FILE), status (NEW, MATCHED, BOOKED, EXCLUDED), match (nullable), created_at. Unique: (bank_account, external_id). |
| `Match` | bank_transaction, then one of: journal_entry (booked from the inbox or a transfer), sales payment, deposit, refund, purchasing vendor payment. Plus confirmed_by and confirmed_at. |
| `Rule` | position, is_active, pattern, pattern_type (CONTAINS, STARTS_WITH), bank_account (nullable = all accounts), account (the account to book to), memo |
| `Transfer` | date, from_account, to_account, amount, kind (TRANSFER, CARD_PAYOFF, LOAN_PAYMENT), principal, interest, interest_account, journal_entry, status |
| `Reconciliation` | bank_account, statement_date, statement_balance, status (IN_PROGRESS, FINISHED, UNDONE), finished_by, finished_at, report_pdf (file under `/data/media`) |
| `ReconciliationItem` | reconciliation, journal_line (the bank account's line), cleared (bool) |
| `CheckLayout` | bank_account (one-to-one), fields (JSON: per field x, y, font size, visible), offset_x, offset_y (inches, 2 dp) |
| `CheckNumber` | bank_account, number, status (PRINTED, SPOILED, VOIDED, HANDWRITTEN), vendor_payment or refund (nullable), printed_by, printed_at. Unique: (bank_account, number). |

## 5. Rules

### 5.1 Bringing transactions in

- **Plaid sync:**
  - `/transactions/sync` per PlaidItem with the saved cursor.
  - Added, modified, and removed transactions are applied. Only posted transactions (`pending = false`) become `BankTransaction` rows.
  - A removed transaction is deleted only if it is still NEW. Otherwise its row is marked and shown for review.
  - Errors set the item's status. `ITEM_LOGIN_REQUIRED` sets NEEDS_LOGIN and shows a home warning with a "Re-link" button (Link in update mode).
- **Schedule:** the scheduler service runs `sync_banks` at 06:00, 10:00, 14:00, and 18:00 server time, configurable with `DNB_BANK_SYNC_TIMES`. An admin can also click "Sync now".
- **Files:**
  - OFX and QFX are read by ofxtools.
  - CSV uses the account's saved mapping. The first upload for an account asks for the mapping and shows a 5-row preview.
  - Rows already present (same `external_id`) are skipped, and the result says how many.
- **Signs:** amounts are stored from the account's point of view. For card and loan accounts, a purchase or draw is negative.

### 5.2 Inbox matching

For each NEW transaction the inbox suggests one action, scored by these rules:

| Candidate | Rule | Score |
|---|---|---|
| Vendor payment by check | Bank check number = payment check number and amount matches | 100 |
| Any unmatched document posting to this ledger account | Same amount, date within ±10 days | 80 − days apart |
| Deposit | Deposit total = amount, date within ±5 days | 85 − days apart |
| Transfer side | An unmatched transfer between this and another account with the same amount within ±5 days | 75 − days apart |
| Rule | First active rule whose pattern matches the description (case-insensitive) | 50 |

"Unmatched document" means a non-void sales payment (ACH or wire to this bank), deposit, refund, vendor payment (`paid_from` = this account), or transfer whose bank-side journal line is not yet linked to a `Match`.

**Actions:**
- **Confirm** links the transaction to the suggestion. A rule suggestion books a new entry.
- **Change** picks another candidate or account.
- **New entry** posts a two-line journal entry (`banking.entry`): this ledger account against the chosen account, dated the bank date, with the description as memo.
- **Transfer** opens the transfer form pre-filled.
- **Exclude** needs a reason.
- **Bulk confirm** applies Confirm to the ticked rows.

Confirming never posts anything for an existing document. It only links.

### 5.3 Transfers and loan payments

- **Entries:** a transfer posts one entry (`banking.transfer`): Dr to_account, Cr from_account.
- **Loan payments:**
  - Cr checking (total), Dr the loan account (principal), Dr `interest_account` (interest).
  - Principal + interest must equal the total: "Principal and interest must add up to {total}."
- **Both sides linked:** the transfer is created from the first side's inbox row. The other account's matching row then scores 75 and matches the same transfer, so it is never booked twice.

### 5.4 Reconciliation

1. Start: choose the account, statement date, and statement ending balance. The screen lists every uncleared ledger line on that account dated on or before the statement date, plus earlier uncleared items.
2. Lines linked to a matched bank transaction dated on or before the statement date are pre-ticked.
3. Difference = statement balance − (last reconciled balance + Σ cleared amounts). "Finish" is enabled only at 0.00.
4. Finishing:
   - Marks the cleared lines reconciled.
   - Saves a PDF report: beginning balance, cleared deposits and payments, outstanding checks and payments, deposits in transit, and ending balances.
   - Locks the documents behind the cleared lines.
5. Locked documents refuse void and edit with: "{number} is part of the {Month YYYY} reconciliation for {account}. An admin must undo that reconciliation first." That covers sales payments, deposits, refunds, vendor payments, bills paid by them (edit only), transfers, and inbox entries.
6. Undo (admin, reason required) reopens the most recent finished reconciliation for that account only.

### 5.5 Checks

- **Who prints:** CHECK-method vendor payments and refunds whose check number is blank are "to print". Phase 4's check number becomes optional for CHECK payments: blank means to print, and a number means handwritten, recorded as `CheckNumber` HANDWRITTEN.
- **Printing:**
  - Print check shows the PDF for number = the account's `next_check_number` (editable).
  - Then it asks "Did check {n} print correctly?" Yes records PRINTED, sets the payment's check number, and advances the next number.
  - No marks {n} SPOILED and offers the next number.
- **Reprinting** a printed check (paper lost before mailing) marks the old number SPOILED and prints on a new number. Admin only.
- **Numbers:** a number with any `CheckNumber` row is never used again on that account: "Check {n} was already used on {account} ({status})." Voiding a check payment sets its number VOIDED.
- **Amount in words:** "One thousand seventy-five and 00/100", filled with asterisks to the line end. The amount figure is prefixed with "**". Tests cover 0.07 ("Zero and 07/100"), 1,000,000.00, and 19.99.
- **Layout:**
  - Default voucher-top positions match `docs/design/check-mockup.html`: date, payee, amount, amount in words, address block (window-envelope position), memo (vendor invoice numbers or the refund number), stub 1, stub 2.
  - Stubs list each bill paid: bill number, vendor invoice number, bill date, bill amount, paid. For refunds: refund number, source document, amount.
  - Stubs over 12 bills show the first 11 and "and {n} more (see statement)".
- **Calibration print:** each field's box outline and a ruler grid at 0.25 in, the current offsets printed. Offset nudges are in 0.01 in steps, −0.50 to 0.50.
- **Rendering:** WeasyPrint, Letter size, `@page { margin: 0 }`, positions in inches. The PDF must be printed at actual size, and the print screen says so: "Print at 100% (actual size), not 'fit to page'."

## 6. Security

- `PLAID_CLIENT_ID`, `PLAID_SECRET`, and `PLAID_ENV` (`sandbox` or `production`) come from `.env`. When missing, the Link button is hidden and file import still works.
- `DNB_FIELD_KEY` (a Fernet key, generated by `generate_field_key`) encrypts `PlaidItem.access_token`. Without it the app starts, but syncing shows "Plaid tokens can't be read. Check DNB_FIELD_KEY or re-link the accounts."
- Access tokens and the Plaid secret never appear in templates, logs, the audit log (excluded field), or error messages.

## 7. Screens

All screens use the Ledger paper layout. The sidebar gains a **Banking** group: Bank accounts, Inbox, Transfers, Reconcile, Checks to print.

| Screen | URL | Notes |
|---|---|---|
| Bank accounts | `/banking/` | Per account: kind, mask, linked status, last sync, ledger balance, last reconciled date, inbox count. "Link with Plaid", "Import file", "Sync now" (admin). |
| Link | `/banking/link/` | Plaid Link (Plaid's JavaScript from their CDN, the only external script). Then map each Plaid account to a ledger account and kind. Admin. |
| Import file | `/banking/<id>/import/` | Upload, CSV mapping with preview, result counts |
| Inbox | `/banking/inbox/?account=` | Rows with suggestion and score, actions, bulk confirm, filters (account, status) |
| Rules | `/banking/rules/` | List with order, add, edit, toggle |
| Transfers | `/banking/transfers/`, `.../new/` | Kind, accounts, amount, principal and interest for loans |
| Reconcile | `/banking/reconcile/<account_id>/`, `.../<id>/`, `.../<id>/report.pdf`, `.../<id>/undo/` | Running difference shown at the top |
| Checks | `/banking/checks/` (to print), `/banking/checks/<payment>/print/`, `.../confirm/`, `/banking/layout/<account_id>/`, `/banking/layout/<account_id>/calibrate.pdf` | |
| Home | | Cards: inbox items waiting, last sync (warning if over 24 hours or any account NEEDS_LOGIN), accounts not reconciled in 40 days |

## 8. Permissions

| Action | Standard | Admin |
|---|---|---|
| View banking; inbox confirm, change, new entry, exclude; rules; transfers; print checks (first print) | ✓ | ✓ |
| Link and unlink accounts, Sync now, Plaid settings; check layouts and calibration; finish and undo reconciliations; reprint (spoil) a printed check | | ✓ |

## 9. Testing

| Layer | Proves |
|---|---|
| Plaid client | Against recorded responses: link token, exchange, sync pages with cursor, modified and removed, pending skipped, login-required status. Live Sandbox is checked by the `plaid_sandbox_check` management command, run by hand, not by pytest (no skipped tests). |
| Importers | OFX and QFX samples; CSV mapping with debit and credit columns; duplicates skipped |
| Matching | Each candidate type and score; check number first; transfers from both sides booked once; rules order |
| Transfers | Entries for each kind; loan principal + interest check |
| Reconciliation | Difference math; finish only at 0.00; locks block voids and edits in Phases 3 and 4; undo; report contents |
| Checks | Amount in words cases; numbering, spoiled and voided never reused; handwritten numbers; stub overflow; PDF renders; screenshots compared with the mockup |
| Security | Tokens encrypted at rest; absent from templates, logs, and the audit log |
| Acceptance | Section 1 scenario with all ledger checks at 0.00 |

`finance:reconciliation` reviews 5.2 and 5.4. `finance:journal-entry` reviews 5.3.
