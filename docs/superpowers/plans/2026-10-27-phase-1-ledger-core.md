# Phase 1: Ledger Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A locally hosted Django app with a chart of accounts, enforced double-entry posting, immutable entries with void/reversal, monthly period locks, four ledger reports, an audit log, and nightly encrypted off-site backups with a restore drill.

**Architecture:** One Django project (`dnb`) with focused apps: `core` (home, roles, server commands), `ledger` (models, posting service, periods, reports), `backup`. All ledger writes go through `ledger/posting.py`; database triggers back up the model-level immutability rules. Reports are pure functions over posted lines that return dataclasses; views only render them as HTML or CSV.

**Tech Stack:** Python 3.12, Django 5.2 LTS, SQLite (WAL), uv (dependency and venv management), waitress (WSGI server, works on Windows and Linux), whitenoise (static files), django-auditlog, cryptography (Fernet), python-dotenv, pytest, pytest-django, hypothesis.

**Spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md` (sections 3, 5, 9, 10, 12 apply to this phase).

## Global Constraints

- Money is `Decimal`, never `float`. Ledger amounts: `DecimalField(max_digits=14, decimal_places=2)`. The posting service rejects amounts with more than 2 decimal places; callers round.
- Single currency, USD. Accrual basis. Calendar fiscal year (Jan 1 to Dec 31).
- SQLite in WAL mode with `transaction_mode="IMMEDIATE"` and `timeout=20`.
- Two roles: **Admin** and **Standard**. Only Admin can close/reopen periods, void entries, and manage users.
- Every ledger record change is captured by the audit log (who, when, what).
- User-facing error messages are plain language (no stack traces, no field codes).
- The app is for the office network only; `ALLOWED_HOSTS` and `DNB_SECRET_KEY` come from `.env`.
- Backups: SQLite online backup API (never a raw file copy), Fernet-encrypted, written to `DNB_BACKUP_DIR`; retention 30 daily plus newest per month kept indefinitely.
- American English in all UI text.

## Review Focus

1. **Two people saving at the same moment** → no "database is locked" error; second save waits. Pinned in Task 1 (settings test) and Task 5 (threaded posting test).
2. **Amount typed with 3 decimals, a negative amount, or a line with both debit and credit** → rejected with a plain message naming the line. Pinned in Task 5.
3. **Voiding an entry whose month is closed** → reversal must be dated in an open month; the error says so and the original stays POSTED. Pinned in Task 6.
4. **Backup folder missing or unwritable (sync app uninstalled, drive unplugged)** → run recorded as FAILED, home screen shows a red warning. Pinned in Tasks 9 and 10.
5. **Someone edits a posted entry through admin or a stray SQL update** → blocked at both model and database level. Pinned in Tasks 3 and 8.

---

## File Structure

```
pyproject.toml                 uv project, deps, pytest config
manage.py
.env.example                   DNB_SECRET_KEY, DNB_ALLOWED_HOSTS, DNB_BACKUP_DIR, DNB_BACKUP_KEY, DNB_DB_PATH
start.sh / start.bat           uv run python manage.py serve
update.sh / update.bat         backup_now, git pull, uv sync, migrate, collectstatic
README.md                      install, start, update, backup scheduling, restore
dnb/settings.py, urls.py, wsgi.py
core/
  roles.py                     is_admin(), ADMIN_GROUP, STANDARD_GROUP
  views.py                     home
  management/commands/serve.py, setup_roles.py
  templates/core/home.html, base.html
