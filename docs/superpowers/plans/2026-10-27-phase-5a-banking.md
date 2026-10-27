# Phase 5a: Banking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bank, card, and loan accounts connected through Plaid or statement files; an inbox that suggests matches a person confirms; transfers and loan payments; reconciliation that locks what it clears; and check printing on the owner's voucher stock with a layout designer and calibration print.

**Architecture:**
- **New `banking` app.**
- **Reconciliation locks and inbox clean-up live in `ledger`:** two small registries in `ledger/locks.py`. `void_entry` asks lock checks before voiding and calls void hooks after, so every Phase 3 and 4 void path is covered without those apps importing `banking`.
- **Check numbers** are one registry (`banking/checknumbers.py`) that purchasing and sales consult through local imports.
- **Plaid** is reached only through `banking/plaid_client.py`, which tests replace with a recorded fake.

**Tech Stack:** Previous phases, plus `plaid-python` and `ofxtools` (both approved in the spec). Plaid Link's JavaScript is loaded from Plaid's CDN on the link page only.

**Spec:** `docs/superpowers/specs/2026-10-27-phase-5a-banking-design.md`. Visual references: `docs/design/items-mockup.html`, `docs/design/check-mockup.html`.

## Global Constraints

- **`CLAUDE.md` applies in full,** including the screenshot rule and a filled-in PR body. No skipped tests. Plaid Sandbox is exercised only by the manual `plaid_sandbox_check` command.
- **Money:** `Decimal`, 2 dp. A bank transaction's amount is signed from the account's point of view: money in or a balance decrease on a liability is positive. For CARD and LOAN accounts a purchase or draw is negative.
- **Journal `source_type`:** `banking.entry` (inbox new entries) and `banking.transfer`. Source id `BT-<bank transaction pk>` or `TR-<transfer pk>`.
- **Secrets:** Plaid secrets come only from `.env` (`PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`). Access tokens are encrypted with `DNB_FIELD_KEY`. Neither appears in templates, logs, exceptions shown to users, or the audit log (`auditlog.register(PlaidItem, exclude_fields=["access_token"])`).
- **Nothing books automatically** from the inbox. Confirming an existing document only creates a `Match`.
- **Exact strings:** user-facing messages exactly as in the spec or this plan.

## Review Focus

1. **CSV files in real bank formats:** `$1,234.56`, `(45.00)` for negatives, separate debit and credit columns, `MM/DD/YYYY` dates, a trailing "Total" row, a byte-order mark. All parse, or a bad row gives "Row {n}: {problem}." and nothing is saved. Pinned in Task 3.
2. **The same period arriving from both Plaid and a file:** a file row on the same account with the same date and amount as an existing Plaid row (or the other way round) is saved as EXCLUDED with reason "Possible duplicate of a {source} transaction on {date}." and can be restored. Pinned in Task 3.
3. **Printer-confirm double-click, or refresh after printing:** one `CheckNumber` per check, and the next number advances once. Pinned in Task 13.
4. **Credit card statement balance typed as the amount owed** (positive `1,250.00`): treated as owed, and the reconcile difference is correct. Pinned in Task 7.
5. **A matched (not yet reconciled) document is voided:** the bank transaction goes back to NEW with its match removed, so it reappears in the inbox. Pinned in Task 1 (hook) and Task 5 (behavior).

---

## File Structure

```
ledger/locks.py                    register_lock_check, lock_message, register_void_hook
ledger/posting.py                  void_entry consults locks and calls hooks
purchasing/posting/payments.py, bills.py   check number optional; registry; lock check on bill edit
sales/posting/receipts.py          refund check number via registry
banking/
  models.py  crypto.py  checknumbers.py  words.py  importers.py  plaid_client.py  sync.py
  inbox.py  transfers.py  reconcile.py  checks.py  forms.py  urls.py  apps.py  demo.py
  views/ (accounts.py, inbox.py, transfers.py, reconcile.py, checks.py)
  templates/banking/*  static/banking/link.js
  management/commands/ generate_field_key.py, sync_banks.py, plaid_sandbox_check.py
backup/management/commands/run_scheduler.py   + bank sync times
tests/ test_locks.py, test_banking_models.py, test_importers.py, test_plaid_sync.py, test_inbox.py,
       test_transfers.py, test_reconcile.py, test_check_numbers.py, test_check_pdf.py,
       test_bank_account_views.py, test_inbox_views.py, test_reconcile_views.py, test_check_views.py,
       test_phase5a_acceptance.py, fixtures/plaid/*.json, fixtures/bank/*.ofx, *.qfx, *.csv
```

