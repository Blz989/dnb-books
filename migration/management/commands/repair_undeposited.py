"""Link imported payments that Zoho cleared outside a deposit to the ledger entry that cleared them.

Zoho moved years of customer payments out of Undeposited Funds with one manual journal (bank in, Undeposited
out) and paid other cash out through vendor payments. The import left those payments with no deposit, so they
showed as waiting to be banked while the ledger said Undeposited Funds was empty. This links them to the existing
entries. It posts nothing and changes no amounts.

Dry run unless --apply. It takes a backup first, refuses (changing nothing) if any total does not match exactly,
and can be run again safely.
"""
import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from backup.service import run_backup
from ledger.models import AccountSubtype, EntryStatus, JournalEntry
from sales.checks import undeposited_check
from sales.errors import DepositError
from sales.models import Deposit, Payment, SalesSettings, UndepositedClearing
from sales.posting.clearings import clear_with_entry, taken_out_of_undeposited
from sales.posting.deposits import undeposited_payments

ZERO = Decimal("0.00")


class _DryRun(Exception):
    pass


def _entry_ref(text: str) -> int:
    text = text.strip().upper().removeprefix("JE-")
    if not text.isdigit():
        raise CommandError(f'"{text}" is not an entry number like JE-111940.')
    return int(text)


