# DNB Books

Self-hosted replacement for Zoho Books and Zoho Inventory for a small business. Django + SQLite, runs on one office PC, a few users on the office network. The owner directs the work and is not a developer; Claude writes and maintains the code.

## Source of truth

- **Spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md` (whole system, approved).
- **Current plan:** `docs/superpowers/plans/2026-10-27-phase-1-ledger-core.md` (Phase 1, approved).

Read both before starting. The plan's Global Constraints, Review Focus, and per-task Interfaces blocks are binding. If the plan and spec conflict, or a plan step cannot work as written, stop and ask the owner rather than improvising a different design. Small mechanical fixes to the plan (a wrong import path, a typo in a test) are fine; note them in the PR.

## How to execute Phase 1

1. Work on a new branch named `claude/phase-1-ledger-core`, created from whichever branch holds this file (`main` if the handoff commit has been merged, otherwise `claude/phase-1-handoff`).
2. Use the `superpowers:subagent-driven-development` skill to execute the plan task by task (fallback: `superpowers:executing-plans`). Follow TDD as each task's steps describe: failing test, confirm it fails, implement, confirm it passes, commit.
3. One commit per task at minimum, using the commit message given in the task.
4. Tick the plan's checkboxes (`- [ ]` to `- [x]`) as steps complete, and commit that with the task.
5. During Task 7, use the `finance:journal-entry` and `finance:financial-statements` skills to review posting and report logic. Record findings in that task's commit body.
6. When all 11 tasks pass, open a PR into `main` titled `Phase 1: Ledger core`. The PR body lists each task with its status, the final `uv run pytest -q` output, any deviations from the plan and why, and the manual checks from Task 11 Step 3 that could not be run in a cloud container (these are for the owner to do on the office PC).

## Commands

```bash
uv sync                          # install deps (the SessionStart hook does this automatically once pyproject.toml exists)
uv run pytest -q                 # full test suite
uv run pytest tests/test_x.py -v # one file
uv run python manage.py migrate
uv run python manage.py serve    # start the app (waitress) on 0.0.0.0:8000
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
- One Django app per module (`core`, `ledger`, `backup` in Phase 1). Keep files focused; split a file when it grows past one clear responsibility.
- Tests live in `tests/`, pytest style, one file per task area as the plan names them.
- American English (-ize spellings) in code, UI text, and docs.
- UI text is plain and short. No marketing tone.
- Comments only where the code does something non-obvious (accounting rules, trigger behavior).

## Later phases (not now)

Phase 2 items and inventory, Phase 3 sales, Phase 4 purchasing, Phase 5 banking (Plaid, check printing, reconciliation) and Zoho migration. Each gets its own spec and plan before any code.
