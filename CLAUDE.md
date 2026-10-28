# DNB Books

Self-hosted replacement for Zoho Books and Zoho Inventory for a small business. Django + SQLite, runs in Docker on one Linux server, a few users on the office network. The owner directs the work and is not a developer; Claude writes and maintains the code.

## Source of truth

- **System spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md` (whole system, approved).
- **Current spec:** none. Phase 5b is done; its spec is `docs/superpowers/specs/2026-10-28-phase-5b-zoho-migration-design.md`.
- **Current plan:** none. Phase 5b is done; its plan is `docs/superpowers/plans/2026-10-28-phase-5b-zoho-migration.md`.
- **Visual references:** `docs/design/items-mockup.html` ("Ledger paper"; open with `?t=light` or `?t=dark`; all screens match it) `docs/design/invoice-mockup.html` (invoice PDF), and `docs/design/check-mockup.html` (printed check).
- **Done:** Phases 1 to 5a (specs and plans in `docs/superpowers/`, merged), Docker deployment, Order profitability and handling, Phase 5b (Zoho migration and catch-up).

Read the specs and the current plan before starting. The plan's Global Constraints, Review Focus, and per-task Interfaces blocks are binding. If the plan and spec conflict, or a plan step cannot work as written, stop and ask the owner rather than improvising a different design. Small mechanical fixes to the plan (a wrong import path, a typo in a test) are fine; note them in the PR.

## How to execute the current plan

1. Work on a new branch from `main` named after the work (now: `claude/phase-5b-zoho-migration`).
2. Use the `superpowers:subagent-driven-development` skill to execute the plan task by task, unless the owner chooses inline execution (`superpowers:executing-plans`). Follow TDD as each task's steps describe: failing test, confirm it fails, implement, confirm it passes, commit.
3. One commit per task at minimum, using the commit message given in the task.
4. Tick the plan's checkboxes (`- [ ]` to `- [x]`) as steps complete, and commit that with the task.
5. Use the finance skills where the plan names them (`finance:journal-entry`, `finance:financial-statements`, `finance:reconciliation`). Record findings in that task's commit body.
6. Keep `README.md` current, including the "Status" line once the PR is ready. Do not remove the overview, roadmap, or cutover criteria.
7. When all tasks pass, open a PR into `main` with the title the plan gives. The PR body must not be empty: it lists each task with its status, the final `uv run pytest -q` output, any deviations from the plan and why, every screenshot set sent, and any manual checks the owner must do on the server.

## Standing rule: send screenshots of anything the owner will see

The owner aligns on the work visually. Whenever a session builds or changes a screen, report, PDF, or printed output:

1. Run the app against a throwaway demo database with realistic sample data (never the real `data/` database), using the pre-installed Playwright Chromium (`require('playwright')` in Node, no install needed).
2. Capture full-page screenshots of every new or changed screen, including error states and empty states that the change touches. For printed output (checks, invoices), screenshot the rendered PDF.
3. Send them to the owner with the `SendUserFile` tool as each task that changes the UI is completed, not batched at the end, with a one-line caption saying what each shows.
4. List the screens sent in the PR description. Do not commit screenshots to the repo.

If `SendUserFile` is not available in the session, save the screenshots under the session's scratchpad and say so in the PR so the owner can ask for them.

## Commands

```bash
uv sync                          # install deps (the SessionStart hook does this automatically once pyproject.toml exists)
uv run pytest -q                 # full test suite
uv run pytest tests/test_x.py -v # one file
uv run python manage.py migrate
uv run python manage.py serve    # start the app (waitress) on 0.0.0.0:8000
docker compose up -d --build     # production-like run (app + scheduler); see README
```

## Non-negotiable rules

- **Money is `Decimal`, never `float`.** Ledger amounts are `DecimalField(max_digits=14, decimal_places=2)`.
- **All ledger writes go through `ledger/posting.py`** (`post_entry`, `post_draft`, `void_entry`). No other code creates posted entries or lines.
- **Posted entries are immutable.** Corrections are voids (reversing entries). Do not weaken the model guards or the SQLite triggers to make a test pass.
- **Reports read only from posted ledger lines** (`JournalLine.objects.in_ledger()`), never from documents.
- **Never skip, xfail, or delete a failing test** to get green. Fix the code or ask.
- Error messages shown to users are plain language and match the exact strings in the plan.
- Do not add features listed as out of scope in the spec (multi-currency, sales tax, warehouses, emailing, etc.) or anything from later phases.
- No secrets in the repo. `.env` is gitignored; `.env.example` holds placeholders only.

## Conventions

- Python 3.12+, Django 5.2 LTS. Keep dependencies to the list in the plan's Tech Stack; ask before adding any other.
- One Django app per module (`core`, `ledger`, `backup`, `items`, `contacts`, `sales`, `purchasing`, `banking`, `handling`, `migration`). Keep files focused; split a file when it grows past one clear responsibility.
- Tests live in `tests/`, pytest style, one file per task area as the plan names them.
- American English (-ize spellings) in code, UI text, and docs.
- UI text is plain and short. No marketing tone.
- Comments only where the code does something non-obvious (accounting rules, trigger behavior).

## Later phases (not now)

None planned after Phase 5b. The owner's real Zoho export is never committed; dry runs against it happen in the owner's Claude session.