ledger/
  models.py                    Account, JournalEntry, JournalLine, AccountingPeriod, enums
  migrations/0002_immutability_triggers.py
  errors.py                    PostingError hierarchy
  periods.py                   period lookup, close, reopen
  posting.py                   LineSpec, post_entry, post_draft, void_entry
  balances.py                  signed_balance, ledger_lines
  reports.py                   trial_balance, general_ledger, profit_and_loss, balance_sheet
  views.py, urls.py, csv_export.py
  admin.py
  templates/ledger/*.html
backup/
  models.py                    BackupRun
  service.py                   snapshot, encrypt/decrypt, fingerprint, retention, run_backup, restore_drill, backup_status
  management/commands/backup_now.py, restore_drill.py, restore_backup.py, generate_backup_key.py
tests/
  conftest.py                  fixtures: admin_user, standard_user, accounts
  test_*.py                    one file per task area
```

---

### Task 1: Project skeleton, settings, home page, server commands

**Files:**
- Create: `pyproject.toml`, `manage.py`, `.env.example`, `.gitignore`, `dnb/settings.py`, `dnb/urls.py`, `dnb/wsgi.py`, `core/apps.py`, `core/views.py`, `core/templates/core/base.html`, `core/templates/core/home.html`, `core/management/commands/serve.py`, `start.sh`, `start.bat`, `update.sh`, `update.bat`
- Modify: `README.md` (already exists with project overview; replace the "Getting started" planned-workflow section with real instructions)
- Test: `tests/test_skeleton.py`, `tests/conftest.py`

**Interfaces:**
- Produces: URL name `home` at `/`; template `core/base.html` with blocks `title`, `content`, `messages`; settings keys `DNB_BACKUP_DIR: Path`, `DNB_BACKUP_KEY: str`, `DNB_DB_PATH: Path`; management command `serve [--host 0.0.0.0] [--port 8000]`.

- [x] **Step 1: Write the failing tests**

```python
def test_anonymous_home_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]

def test_logged_in_home_ok(client, standard_user):
    client.force_login(standard_user)
    assert client.get("/").status_code == 200

def test_sqlite_options_for_concurrency(settings):
    opts = settings.DATABASES["default"]["OPTIONS"]
    assert "journal_mode=WAL" in opts["init_command"]
    assert opts["transaction_mode"] == "IMMEDIATE"
    assert opts["timeout"] >= 20
```

`conftest.py` defines `standard_user` and `admin_user` fixtures (plain users for now; Task 2 adds groups).

- [x] **Step 2: Run tests, expect FAIL** (`uv run pytest tests/test_skeleton.py -v`, no project yet)

- [x] **Step 3: Implement the skeleton**
  - `pyproject.toml`: `requires-python = ">=3.12"`, deps `Django>=5.2,<6.0`, `waitress`, `whitenoise`, `django-auditlog`, `cryptography`, `python-dotenv`; dev deps under `[dependency-groups] dev = ["pytest", "pytest-django", "hypothesis"]` (installed by `uv sync` and by the SessionStart hook in `.claude/hooks/session-start.sh`); `.gitignore` covers `.env`, `.venv/`, `data/`, `staticfiles/`, `__pycache__/`; `[tool.pytest.ini_options] DJANGO_SETTINGS_MODULE = "dnb.settings"`.
  - `settings.py` loads `.env` via `python-dotenv`; DB `NAME = DNB_DB_PATH` (default `BASE_DIR / "data" / "dnb.sqlite3"`), `OPTIONS = {"init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;", "transaction_mode": "IMMEDIATE", "timeout": 20}`; `TEST = {"NAME": BASE_DIR / "data" / "test.sqlite3"}` so tests run on a real file (needed for the threaded posting test and backup snapshots); `LOGIN_URL = "/accounts/login/"`; include `django.contrib.auth.urls`; whitenoise middleware; `TIME_ZONE` from env, default `America/Los_Angeles` (override in `.env`); `USE_TZ = True`.
  - `serve` command runs `waitress.serve(get_wsgi_application(), host=..., port=...)`.
  - `start.*` runs `uv run python manage.py serve`. `update.*` runs in order and stops on first failure: `uv run python manage.py backup_now` (skip with a printed notice if the command does not exist yet), `git pull`, `uv sync`, `uv run python manage.py migrate`, `uv run python manage.py collectstatic --noinput`.
  - `README.md` "Getting started": first-time install (install uv, copy `.env.example` to `.env`, `uv sync`, `migrate`, `createsuperuser`, `setup_roles`), start, update.

- [x] **Step 4: Run tests, expect PASS**

- [x] **Step 5: Commit** `feat: project skeleton with SQLite WAL, login, serve command`

---

### Task 2: Roles and audit log

**Files:**
- Create: `core/roles.py`, `core/management/commands/setup_roles.py`
- Modify: `dnb/settings.py` (add `auditlog` app and `auditlog.middleware.AuditlogMiddleware`), `tests/conftest.py`
- Test: `tests/test_roles.py`

**Interfaces:**
- Produces: `ADMIN_GROUP = "Admin"`, `STANDARD_GROUP = "Standard"`, `is_admin(user) -> bool` (true if superuser or in Admin group), `require_admin(user) -> None` (raises `django.core.exceptions.PermissionDenied` with message "Only an admin can do this."). Command `setup_roles` is idempotent and gives Standard `add/change/view` on `ledger.Account`, `ledger.JournalEntry`, `ledger.JournalLine` and `view` on `ledger.AccountingPeriod`; Admin gets all ledger and backup permissions. Both groups are `is_staff`-eligible (admin site access).
- Fixtures: `admin_user` (staff, Admin group), `standard_user` (staff, Standard group).

- [x] **Step 1: Write failing tests**

```python
def test_is_admin(admin_user, standard_user):
    assert is_admin(admin_user) and not is_admin(standard_user)

def test_require_admin_denies_standard(standard_user):
    with pytest.raises(PermissionDenied, match="Only an admin"):
        require_admin(standard_user)

def test_setup_roles_idempotent(db):
    call_command("setup_roles"); call_command("setup_roles")
    assert Group.objects.filter(name__in=["Admin", "Standard"]).count() == 2
```

Note: `setup_roles` runs after Task 3 models exist; write the permission assignment so missing models are skipped with a warning, and re-verify in Task 3 Step 4.

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement `core/roles.py` and `setup_roles`; enable auditlog middleware**
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Commit** `feat: admin/standard roles and audit log middleware`

---

### Task 3: Ledger models and database-level immutability

**Files:**
- Create: `ledger/apps.py`, `ledger/models.py`, `ledger/errors.py`, `ledger/migrations/0001_initial.py` (generated), `ledger/migrations/0002_immutability_triggers.py`
- Test: `tests/test_ledger_models.py`

**Interfaces:**
- Produces:
  - `AccountType` (TextChoices): `ASSET, LIABILITY, EQUITY, INCOME, EXPENSE`.
  - `AccountSubtype` (TextChoices) and `SUBTYPES_BY_TYPE: dict[AccountType, set[AccountSubtype]]`:
    ASSET → `BANK, ACCOUNTS_RECEIVABLE, INVENTORY, OTHER_CURRENT_ASSET, FIXED_ASSET, OTHER_ASSET`;
    LIABILITY → `ACCOUNTS_PAYABLE, CREDIT_CARD, OTHER_CURRENT_LIABILITY, LONG_TERM_LIABILITY`;
    EQUITY → `EQUITY`; INCOME → `INCOME, OTHER_INCOME`;
    EXPENSE → `COST_OF_GOODS_SOLD, EXPENSE, OTHER_EXPENSE`.
  - `Account(number: CharField(20, unique), name: CharField(200), type, subtype, is_active=True, description)`; `clean()` raises `ValidationError` if subtype not in `SUBTYPES_BY_TYPE[type]`; `save()` calls `full_clean()`.
  - `EntryStatus` (TextChoices): `DRAFT, POSTED, VOID`.
  - `JournalEntry(date, reference: CharField(50, blank), memo: TextField(blank), source_type: CharField(50, blank), source_id: CharField(50, blank), status=DRAFT, reverses: FK self null related_name="reversals", created_by FK User, posted_by FK User null, posted_at null)`; property `display_number -> str` = `f"JE-{pk:06d}"`.
  - `JournalLine(entry FK CASCADE related_name="lines", account FK PROTECT, debit Decimal(14,2) default 0, credit Decimal(14,2) default 0, description CharField(200, blank))`. DB `CheckConstraint`s: `debit >= 0`, `credit >= 0`, exactly one of debit/credit > 0.
  - `JournalLine.objects.in_ledger()` → lines whose entry status is POSTED or VOID (VOID originals stay in the ledger; their reversal nets them to zero).
  - `AccountingPeriod(year, month, is_closed=False, closed_by null, closed_at null, reopen_reason TextField(blank))`, unique `(year, month)`.
  - `ledger/errors.py`: `PostingError(Exception)` base with subclasses `UnbalancedEntryError`, `PeriodClosedError`, `InactiveAccountError`, `InvalidLineError`, `EntryStateError`, `ImmutableEntryError`.
  - Model guards: `JournalEntry.save()` raises `ImmutableEntryError("Posted entries can't be changed. Void it and create a new entry.")` if the stored row is not DRAFT and anything other than the POSTED→VOID status change is attempted; `delete()` raises for non-DRAFT. `JournalLine.save()/delete()` raise when parent is not DRAFT.
  - All four models registered with `auditlog.register`.
- Triggers (migration 0002, `RunSQL` with reverse SQL dropping them):
  - `BEFORE DELETE ON ledger_journalentry WHEN OLD.status != 'DRAFT'` → `RAISE(ABORT, 'posted entry is immutable')`.
  - `BEFORE UPDATE ON ledger_journalentry WHEN OLD.status = 'VOID' OR (OLD.status = 'POSTED' AND (NEW.status NOT IN ('POSTED','VOID') OR NEW.date IS NOT OLD.date OR NEW.reference IS NOT OLD.reference OR NEW.memo IS NOT OLD.memo OR NEW.source_type IS NOT OLD.source_type OR NEW.source_id IS NOT OLD.source_id OR NEW.reverses_id IS NOT OLD.reverses_id))` → abort.
  - `BEFORE INSERT`, `BEFORE UPDATE`, `BEFORE DELETE ON ledger_journalline` when the parent entry's status is not `'DRAFT'` → abort.

- [ ] **Step 1: Write failing tests**

```python
def test_subtype_must_match_type(db):
    with pytest.raises(ValidationError):
        Account.objects.create(number="1000", name="X", type=AccountType.ASSET, subtype=AccountSubtype.ACCOUNTS_PAYABLE)

def test_line_cannot_have_both_debit_and_credit(draft_entry, cash):
    with pytest.raises(IntegrityError):
        JournalLine.objects.create(entry=draft_entry, account=cash, debit=Decimal("1.00"), credit=Decimal("1.00"))

def test_posted_entry_model_save_blocked(posted_entry):
    posted_entry.memo = "changed"
    with pytest.raises(ImmutableEntryError):
        posted_entry.save()

def test_posted_entry_sql_update_blocked(posted_entry):
    with pytest.raises(DatabaseError, match="immutable"):
        JournalEntry.objects.filter(pk=posted_entry.pk).update(memo="changed")

def test_posted_line_sql_update_blocked(posted_entry):
    with pytest.raises(DatabaseError):
        JournalLine.objects.filter(entry=posted_entry).update(debit=Decimal("9.99"))

def test_account_with_lines_cannot_be_deleted(posted_entry, cash):
    with pytest.raises(ProtectedError):
        cash.delete()

def test_in_ledger_excludes_drafts(draft_entry, posted_entry):
    assert set(JournalLine.objects.in_ledger().values_list("entry_id", flat=True)) == {posted_entry.pk}
```

Fixtures in `conftest.py`: `cash` (1000, ASSET/BANK), `revenue` (4000, INCOME/INCOME), `expense` (6000, EXPENSE/EXPENSE), `ar`, `ap`, `equity` as needed; `draft_entry`; `posted_entry` built as a DRAFT with two lines, then promoted with `JournalEntry.objects.filter(pk=...).update(status=EntryStatus.POSTED)` (the trigger allows DRAFT→POSTED); switch it to `post_entry` in Task 5.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement models, errors, generate 0001, hand-write 0002 triggers, register with auditlog**
- [ ] **Step 4: Run, expect PASS**; also re-run `tests/test_roles.py` (setup_roles now finds ledger models)
- [ ] **Step 5: Commit** `feat: ledger models with model and trigger immutability`

---

### Task 4: Accounting periods

**Files:**
- Create: `ledger/periods.py`
- Test: `tests/test_periods.py`

**Interfaces:**
- Consumes: `AccountingPeriod`, `require_admin`, `PeriodClosedError`.
- Produces:
  - `get_period(d: date) -> AccountingPeriod` (get-or-create, open by default).
  - `is_closed(d: date) -> bool`.
  - `assert_open(d: date) -> None` raises `PeriodClosedError(f"{Month YYYY} is closed. Choose a date in an open month or ask an admin to reopen it.")`.
  - `close_period(year: int, month: int, user) -> AccountingPeriod` (admin only; sets `closed_by`, `closed_at`).
  - `reopen_period(year: int, month: int, user, reason: str) -> AccountingPeriod` (admin only; `reason` required and stripped non-empty, else `ValueError("A reason is required to reopen a period.")`; stores `reopen_reason`).

- [ ] **Step 1: Write failing tests**

```python
def test_new_period_is_open(db):
    assert not is_closed(date(2026, 3, 15))

def test_close_then_assert_open_raises(admin_user):
    close_period(2026, 3, admin_user)
    with pytest.raises(PeriodClosedError, match="March 2026 is closed"):
        assert_open(date(2026, 3, 31))
    assert_open(date(2026, 4, 1))  # next month unaffected

def test_standard_user_cannot_close(standard_user):
    with pytest.raises(PermissionDenied):
        close_period(2026, 3, standard_user)

def test_reopen_requires_reason(admin_user):
    close_period(2026, 3, admin_user)
    with pytest.raises(ValueError):
        reopen_period(2026, 3, admin_user, "  ")
    p = reopen_period(2026, 3, admin_user, "Late vendor bill")
    assert not p.is_closed and p.reopen_reason == "Late vendor bill"
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement `ledger/periods.py`**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: monthly period close and admin-only reopen`

---

### Task 5: Posting service

**Files:**
- Create: `ledger/posting.py`
- Modify: `tests/conftest.py` (`posted_entry` fixture now uses `post_entry`)
- Test: `tests/test_posting.py`

**Interfaces:**
- Consumes: models, errors, `periods.assert_open`.
- Produces:
  - `@dataclass(frozen=True) LineSpec(account: Account, debit: Decimal = Decimal("0"), credit: Decimal = Decimal("0"), description: str = "")`.
  - `post_entry(*, date: date, lines: Sequence[LineSpec], user, memo: str = "", reference: str = "", source_type: str = "", source_id: str = "", reverses: JournalEntry | None = None) -> JournalEntry`: validates, then in one `transaction.atomic()` creates entry as DRAFT, bulk-creates lines, sets status POSTED, `posted_by`, `posted_at`.
  - `post_draft(entry: JournalEntry, user) -> JournalEntry`: same validation on the draft's existing lines; raises `EntryStateError("Only draft entries can be posted.")` if not DRAFT.
  - `validate_lines(lines: Sequence[LineSpec]) -> None` (shared by both).
- Validation order and messages (line numbers are 1-based):
  1. Fewer than 2 lines → `InvalidLineError("An entry needs at least two lines.")`
  2. Any amount negative → `InvalidLineError("Line {n}: amounts can't be negative.")`
  3. Any amount with more than 2 decimal places → `InvalidLineError("Line {n}: amounts can have at most 2 decimal places.")`
  4. Both or neither of debit/credit > 0 → `InvalidLineError("Line {n}: enter a debit or a credit, not both.")` / `("Line {n}: enter an amount.")`
  5. Inactive account → `InactiveAccountError("Line {n}: account {number} {name} is inactive.")`
  6. Sum debits ≠ sum credits → `UnbalancedEntryError("Debits ({d}) and credits ({c}) must be equal. Difference: {diff}.")` formatted with 2 decimals and thousands separators.
  7. Date in closed period → `PeriodClosedError` from `assert_open`.

- [ ] **Step 1: Write failing tests**

```python
def test_post_balanced_entry(admin_user, cash, revenue):
    e = post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
        LineSpec(cash, debit=Decimal("100.00")), LineSpec(revenue, credit=Decimal("100.00"))])
    assert e.status == EntryStatus.POSTED and e.lines.count() == 2

def test_unbalanced_rejected_and_nothing_saved(admin_user, cash, revenue):
    with pytest.raises(UnbalancedEntryError, match="Difference: 0.01"):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=Decimal("100.00")), LineSpec(revenue, credit=Decimal("99.99"))])
    assert JournalEntry.objects.count() == 0

@pytest.mark.parametrize("amount,msg", [
    (Decimal("-5.00"), "Line 1: amounts can't be negative"),
    (Decimal("1.005"), "Line 1: amounts can have at most 2 decimal places")])
def test_bad_amounts(admin_user, cash, revenue, amount, msg): ...

def test_both_debit_and_credit_rejected(...): ...  # "Line 2: enter a debit or a credit, not both."
def test_inactive_account_rejected(...): ...
def test_closed_period_rejected(...): ...           # close Jan 2026, post on 2026-01-31
def test_post_draft_validates_existing_lines(...): ...

# Property test lives in a hypothesis.extra.django.TestCase subclass so each example gets a fresh transaction.
@given(st.lists(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999.99"), places=2), min_size=1, max_size=10))
def test_ledger_always_balances(amounts): ...
# post one entry per amount (debit cash / credit revenue); assert sum(debit) == sum(credit) over JournalLine.objects.in_ledger()

@pytest.mark.django_db(transaction=True)
def test_concurrent_posts_both_succeed(admin_user, cash, revenue): ...
# Review Focus 1: two threads (each closes its connection at the end) each post 20 entries on the file-backed test DB; assert 40 posted entries and no OperationalError.
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement `ledger/posting.py`** (use `Decimal.as_tuple().exponent < -2` for the precision check)
- [ ] **Step 4: Run, expect PASS** (full suite)
- [ ] **Step 5: Commit** `feat: posting service with balance, precision, and period validation`

---

### Task 6: Void and reversal

**Files:**
- Modify: `ledger/posting.py`
- Test: `tests/test_void.py`

**Interfaces:**
- Produces: `void_entry(entry: JournalEntry, *, user, reason: str, date: date | None = None) -> JournalEntry`. Admin only (`require_admin`). `reason` required. Date defaults to `entry.date`. In one transaction: posts a reversal via `post_entry` with each line's debit and credit swapped, `memo = f"Void of {entry.display_number}: {reason}"`, `reverses=entry`, same `source_type/source_id`; then sets `entry.status = VOID`. Returns the reversal.
- Errors: entry not POSTED → `EntryStateError("Only posted entries can be voided.")`. Reversal date in a closed month → `PeriodClosedError` with the message from `assert_open`, and the original stays POSTED.

- [ ] **Step 1: Write failing tests**

```python
def test_void_nets_to_zero(admin_user, posted_entry, cash):
    rev = void_entry(posted_entry, user=admin_user, reason="Duplicate")
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.VOID and rev.reverses == posted_entry
    assert balance_for(cash) == Decimal("0.00")   # helper summing in_ledger lines

def test_void_in_closed_month_needs_open_date(admin_user, posted_entry):  # Review Focus 3
    close_period(posted_entry.date.year, posted_entry.date.month, admin_user)
    with pytest.raises(PeriodClosedError):
        void_entry(posted_entry, user=admin_user, reason="x")
    posted_entry.refresh_from_db(); assert posted_entry.status == EntryStatus.POSTED
    rev = void_entry(posted_entry, user=admin_user, reason="x", date=date(2026, 2, 1))
    assert rev.date == date(2026, 2, 1)

def test_cannot_void_twice(...): ...
def test_standard_user_cannot_void(...): ...
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement `void_entry`**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: void posted entries via reversing entry`

---

### Task 7: Reports (trial balance, general ledger, P&L, balance sheet)

**Files:**
- Create: `ledger/balances.py`, `ledger/reports.py`
- Test: `tests/test_reports.py`

**Interfaces:**
- Produces:
  - `signed_balance(account_type: AccountType, debit: Decimal, credit: Decimal) -> Decimal` (ASSET and EXPENSE: debit − credit; others: credit − debit).
  - `account_totals(*, start: date | None, end: date) -> dict[int, tuple[Decimal, Decimal]]` (account_id → (debit sum, credit sum) over `in_ledger()` lines with `start <= entry.date <= end`; inclusive both ends).
  - `trial_balance(as_of: date) -> TrialBalance(as_of, rows: list[TBRow(account, debit, credit)], total_debit, total_credit)`. Each row shows its net in the debit or credit column; zero-balance accounts omitted; sorted by account number.
  - `general_ledger(account: Account, start: date, end: date) -> GeneralLedger(account, start, end, opening_balance, lines: list[GLLine(date, entry_number, reference, description, debit, credit, balance)], closing_balance)`. Balances are signed per `signed_balance`; lines ordered by date then entry id.
  - `profit_and_loss(start: date, end: date) -> ProfitAndLoss(start, end, income, cost_of_goods_sold, operating_expenses, other_income, other_expenses: list[ReportRow(account, amount)], gross_profit, net_income)`. Sections by subtype: INCOME, COST_OF_GOODS_SOLD, EXPENSE, OTHER_INCOME, OTHER_EXPENSE.
  - `balance_sheet(as_of: date) -> BalanceSheet(as_of, assets, liabilities, equity: list[ReportRow], retained_earnings, current_year_earnings, total_assets, total_liabilities_and_equity)`. `current_year_earnings` = net income Jan 1 of `as_of.year` through `as_of`; `retained_earnings` = net income of all dates before that Jan 1. No closing entries are posted (matches Zoho).
  - All dataclasses frozen; amounts quantized to `0.01`.

- [ ] **Step 1: Write failing tests** (one scenario fixture `sample_ledger`: owner invests 10,000 cash on 2025-06-01; sale 1,000 cash 2025-12-31; expense 300 on 2026-01-01; sale 500 on AR 2026-01-31; plus one draft entry and one voided entry that must not change results)

```python
def test_trial_balance_balances(sample_ledger):
    tb = trial_balance(date(2026, 1, 31))
    assert tb.total_debit == tb.total_credit == Decimal("11500.00")

def test_trial_balance_includes_boundary_day(sample_ledger):
    assert trial_balance(date(2025, 12, 31)).total_debit == Decimal("11000.00")

def test_pnl_range_inclusive(sample_ledger):
    p = profit_and_loss(date(2026, 1, 1), date(2026, 1, 31))
    assert p.net_income == Decimal("200.00")

def test_balance_sheet_balances_across_year_end(sample_ledger):
    bs = balance_sheet(date(2026, 1, 31))
    assert bs.retained_earnings == Decimal("1000.00")
    assert bs.current_year_earnings == Decimal("200.00")
    assert bs.total_assets == bs.total_liabilities_and_equity == Decimal("11200.00")

def test_general_ledger_running_balance(sample_ledger, cash):
    gl = general_ledger(cash, date(2026, 1, 1), date(2026, 1, 31))
    assert gl.opening_balance == Decimal("11000.00") and gl.closing_balance == Decimal("10700.00")

def test_draft_and_void_have_no_effect(sample_ledger): ...
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement `balances.py` and `reports.py`** (aggregate in the database with `Sum`, convert with `Decimal(str(v))`)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: trial balance, general ledger, P&L, balance sheet`

Before committing, review the posting and report rules against the `finance:journal-entry` and `finance:financial-statements` skills and note any finding in the commit body.

---

### Task 8: Admin UI and report pages

**Files:**
- Create: `ledger/admin.py`, `ledger/views.py`, `ledger/urls.py`, `ledger/csv_export.py`, `ledger/forms.py`, `ledger/templates/ledger/{trial_balance,general_ledger,profit_and_loss,balance_sheet,void_entry,reopen_period}.html`
- Modify: `dnb/urls.py`, `core/templates/core/home.html` (links to reports and admin)
- Test: `tests/test_admin.py`, `tests/test_report_views.py`

**Interfaces:**
- Consumes: `post_draft`, `void_entry`, `close_period`, `reopen_period`, report functions, `is_admin`.
- Produces URLs (all login required):
  - `reports:trial-balance` `/reports/trial-balance/?as_of=YYYY-MM-DD`
  - `reports:profit-and-loss` `/reports/profit-and-loss/?start=&end=`
  - `reports:balance-sheet` `/reports/balance-sheet/?as_of=`
  - `reports:general-ledger` `/reports/general-ledger/?account=<id>&start=&end=`
  - Adding `&format=csv` returns `text/csv` with filename `<report>-<date(s)>.csv`. Defaults: `as_of`/`end` = today, `start` = Jan 1 of the current year. Invalid dates re-render the form with "Enter a date like 2026-01-31." (no 500).
- Admin:
  - `AccountAdmin`: list by number, filter by type/active; delete of an account with lines shows Django's protected-object page (no 500).
  - `JournalEntryAdmin` with `JournalLineInline`: editable only while DRAFT (`has_change_permission`/`has_delete_permission` return False otherwise; inline add/delete off). Shows totals and difference. Action **"Post selected drafts"** calls `post_draft` per entry and reports each `PostingError` message via `messages.error`. Object page for POSTED entries shows a **Void** button (admin only) linking to `/admin/ledger/journalentry/<id>/void/`, a form with `date` and `reason`.
  - `AccountingPeriodAdmin`: actions **Close selected periods** (admin only) and a per-row **Reopen** link to a form requiring `reason`.

- [ ] **Step 1: Write failing tests**

```python
def test_report_csv(client, admin_user, sample_ledger):
    client.force_login(admin_user)
    r = client.get("/reports/trial-balance/?as_of=2026-01-31&format=csv")
    assert r["Content-Type"].startswith("text/csv") and b"11,500.00" not in r.content and b"11500.00" in r.content

def test_bad_date_shows_message(client, admin_user):
    client.force_login(admin_user)
    r = client.get("/reports/balance-sheet/?as_of=31/01/2026")
    assert r.status_code == 200 and b"Enter a date like 2026-01-31." in r.content

def test_admin_post_action_shows_plain_error(client, admin_user, unbalanced_draft): ...
def test_posted_entry_change_view_is_read_only(client, admin_user, posted_entry): ...  # Review Focus 5: POST to change view returns 403 and data unchanged
def test_standard_user_has_no_void_button(client, standard_user, posted_entry): ...
def test_reopen_without_reason_rejected(client, admin_user): ...
```

CSV amounts are plain numbers with 2 decimals, no thousands separators. HTML shows separators.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement admin, forms, views, templates**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: admin screens for ledger and report pages with CSV export`

---

### Task 9: Backup (snapshot, encryption, retention, run record)

**Files:**
- Create: `backup/apps.py`, `backup/models.py`, `backup/service.py`, `backup/management/commands/backup_now.py`, `backup/management/commands/generate_backup_key.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- Produces:
  - `BackupRun(started_at, finished_at null, status: RUNNING|SUCCESS|FAILED, filename blank, size_bytes null, ledger_fingerprint CharField(64, blank), error TextField(blank))`.
  - `snapshot(dest: Path) -> Path` using `sqlite3.connect(src).backup(sqlite3.connect(dest))` on `settings.DNB_DB_PATH`.
  - `ledger_fingerprint(conn: sqlite3.Connection) -> str`: SHA-256 hex over sorted lines `f"{account_id}|{debit_sum}|{credit_sum}"` for lines whose entry status is `POSTED` or `VOID`, sums computed in Python as `Decimal(str(v))` quantized to 0.01, plus a final line `f"entries|{count}"`.
  - `encrypt_file(src: Path, dst: Path, key: str) -> None`, `decrypt_file(src: Path, dst: Path, key: str) -> None` (Fernet).
  - `backup_filename(ts: datetime) -> str` = `f"dnb-{ts:%Y%m%d-%H%M%S}.sqlite3.enc"`.
  - `apply_retention(directory: Path, today: date) -> list[Path]` (returns deleted). Keep files dated within the last 30 days (today minus 29 days, inclusive); for older files keep only the newest per calendar month; never touch files not matching the filename pattern.
  - `run_backup(now: datetime | None = None) -> BackupRun`: records RUNNING, snapshots to a temp dir, computes fingerprint, encrypts into `DNB_BACKUP_DIR`, applies retention, records SUCCESS. Any exception → FAILED with a plain message (for example "Backup folder D:\Drive\dnb-backups does not exist.") and is not re-raised. Missing `DNB_BACKUP_KEY` → FAILED "No backup key set. Run generate_backup_key."
  - Commands: `backup_now` (exit code 1 if FAILED, prints message), `generate_backup_key` (prints a new Fernet key and the instruction: "Add this to .env as DNB_BACKUP_KEY and store a copy in your password manager. Backups cannot be restored without it.").

- [ ] **Step 1: Write failing tests**

```python
def test_encrypt_roundtrip(tmp_path): ...
def test_run_backup_success_writes_encrypted_file(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    assert run.status == "SUCCESS" and (tmp_path / run.filename).exists()
    assert not (tmp_path / run.filename).read_bytes().startswith(b"SQLite format 3")

def test_missing_backup_dir_records_failure(file_db_with_entries, tmp_path, settings):  # Review Focus 4
    settings.DNB_BACKUP_DIR = tmp_path / "gone"
    run = run_backup()
    assert run.status == "FAILED" and "does not exist" in run.error

def test_retention(tmp_path):
    # files for 2026-10-27 back 40 days, plus two in Aug 2026 and two in Jul 2026, plus "notes.txt"
    deleted = apply_retention(tmp_path, date(2026, 10, 27))
    # kept: 2026-09-28..2026-10-27, newest of Aug, newest of Jul, notes.txt
```

`file_db_with_entries` fixture: `transactional_db` plus a few posted entries; `settings.DNB_DB_PATH` points at the file-backed test DB name from Task 1.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement `backup/service.py`, model, commands; wire `update.*` to call `backup_now` first (already scripted in Task 1)**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: encrypted nightly backup with retention`

---

### Task 10: Restore, restore drill, home screen status, scheduling docs

**Files:**
- Modify: `backup/service.py`, `core/views.py`, `core/templates/core/home.html`, `README.md`
- Create: `backup/management/commands/restore_drill.py`, `backup/management/commands/restore_backup.py`
- Test: `tests/test_restore.py`, `tests/test_home_status.py`

**Interfaces:**
- Consumes: `BackupRun`, `decrypt_file`, `ledger_fingerprint`.
- Produces:
  - `restore_drill() -> DrillResult(ok: bool, backup_file: str, expected: str, actual: str, message: str)`: decrypts the newest successful run's file to a temp dir, opens it read-only, compares `ledger_fingerprint` with `BackupRun.ledger_fingerprint`. Command exits 1 when not ok.
  - `restore_backup <file> --to <path>`: decrypts to `--to`; refuses to overwrite an existing file; prints the steps to swap it in (stop app, rename current DB, move restored file to `DNB_DB_PATH`, start app).
  - `@dataclass BackupStatus(level: "ok" | "warning" | "error", message: str)`; `backup_status(now: datetime) -> BackupStatus`: `error` if the latest run FAILED ("Last backup failed: {error}"); `warning` if no SUCCESS in the last 26 hours or none ever ("No successful backup in the last 26 hours."); else `ok` ("Last backup {time}.").
  - Home page renders the status as a colored banner, visible to every logged-in user.
  - README section "Nightly backup" (under "Getting started"): Windows Task Scheduler and Linux cron instructions running `backup_now` at 1:15 AM, and `restore_drill` on the 1st of each month.

- [ ] **Step 1: Write failing tests**

```python
def test_drill_passes_on_fresh_backup(file_db_with_entries, tmp_path, settings): ...
def test_drill_detects_tampered_backup(...): ...   # re-encrypt a modified DB under the same name; ok is False
def test_restore_refuses_overwrite(tmp_path): ...
def test_status_error_after_failed_run(db): ...
def test_status_warning_when_stale(db): ...        # last success 27 hours ago
def test_home_shows_banner(client, standard_user, failed_run):
    client.force_login(standard_user)
    assert b"Last backup failed" in client.get("/").content
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: restore drill, restore command, backup status on home screen`

---

### Task 11: Phase 1 acceptance

**Files:**
- Create: `tests/test_acceptance.py`

- [ ] **Step 1: Write the acceptance test** (Spec 12, "Phase 1 done when"): as an admin through the Django test client only, create two accounts in admin, create a draft entry with two lines, post it with the admin action, then load all four report pages and assert the amount appears on each; run `backup_now` and `restore_drill` via `call_command` against the file-backed test DB and assert both exit cleanly.
- [ ] **Step 2: Run full suite** `uv run pytest -q` → all pass, no warnings about naive datetimes.
- [ ] **Step 3: Manual check on a real machine:** `start` script, log in from a second browser on the network, post one entry, view reports, run `backup_now`, confirm the encrypted file appears in the synced folder, run `restore_drill`.
- [ ] **Step 4: Commit** `test: phase 1 acceptance`