class Command(BaseCommand):
    help = "Link old undeposited payments to the journal or entry that cleared them (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes (after a backup). Default is a dry run.")
        parser.add_argument("--pairs", type=Path, help="CSV with columns entry,payment_numbers (numbers separated by ;).")
        parser.add_argument("--user", help="Username to record as the user (default: the first admin).")

    def handle(self, *args, apply, pairs, user, **options):
        out = self.stdout
        settings = SalesSettings.load()
        if settings.undeposited_account_id is None:
            raise CommandError("Sales settings have no Undeposited Funds account.")
        who = get_user_model().objects.filter(username=user).first() if user else (
            get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
            or get_user_model().objects.order_by("pk").first())
        if who is None:
            raise CommandError("No user to record the change under.")
        wanted = self._read_pairs(pairs) if pairs else []

        # Work inside a transaction that is always rolled back first: nothing is written until every
        # total has been proven, and the dry run is the very same code.
        try:
            with transaction.atomic():
                made, problems = self._link(who, wanted)
                raise _DryRun
        except _DryRun:
            pass
        self._report(made, problems)
        if problems:
            raise CommandError(f"{len(problems)} total{'s' if len(problems) != 1 else ''} did not match; nothing was changed. "
                               "Fix the list above and run again.")
        if not made:
            out.write("Nothing to link.")
        if not apply:
            out.write("Dry run: nothing was changed. Add --apply to make these changes.")
            return
        if not made:
            self._remaining()
            return
        run_backup()
        out.write("Backup taken.")
        with transaction.atomic():
            made, problems = self._link(who, wanted)
            if problems:
                raise CommandError("Totals changed since the dry run; nothing was changed.")
        out.write(f"Linked {sum(len(p) for _, p, _ in made)} payments to {len(made)} entries.")
        self._remaining()

    def _read_pairs(self, path: Path) -> list[tuple[int, list[str], list[int]]]:
        if not path.is_file():
            raise CommandError(f"{path} is not a file.")
        rows = []
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("entry") or "").strip():
                    numbers = [n.strip() for n in row["payment_numbers"].split(";") if n.strip()]
                    others = [_entry_ref(n) for n in (row.get("other_entries") or "").split(";") if n.strip()]
                    rows.append((_entry_ref(row["entry"]), numbers, others))
        return rows

    def _claimed(self) -> set[int]:
        return set(UndepositedClearing.objects.values_list("journal_entry_id", flat=True)) | set(
            UndepositedClearing.objects.values_list("other_entries", flat=True)) - {None} | set(
            Deposit.objects.exclude(journal_entry=None).values_list("journal_entry_id", flat=True))

    def _link(self, who, wanted) -> tuple[list, list]:
        settings = SalesSettings.load()
        made, problems = [], []
        claimed = self._claimed()
        # 1. Bank-in / Undeposited-out journals that equal every older undeposited payment.
        candidates = JournalEntry.objects.filter(
            status=EntryStatus.POSTED, reverses=None, lines__account=settings.undeposited_account,
        ).exclude(pk__in=claimed).distinct().order_by("date", "id")
        for entry in candidates:
            lines = list(entry.lines.select_related("account"))
            if not (len(lines) == 2 and any(
                    l.debit and l.account.subtype == AccountSubtype.BANK for l in lines)):
                continue
            taken = taken_out_of_undeposited(entry)
            if taken <= 0:
                continue
            waiting = list(undeposited_payments().filter(date__lte=entry.date))
            total = sum((p.amount for p in waiting), ZERO)
            if total != taken:
                problems.append(
                    f"{entry.display_number} ({entry.date}) takes {taken:,.2f} out of Undeposited Funds, but the "
                    f"{len(waiting)} payments dated by then add up to {total:,.2f}; it does not add up, so nothing is guessed.")
                continue
            self._clear(entry, waiting, who, made, problems)
        # 2. Entries named in the pairs file.
        for ref, numbers, other_refs in wanted:
            entry = JournalEntry.objects.filter(pk=ref).first()
            payments = list(Payment.objects.filter(number__in=numbers))
            others = list(JournalEntry.objects.filter(pk__in=other_refs))
            if entry is None or len(payments) != len(set(numbers)) or len(others) != len(set(other_refs)):
                problems.append(
                    f"JE-{ref:06d}: the entry, a payment ({';'.join(numbers)}), or an other entry was not found.")
                continue
            if Payment.objects.filter(
                    pk__in=[p.pk for p in payments], clearing__journal_entry=entry).count() == len(payments):
                continue  # done by an earlier run
            self._clear(entry, payments, who, made, problems, others)
        return made, problems

    def _clear(self, entry, payments, who, made, problems, others=()) -> None:
        try:
            with transaction.atomic():
                clear_with_entry(payments=payments, entry=entry, user=who, other_entries=others,
                                 note=entry.memo.strip() or "Cleared outside a deposit")
        except DepositError as exc:
            problems.append(f"{entry.display_number}: {exc}")
            return
        made.append((entry, payments, list(others)))

    def _report(self, made, problems) -> None:
        out = self.stdout
        for entry, payments, others in made:
            total = sum((p.amount for p in payments), ZERO)
            extra = "".join(f" + {-taken_out_of_undeposited(o):,.2f} from {o.display_number}" for o in others)
            out.write(f"{len(payments)} payments, {total:,.2f}{extra}, cleared by {entry.display_number} "
                      f"({entry.date}, {entry.memo.strip() or entry.source_type}).")
        for problem in problems:
            self.stderr.write(f"Problem: {problem}")

    def _remaining(self) -> None:
        out = self.stdout
        left = list(undeposited_payments())
        problems = undeposited_check()
        if not left and not problems:
            out.write("Done. No payments are waiting, and the check agrees with the ledger.")
            return
        by_year = defaultdict(lambda: [0, ZERO])
        for p in left:
            by_year[p.date.year][0] += 1
            by_year[p.date.year][1] += p.amount
        out.write(f"{len(left)} payments, {sum((p.amount for p in left), ZERO):,.2f}, are still waiting:")
        for year, (count, total) in sorted(by_year.items()):
            out.write(f"  {year}: {count} payments, {total:,.2f}")
        for p in left[:30]:
            out.write(f"  {p.number} {p.date} {p.amount:,.2f} {p.customer.name}")
        settings = SalesSettings.load()
        claimed = self._claimed()
        explain = JournalEntry.objects.filter(
            status=EntryStatus.POSTED, reverses=None, lines__account=settings.undeposited_account,
        ).exclude(pk__in=claimed).distinct().order_by("date", "id")
        listed = [(e, taken_out_of_undeposited(e)) for e in explain]
        listed = [(e, t) for e, t in listed if t > 0]
        if listed:
            out.write("Entries that take money out of Undeposited Funds and are not linked to any payment:")
            for entry, taken in listed[:30]:
                out.write(f"  {entry.display_number} {entry.date} {taken:,.2f} {entry.memo.strip()[:60]}")
            out.write("Put the right entry and payment numbers in a pairs file (entry,payment_numbers) and use --pairs.")
        for line in problems:
            out.write(line)