---

### Task 1: Ledger locks and void hooks

**Files:**
- Create: `ledger/locks.py`
- Modify: `ledger/posting.py`
- Test: `tests/test_locks.py`

**Interfaces:**
- Produces:
  - `register_lock_check(fn: Callable[[JournalEntry], str | None])`. `lock_message(entry) -> str | None` returns the first non-None message.
  - `register_void_hook(fn: Callable[[JournalEntry], None])`.
  - `void_entry` (including `system=True`):
    - Before anything else it calls `lock_message(entry)` and raises `EntryStateError(message)` if set.
    - After the reversal is posted it calls each void hook with the original entry.

- [ ] **Step 1: Failing tests:**
  - A registered check blocks `void_entry` with its message, and nothing is posted.
  - A hook is called once with the voided entry.
  - With no registrations, behavior is unchanged (the full Phase 1 to 4 suite passes).
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: ledger lock checks and void hooks`

---

### Task 2: Banking models, encryption, settings

**Files:**
- Create: `banking/__init__.py`, `apps.py`, `models.py`, `crypto.py`, `admin.py`, migrations, `management/commands/generate_field_key.py`
- Modify: `dnb/settings.py` (`banking`; `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV` default `sandbox`, `DNB_FIELD_KEY`, `DNB_BANK_SYNC_TIMES` default `06:00,10:00,14:00,18:00`), `.env.example`, `pyproject.toml` and `uv.lock` (`plaid-python`, `ofxtools`), `core/management/commands/setup_roles.py`, `tests/conftest.py`
- Test: `tests/test_banking_models.py`

**Interfaces:**
- Produces:
  - All spec section 4 models and choices: `AccountKind` (CHECKING, SAVINGS, CARD, LOAN), `TxnStatus` (NEW, MATCHED, BOOKED, EXCLUDED), `TxnSource` (PLAID, FILE), `ItemStatus` (OK, NEEDS_LOGIN, ERROR), `TransferKind` (TRANSFER, CARD_PAYOFF, LOAN_PAYMENT), `ReconStatus` (IN_PROGRESS, FINISHED, UNDONE), `CheckStatus` (PRINTED, SPOILED, VOIDED, HANDWRITTEN).
  - `BankTransaction` adds `exclude_reason` (text).
  - `Match` has one nullable FK per target and a check constraint requiring exactly one.
  - `BankAccount.clean()` checks that the ledger subtype fits the kind: CHECKING and SAVINGS → BANK; CARD → CREDIT_CARD; LOAN → OTHER_CURRENT_LIABILITY or LONG_TERM_LIABILITY.
  - `banking/crypto.py`: `encrypt(text: str) -> str`, `decrypt(token: str) -> str`. Missing or bad key raises `FieldKeyError("Plaid tokens can't be read. Check DNB_FIELD_KEY or re-link the accounts.")`.
  - `PlaidItem.access_token` is stored encrypted. Properties `get_access_token()` / `set_access_token(value)`. Audit-log registration excludes it.
  - `generate_field_key` prints a Fernet key and the instruction: "Add this to .env as DNB_FIELD_KEY and store a copy in your password manager."
- Roles: Standard gets add, change, and view on BankTransaction, Match, Rule, Transfer, CheckNumber, and view on the rest. `ADMIN_APPS` gains `banking`.
- Fixtures: `checking` (BankAccount on `bank`, CHECKING, next_check_number 2051), `card_account` (CARD on `card`), `loan` (2500 LONG_TERM_LIABILITY "Equipment loan", LOAN), `interest_expense` (6800 EXPENSE), `field_key` (sets `DNB_FIELD_KEY`).

- [ ] **Step 1: Failing tests:**
  - Encrypt and decrypt round trip; the raw DB column does not contain the token.
  - A missing key gives the message.
  - A subtype/kind mismatch is rejected.
  - Unique `(bank_account, external_id)`.
  - The `Match` exactly-one constraint.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: banking models and encrypted Plaid tokens`

