"""The child half of import_zoho: runs inside the copy of the database (DNB_DB_PATH = the copy).

It replays the export, ties it out, saves an ImportRun with the report into the copy, and writes
report.html and report.pdf next to the export. It reports back only through its exit code and
output; the parent decides whether the copy is kept.
"""
from pathlib import Path

from auditlog.context import disable_auditlog
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from items.errors import StockError
from ledger.errors import PostingError
from migration.models import ImportRun
from migration.tieout import compare, render_html, render_pdf
from migration.zoho import mapping
from migration.zoho.reader import FILES, read_export
from migration.zoho.replay import EVENT_SOURCES, Replay
from purchasing.errors import PurchasingError
from sales.errors import SalesError

# Same values as in the command module (kept here to avoid importing the command).
PASSED, DIFFERENCES, FAILED, UNDEPOSITED = 0, 4, 3, 5
COPY_SUFFIX = ".import-copy"
UNDEPOSITED_PREFIX = "Undeposited transfer "
PLAIN_ERRORS = (PostingError, SalesError, PurchasingError, StockError, ValueError)


def _message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return " ".join(exc.messages)
    if isinstance(exc, PLAIN_ERRORS):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _where(event) -> str:
    short = EVENT_SOURCES["transfer" if event.kind == "undeposited_transfer" else event.kind][0]
    number = "" if event.number == event.zoho_id else f"{event.number}, "
    file = FILES[short]
    if event.zoho_id.startswith(f"{file}:"):  # file without an ID column: name the file line (header = line 1)
        return f"{file} row {int(event.zoho_id.rsplit(':', 1)[1]) + 1} ({number}{event.date})"
    return f"{file} ID {event.zoho_id} ({number}{event.date})"


def _summary(t, replay, events: int, accepted: bool) -> dict:
    differences = []
    for check in t.checks:
        if not check.provided:
            differences.append({"check": check.name, "label": "Not provided"})
        for row in check.rows:
            if not row.matches:
                differences.append({"check": check.name, "label": row.label, "ours": str(row.ours), "zoho": str(row.zoho)})
    return {
        "events": events,
        "counts": [{"label": d.label, "ours": str(d.ours), "zoho": str(d.zoho)} for d in t.counts],
        "warnings": list(replay.warnings),
        "differences": differences,
        "failed_checks": t.failed_checks,
        "count_differences": t.count_differences,
        "tieout_passed": t.passed,
        "accepted_differences": accepted,
    }


def run_child(command, opts) -> int:
    # The import keeps its own trail (ImportRun, and a ZohoRecord per document); an audit-log row for each of
    # the hundreds of thousands of imported objects would cost a sixth of the run and add nothing.
    with disable_auditlog():
        return _run_child(command, opts)


def _run_child(command, opts) -> int:
    out, err = command.stdout, command.stderr
    if not str(settings.DNB_DB_PATH).endswith(COPY_SUFFIX):
        err.write("--_into-copy only runs against the import copy.")
        return FAILED
    started = timezone.now()
    user = get_user_model().objects.get(username=opts["user"])
    export_path = Path(opts["export"])
    export = read_export(export_path)
    replay = Replay(export, user)

    try:
        with transaction.atomic():
            replay.load_master()
        if opts["deposit_pairs"]:
            replay.load_deposit_pairs(Path(opts["deposit_pairs"]))
        if opts["opening_balances"]:
            replay.load_opening_balances(Path(opts["opening_balances"]))
    except Exception as exc:  # noqa: BLE001 - reported to the owner in plain words; the copy is discarded
        err.write(f"Stopped while loading accounts, items, and contacts: {_message(exc)}")
        return FAILED

    events = replay.events()
    month, in_month, skipped = None, 0, 0
    for event in events:
        key = f"{event.date:%Y-%m}"
        if key != month:
            if month:
                out.write(f"{month}: {in_month} events")
            month, in_month = key, 0
        try:
            with transaction.atomic():
                replay.post(event)
        except Exception as exc:  # noqa: BLE001
            if not opts.get("keep_going"):
                err.write(f"Stopped at {_where(event)}: {_message(exc)}")
                return FAILED
            # Dry run only: list every failing document in one pass instead of stopping at the first.
            err.write(f"Skipped {_where(event)}: {_message(exc)}")
            skipped += 1
            continue
        in_month += 1
    if month:
        out.write(f"{month}: {in_month} events")

    try:
        after = replay.finish()
        plan = mapping.account_plan(export.files["accounts"])
        t = compare(Path(opts["reports"]), timezone.localdate(), after, plan, replay.warnings, export)
        html, pdf = render_html(t), render_pdf(t)
    except Exception as exc:  # noqa: BLE001
        err.write(f"Stopped after the replay, while tying out: {_message(exc)}")
        return FAILED

    undeposited = [w for w in replay.warnings if w.startswith(UNDEPOSITED_PREFIX)]
    accepted = bool(opts["accept_differences"]) and not t.passed
    folder = export_path.parent
    (folder / "report.html").write_text(html, encoding="utf-8")
    (folder / "report.pdf").write_bytes(pdf)
    run = ImportRun(
        started_at=started, dry_run=bool(opts["dry_run"]), export_name=export_path.name, user=user,
        summary=_summary(t, replay, len(events), accepted),
        succeeded=not undeposited and (t.passed or accepted))
    run.report_pdf.save(f"zoho-import-{started:%Y%m%d-%H%M%S}.pdf", ContentFile(pdf), save=False)
    run.finished_at = timezone.now()
    run.save()

    out.write(f"{len(events)} events replayed, {len(replay.warnings)} warnings.")
    if t.passed:
        out.write("Tie-out passed.")
    else:
        out.write(f"Tie-out did not pass: {t.failed_checks} of {len(t.checks)} checks, "
                  f"{t.count_differences} document differences.")
    out.write(f"Report: {folder / 'report.html'} and {folder / 'report.pdf'}")
    if undeposited:
        for warning in undeposited:
            err.write(warning)
        err.write("Add these transfers to deposit_pairs.csv (date, amount, payment numbers) and run again.")
        return UNDEPOSITED
    return PASSED if t.passed else DIFFERENCES