---

### Task 3: Statement file import

**Files:**
- Create: `banking/importers.py`
- Create: `tests/fixtures/bank/checking.ofx`, `card.qfx`, `checking.csv`, `debit_credit.csv`, `messy.csv`
- Test: `tests/test_importers.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ParsedTxn(external_id: str, date: date, description: str, amount: Decimal, check_number: str = "")`.
  - `parse_ofx(data: bytes) -> list[ParsedTxn]`: ofxtools. `external_id` = FITID, `check_number` = CHECKNUM.
  - `@dataclass CsvMapping(date: str, description: str, amount: str | None = None, debit: str | None = None, credit: str | None = None, date_format: str = "%m/%d/%Y", check_number: str | None = None)`.
  - `parse_csv(text: str, mapping: CsvMapping) -> tuple[list[ParsedTxn], list[RowError]]`. Amounts accept `$`, thousands commas, parentheses as negative, and a leading `-`. Rows whose date cell doesn't parse and whose description starts with "Total" are skipped silently. `external_id` = sha256 of `f"{date}|{amount}|{description}|{occurrence}"`, where occurrence counts identical rows in the file, so two identical same-day charges both import.
  - `import_transactions(bank_account, txns: Sequence[ParsedTxn], *, source: TxnSource, user) -> ImportResult(added: int, skipped: int, possible_duplicates: int)`:
    - Same `external_id` → skipped.
    - Same account, date, and amount as an existing row from the *other* source → saved EXCLUDED with `exclude_reason` "Possible duplicate of a {Plaid|file} transaction on {date}." (Review Focus 2).
    - Row errors mean nothing is saved.
    - For CARD and LOAN accounts, files are read as exported: purchases and draws come out negative.

- [ ] **Step 1: Failing tests:**
  - Each fixture parses to the expected rows (amounts and dates spelled out in the tests).
  - `messy.csv` (BOM, `$`, parentheses, Total row) parses (Review Focus 1).
  - A bad amount on row 7 gives "Row 7: amount must be a number." and nothing is saved.
  - A second import of the same file reports added 0, skipped N.
  - Plaid/file overlap is excluded with the reason.
  - Two identical charges on the same day both import.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: OFX, QFX, and CSV statement import`

---

### Task 4: Plaid client and scheduled sync

**Files:**
- Create: `banking/plaid_client.py`, `banking/sync.py`, `banking/management/commands/sync_banks.py`, `banking/management/commands/plaid_sandbox_check.py`, `tests/fixtures/plaid/*.json`
- Modify: `backup/management/commands/run_scheduler.py` (adds `sync_banks` at each `DNB_BANK_SYNC_TIMES` entry)
- Test: `tests/test_plaid_sync.py`

**Interfaces:**
- Produces (`plaid_client.py`, the only code importing `plaid`):
  - `class PlaidClient` with methods:
    - `create_link_token(user_id: str, access_token: str | None = None) -> str` (update mode when given a token)
    - `exchange_public_token(public_token: str) -> tuple[str, str]` (access_token, item_id)
    - `get_accounts(access_token) -> list[PlaidAccount(account_id, name, mask, type, subtype)]`
    - `transactions_sync(access_token, cursor: str | None) -> SyncPage(added, modified, removed: list[str], next_cursor, has_more)`, where `added` and `modified` are lists of `PlaidTxn(transaction_id, account_id, date, name, amount, pending, check_number)`
  - Plaid amounts are positive for money *out*. The client converts them to the Global Constraints sign (`amount = -plaid_amount`).
  - `PlaidError(code: str, message: str)` wraps API errors.
  - `get_client() -> PlaidClient` builds the client from settings. Tests monkeypatch it with `FakePlaidClient` built from the JSON fixtures.
- Produces (`sync.py`):
  - `sync_item(item: PlaidItem) -> SyncResult(added, modified, removed, skipped_pending)`. It loops pages until `has_more` is false, then saves the cursor.
    - Pending transactions are skipped.
    - Modified transactions update NEW rows only.
    - Removed transactions delete NEW rows. Others are flagged with `exclude_reason` "Removed by the bank." and kept for review.
    - An `ITEM_LOGIN_REQUIRED` code sets NEEDS_LOGIN. Other errors set ERROR with `last_error` = the Plaid message.
  - `sync_all() -> None`.
  - Command `sync_banks` runs `sync_all()`.
  - Command `plaid_sandbox_check` runs against `PLAID_ENV=sandbox` only (it refuses otherwise): creates a sandbox public token for institution `ins_109508`, exchanges it, syncs, and prints counts. Manual use only.

- [ ] **Step 1: Failing tests (with `FakePlaidClient`):**
  - Two pages are applied and the cursor saved.
  - Pending is skipped.
  - Modified and removed are handled.
  - The sign is flipped (Plaid 25.00 is −25.00 on checking).
  - Login required sets NEEDS_LOGIN.
  - The access token never appears in `str(PlaidError)` or the log output (caplog).
  - The scheduler includes `sync_banks` at 06:00.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: Plaid sync`

---

### Task 5: Inbox matching, rules, confirm, new entry, exclude

**Files:**
- Create: `banking/inbox.py`
- Modify: `banking/apps.py` (registers the void hook)
- Test: `tests/test_inbox.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Candidate(kind: str, target: object, score: int, label: str)`. Kinds: `vendor_check`, `document`, `deposit`, `transfer`, `rule`. Scores per spec 5.2.
  - `candidates(txn: BankTransaction) -> list[Candidate]` returns them sorted by score, highest first. Ties break by closer date, then lower document number.
  - `suggest(txn) -> Candidate | None`: the first candidate.
  - `confirm(txn, candidate: Candidate, *, user) -> Match`:
    - Document, deposit, vendor check, or transfer: a link only, status MATCHED.
    - Rule: calls `new_entry`.
    - A target already matched: "That item is already matched to another bank transaction."
  - `new_entry(txn, *, account: Account, user, memo: str = "") -> Match`. It posts a `banking.entry`: Dr/Cr this account vs `account` by sign, dated `txn.date`. Status BOOKED.
  - `exclude(txn, *, reason, user)`, `restore(txn, *, user)`.
  - `bulk_confirm(txns, *, user) -> int` confirms each transaction's `suggest()` and skips those without one.
  - Void hook `release_matches(entry)`: a `Match` whose target's journal entry is the voided one is deleted, and its transaction goes back to NEW (Review Focus 5).
  - "Already matched" means a `Match` exists for the target. Candidates exclude matched targets and void documents.

- [ ] **Step 1: Failing tests:**
  - A vendor check payment with the check number scores 100 and wins over an amount-only document.
  - A deposit total matches.
  - An ACH customer payment matches within 10 days, but not at 11.
  - A rule suggestion books a new entry on confirm, and the trial balance moves.
  - Bulk confirm.
  - Exclude and restore.
  - Voiding a matched payment returns its transaction to NEW.
  - A matched target is not offered again.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement.** Review against `finance:reconciliation` and record findings in the commit body.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: bank feed inbox`

---

### Task 6: Transfers and loan payments

**Files:**
- Create: `banking/transfers.py`
- Modify: `banking/inbox.py` (transfer candidates)
- Test: `tests/test_transfers.py`

**Interfaces:**
- Produces:
  - `create_transfer(*, date, from_account: BankAccount, to_account: BankAccount, amount, kind: TransferKind, user, principal=None, interest=None, interest_account=None, from_txn: BankTransaction | None = None) -> Transfer`:
    - TRANSFER and CARD_PAYOFF post Dr to.ledger_account, Cr from.ledger_account.
    - LOAN_PAYMENT posts Dr loan (principal), Dr interest_account (interest), Cr from (amount).
    - When `from_txn` is given, it is matched to the new transfer.
  - `void_transfer(transfer, *, user, reason, date=None)` (admin).
- Messages:
  - "Principal and interest must add up to {amount}."
  - "Choose two different accounts."
  - Card payoff needs `to_account.kind == CARD`: "A card payoff goes to a credit card account."
- Transfer candidate (spec 5.2): a transfer whose other account is this account, the same absolute amount, ±5 days, and no match yet on this account's side, scored 75 − days apart.

- [ ] **Step 1: Failing tests:**
  - The entries for each kind.
  - The loan sum message.
  - A card payoff seen from the checking side creates the transfer. The card account's row then suggests that transfer, and confirming books nothing new: the trial balance is unchanged by the second confirm.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement.** Review against `finance:journal-entry` and record findings.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: transfers and loan payments`

---

### Task 7: Reconciliation and locks

**Files:**
- Create: `banking/reconcile.py`, `banking/templates/banking/reconcile_report_pdf.html`
- Modify: `banking/apps.py` (registers the lock check), `purchasing/posting/bills.py` (edit refused when an applied payment's entry is locked)
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Produces:
  - `start(bank_account, *, statement_date, statement_balance: Decimal, user) -> Reconciliation`:
    - One in progress per account: "Finish or cancel the reconciliation already in progress."
    - It creates `ReconciliationItem`s for every not-yet-reconciled posted journal line on the account's ledger account dated ≤ `statement_date`.
    - It pre-ticks lines whose entry is the target of a `Match` with a bank transaction dated ≤ `statement_date`.
  - `toggle(item, cleared: bool)`.
  - `difference(rec) -> Decimal` = statement balance − (opening + Σ cleared). Signs:
    - BANK accounts use debit − credit.
    - CARD and LOAN accounts use credit − debit, so the amount owed is positive (Review Focus 4).
    - Opening = the last FINISHED reconciliation's statement balance, or 0.
  - `finish(rec, *, user)`: admin; only at 0.00 ("The difference must be 0.00 to finish."); renders the PDF report with WeasyPrint to `report_pdf`.
  - `undo(rec, *, user, reason)`: admin; latest finished only ("Only the most recent reconciliation can be undone.").
  - `cancel(rec, *, user)` deletes an in-progress one.
  - `reconciled_lines()`: journal line ids in FINISHED reconciliations with cleared items.
  - Lock check `entry_lock(entry) -> str | None`: any line of the entry is reconciled → "{number} is part of the {Month YYYY} reconciliation for {account}. An admin must undo that reconciliation first." Here `{number}` is the entry's `source_id`, or `display_number` when it has none.
  - `purchasing.edit_bill`: before editing, any applied payment whose entry `lock_message` returns a message raises `PurchasingError` with that message.

- [ ] **Step 1: Failing tests:**
  - The difference math for checking and for the card (positive amount owed).
  - Finish only at 0.00.
  - The opening balance carries to the next month.
  - Locks block a void of a sales payment, a deposit, a vendor payment, a transfer, and an inbox entry, plus `edit_bill` with a locked payment, each with the message.
  - Undo of the latest unlocks; undo of an older one is refused.
  - The report PDF renders and lists outstanding checks.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: reconciliation with locks`

---

### Task 8: Check numbers and amount in words

**Files:**
- Create: `banking/checknumbers.py`, `banking/words.py`
- Modify: `purchasing/posting/payments.py` (check number optional; registry check; void marks VOIDED), `sales/posting/receipts.py` (refund CHECK: `reference` blank = to print; registry check; void marks VOIDED), their Phase 4 and 3 tests where they assert "Enter the check number for {vendor}." (that rule is replaced by spec 5.5; update those tests to the new rule, do not delete them)
- Test: `tests/test_check_numbers.py`

**Interfaces:**
- Produces (`checknumbers.py`, no module-level imports of `sales` or `purchasing`):
  - `assert_free(bank_account_ledger: Account, number: str) -> None` raises "Check {n} was already used on {account} ({status})." when a `CheckNumber` exists for it. Numbers are normalized: strip, drop leading zeros.
  - `record(bank_account_ledger, number, status: CheckStatus, *, vendor_payment=None, refund=None, user) -> CheckNumber`.
  - `mark_voided(payment_or_refund) -> None`.
  - `next_number(bank_account: BankAccount) -> int` returns the account's `next_check_number`, skipping used numbers.
  - A handwritten number given at payment time is recorded as HANDWRITTEN.
- Produces (`words.py`): `amount_in_words(amount: Decimal) -> str`, for example "One thousand seventy-five and 00/100", "Zero and 07/100", "One million and 00/100", "Nineteen and 99/100". Pure function, no library.
- `is_to_print(payment) -> bool`: method CHECK and a blank check number.

- [ ] **Step 1: Failing tests:**
  - The words cases above plus 1,234,567.89.
  - A handwritten number is recorded and can't be reused.
  - A voided payment's number stays blocked: a new payment with that number is refused with "(voided)".
  - A blank CHECK number is allowed and is to print.
  - The updated Phase 4 test passes under the new rule.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: check number registry and amount in words`

---

### Task 9: Check PDF, layout, calibration

**Files:**
- Create: `banking/checks.py`, `banking/templates/banking/check_pdf.html`, `banking/templates/banking/calibration_pdf.html`
- Test: `tests/test_check_pdf.py`

**Interfaces:**
- Produces:
  - `DEFAULT_LAYOUT: dict[str, dict]` with keys `date`, `payee`, `amount`, `amount_words`, `address`, `memo`, `stub1`, `stub2`. Each value is `{x, y, size, visible}` in inches and points, set to the positions in `docs/design/check-mockup.html`:

    | Field | x | y | size |
    |---|---|---|---|
    | date | 6.15 | 0.86 | 10.5 |
    | payee | 1.80 | 1.29 | 10.5 |
    | amount | 6.78 | 1.29 | 10.5 |
    | amount_words | 0.50 | 1.71 | 10.5 |
    | address | 0.95 | 2.10 | 9.5 |
    | memo | 0.95 | 2.76 | 9 |
    | stub1 | 0.45 | 3.75 | 9 |
    | stub2 | 0.45 | 7.50 | 9 |

  - `layout_for(bank_account) -> dict` returns the saved `CheckLayout` merged over the defaults, plus offsets.
  - `check_context(payment_or_refund, number: str) -> dict`:
    - Payee name and address come from the vendor (or customer for refunds).
    - The amount is `"**" + "{:,.2f}"`, and the words are padded with `*` to 70 characters.
    - Memo: the vendor invoice numbers joined with ", ", truncated at 40 characters, or the refund number.
    - Stub rows: bill number, vendor invoice #, bill date, bill amount, paid. Over 12 rows: the first 11 plus "and {n} more (see statement)".
  - `check_pdf(payment_or_refund, number: str) -> bytes`. Positions are absolute in inches. The offsets are added to every field.
  - `calibration_pdf(bank_account) -> bytes`: field outlines, a 0.25 in grid, labels, and the current offsets printed.
  - `save_layout(bank_account, *, fields: dict, offset_x: Decimal, offset_y: Decimal, user)`: admin; offsets between −0.50 and 0.50 ("Offsets must be between -0.50 and 0.50 inches.").

- [ ] **Step 1: Failing tests:**
  - The context for a two-bill payment matches the mockup values.
  - Stub overflow at 13 bills.
  - The PDF starts with `%PDF`.
  - An offset of 0.10 shifts every field's `left` in the rendered HTML by 0.10 in.
  - The offset range is enforced.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots:** rasterize the check PDF and the calibration PDF with `pdftoppm`, compare the check against `docs/design/check-mockup.html`, and send both.
- [ ] **Step 6: Commit** `feat: check and calibration PDFs`

---

### Task 10: Bank account, link, and import screens

**Files:**
- Create: `banking/views/accounts.py`, `banking/forms.py`, `banking/urls.py`, `banking/static/banking/link.js`, templates (`accounts.html`, `account_form.html`, `link.html`, `link_map.html`, `import.html`, `import_map.html`, `import_result.html`), `banking/demo.py`
- Modify: `dnb/urls.py` (`banking/`), `core/templates/core/base.html` (Banking sidebar group: Bank accounts, Inbox, Transfers, Reconcile, Checks to print), `core/management/commands/seed_demo.py`
- Test: `tests/test_bank_account_views.py`

**Interfaces:**
- Produces the spec section 7 URLs for accounts, link, and import.
- The link page:
  - Loads `https://cdn.plaid.com/link/v2/stable/link-initialize.js`.
  - `link.js` posts the public token to `/banking/link/exchange/` (CSRF-protected).
  - The map page lists each Plaid account with a ledger account select and a kind select.
  - It's hidden, with "Plaid isn't set up. Add PLAID_CLIENT_ID and PLAID_SECRET to .env." when settings are missing.
  - Admin only.
- Re-link (`/banking/link/<item_id>/update/`) uses update mode.
- "Sync now" (admin) runs `sync_item` for that item and shows the counts.
- Import: upload (OFX/QFX/CSV, max 5 MB), then CSV mapping with a 5-row preview (saved to `csv_mapping`), then the result counts.

- [ ] **Step 1: Failing tests:**
  - The account list shows balances and the inbox count.
  - Link is admin only and hidden without Plaid settings.
  - Exchange stores an encrypted token (with the fake client).
  - Import flows for OFX and for CSV with the mapping saved and reused.
  - A file too large shows "The file is too large (5 MB limit)."
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (demo: checking, savings, card, and loan accounts; imported statement rows)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of bank accounts, the link page (Plaid not set up message), the import mapping preview, and the import result. Light and dark, sent.
- [ ] **Step 6: Commit** `feat: bank account, link, and import screens`

---

### Task 11: Inbox, rules, and transfer screens

**Files:**
- Create: `banking/views/inbox.py`, `banking/views/transfers.py`, templates
- Modify: `banking/forms.py`, `banking/urls.py`, `banking/demo.py`
- Test: `tests/test_inbox_views.py`

**Interfaces:**
- **Inbox:**
  - Rows show date, description, amount, the suggestion label and score, and actions (Confirm, Change, New entry, Transfer, Exclude).
  - Bulk confirm for ticked rows.
  - Filters: account and status. The default is NEW.
  - "Change" lists all candidates plus an account picker for a new entry.
- **Rules:** list (ordered; move up and down), add, edit, toggle.
- **Transfers:** list and new form. Kind, accounts, amount, principal, interest, and interest account show for loans.

- [ ] **Step 1: Failing tests:**
  - Confirm through the view.
  - Change to another candidate.
  - New entry with an account.
  - Exclude needs a reason.
  - Bulk confirm count.
  - The rule order is used.
  - A transfer from an inbox row pre-fills it.
  - A double submit posts once (token).
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of the inbox (with suggestions), Change, the rules list and form, the transfer form (loan), and the transfer list. Light and dark, sent.
- [ ] **Step 6: Commit** `feat: inbox, rules, and transfer screens`

---

### Task 12: Reconcile screens

**Files:**
- Create: `banking/views/reconcile.py`, templates
- Modify: `banking/urls.py`
- Test: `tests/test_reconcile_views.py`

**Interfaces:**
- **Start form:** statement date and ending balance.
- **Working page:**
  - Two tables, deposits and credits vs. payments and debits, with tick boxes. Ticking saves through a small fetch, or plain form posts if JavaScript is off.
  - A sticky header shows statement balance, cleared balance, and the **Difference** in the warning color until 0.00.
  - Buttons: Finish (admin, enabled at 0.00), Cancel.
- **Finished:** the reconciliation page links to `report.pdf` and offers Undo (admin, reason).

- [ ] **Step 1: Failing tests:**
  - Start, tick, finish through the views.
  - Finish is refused at a nonzero difference with the message.
  - The report PDF is served.
  - Undo needs a reason and admin.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of start, the working page (difference not zero), at zero, the finished page, and the report PDF page 1 (rasterized). Light and dark, sent.
- [ ] **Step 6: Commit** `feat: reconcile screens`

---

### Task 13: Check printing and layout screens

**Files:**
- Create: `banking/views/checks.py`, templates (`checks_to_print.html`, `print_check.html`, `layout.html`)
- Modify: `banking/urls.py`; purchasing payment detail, Pay bills result, and sales refund detail templates (a "Print check" button when `is_to_print`)
- Test: `tests/test_check_views.py`

**Interfaces:**
- **Checks to print:** CHECK vendor payments and refunds with a blank number, oldest first.
- **Print check page:**
  - Number field defaulting to `next_number`.
  - An "Open check PDF" button opens the PDF in a new tab.
  - The notice "Print at 100% (actual size), not 'fit to page'."
  - Then "Did check {n} print correctly?" with **Yes** and **No, it was spoiled**.
- **Yes:** records PRINTED, sets the payment's check number, advances `next_check_number`. **No:** records SPOILED and reloads with the next number. Both carry the one-time token, so a double click or refresh records once (Review Focus 3).
- **Reprint** (admin) of a printed check spoils the old number and goes back to the print page.
- **Layout page:** the field table (x, y, size, visible), offset nudges, "Print calibration page", and Save.

- [ ] **Step 1: Failing tests:**
  - The print flow advances the number once even when Yes is posted twice.
  - Spoiled moves to the next number.
  - A used number entered by hand is refused with the registry message.
  - Reprint is admin only.
  - Layout save and offset range.
  - The calibration PDF is served.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (demo: two vendor payments to print)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of checks to print, the print page, the confirm step, the layout page, plus the rasterized check and calibration PDFs. Light and dark for screens, sent.
- [ ] **Step 6: Commit** `feat: check printing and layout screens`

---

### Task 14: Home, acceptance, docs, PR

**Files:**
- Modify: `core/views.py`, `core/templates/core/home.html`, `README.md` (Status; What it does: Banking row; Getting started: generate `DNB_FIELD_KEY`, Plaid `.env` values, how to run `plaid_sandbox_check`, check calibration steps)
- Create: `tests/test_phase5a_acceptance.py`

- [ ] **Step 1: Home cards:**
  - **Inbox items waiting** (count, linking to the inbox).
  - **Bank sync** ("Last sync {time}"; a warning if over 24 hours or any item is NEEDS_LOGIN, with "Re-link").
  - **Not reconciled in 40 days** (account names).

  Test them.
- [ ] **Step 2: Acceptance test** (spec section 1, through the test client as an admin, with all ledger checks (`check_invariants`, `ar_check`, `undeposited_check`, `ap_check`) empty after each step):
  - Import `checking.ofx` and `card.qfx`.
  - Match an ACH payment, a deposit, and a printed check.
  - Book a fee by rule.
  - A card payoff matched from both sides.
  - A loan payment.
  - Reconcile both accounts to 0.00.
- [ ] **Step 3: Full suite** `uv run pytest -q` passes.
- [ ] **Step 4: Final screenshots:** home with banking cards, inbox, a reconciliation, and the check PDF. Sent.
- [ ] **Step 5: Commit** `test: phase 5a acceptance`
- [ ] **Step 6: Open the PR** `Phase 5a: Banking` with a full body per `CLAUDE.md`. List the manual checks:
  1. `./update.sh`.
  2. `generate_field_key` into `.env`, saved in the password manager.
  3. Plaid keys in `.env`; Production approval status checked.
  4. Run `plaid_sandbox_check` once with Sandbox keys.
  5. Create bank accounts for each ledger account.
  6. Link or import.
  7. Set each checking account's next check number.
  8. Print a calibration page, adjust the offsets, and print one test check on plain paper held against the stock.
