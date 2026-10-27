"""Statement files (OFX, QFX, CSV) parsed into one shape and saved as bank transactions.

Amounts are kept as the file states them. A card or loan statement already shows purchases and draws as
negative, which is the sign banking stores, so nothing is flipped.
"""
import csv
import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import BankAccount, BankTransaction, TxnSource, TxnStatus

D = Decimal


@dataclass(frozen=True)
class ParsedTxn:
    external_id: str
    date: date_type
    description: str
    amount: Decimal
    check_number: str = ""


@dataclass
class CsvMapping:
    date: str
    description: str
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    date_format: str = "%m/%d/%Y"
    check_number: str | None = None


@dataclass(frozen=True)
class RowError:
    row: int
    problem: str

    def __str__(self) -> str:
        return f"Row {self.row}: {self.problem}."


@dataclass(frozen=True)
class ImportResult:
    added: int
    skipped: int
    possible_duplicates: int


class CsvImportError(Exception):
    def __init__(self, errors: list[RowError]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


def parse_ofx(data: bytes) -> list[ParsedTxn]:
    from ofxtools.Parser import OFXTree

    try:
        tree = OFXTree()
        tree.parse(io.BytesIO(data))
        ofx = tree.convert()
        statements = list(ofx.statements)
    except Exception:
        raise ValueError("That file isn't a readable OFX or QFX statement.") from None
    if not statements:
        raise ValueError("That file isn't a readable OFX or QFX statement.")
    txns = []
    for stmt in statements:
        for t in stmt.transactions:
            txns.append(ParsedTxn(
                external_id=t.fitid,
                date=t.dtposted.date(),
                description=(t.name or t.memo or "").strip(),
                amount=D(t.trnamt),
                check_number=(t.checknum or "").strip(),
            ))
    return txns


def _amount(raw: str) -> Decimal:
    """'$1,234.56', '(45.00)', '-$15.00' and '12' all read; anything else raises ValueError."""
    text = raw.strip().replace("$", "").replace(",", "").replace(" ", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = D(text)
    except InvalidOperation:
        raise ValueError("amount must be a number") from None
    if not value.is_finite():
        raise ValueError("amount must be a number")
    if value.as_tuple().exponent < -2:
        raise ValueError("amount can have at most 2 decimal places")
    return -value if negative else value


def _cell(row: dict, column: str | None) -> str:
    return (row.get(column) or "").strip() if column else ""


def _external_id(day: date_type, amount: Decimal, description: str, occurrence: int) -> str:
    key = f"{day.isoformat()}|{amount:.2f}|{description}|{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()


def _date_hint(fmt: str) -> str:
    return fmt.replace("%m", "MM").replace("%d", "DD").replace("%Y", "YYYY").replace("%y", "YY")


def parse_csv(text: str, mapping: CsvMapping) -> tuple[list[ParsedTxn], list[RowError]]:
    """Row numbers are file line numbers, so the header is row 1."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    reader.fieldnames = [(name or "").strip() for name in reader.fieldnames or []]
    wanted = [c for c in (mapping.date, mapping.description, mapping.amount, mapping.debit, mapping.credit) if c]
    missing = [c for c in wanted if c not in reader.fieldnames]
    if missing:
        return [], [RowError(1, f"column {missing[0]!r} was not found")]

    txns: list[ParsedTxn] = []
    errors: list[RowError] = []
    seen: dict[tuple, int] = {}
    for row in reader:
        n = reader.line_num
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        description = _cell(row, mapping.description)
        try:
            day = datetime.strptime(_cell(row, mapping.date), mapping.date_format).date()
        except ValueError:
            if any(str(v or "").strip().lower().startswith("total") for v in row.values() if isinstance(v, str)):
                continue
            errors.append(RowError(n, f"date must look like {_date_hint(mapping.date_format)}"))
            continue
        try:
            if mapping.amount:
                amount = _amount(_cell(row, mapping.amount))
            else:
                debit, credit = _cell(row, mapping.debit), _cell(row, mapping.credit)
                if not debit and not credit:
                    raise ValueError("enter a debit or a credit")
                amount = (_amount(credit) if credit else D("0")) - (_amount(debit) if debit else D("0"))
        except ValueError as exc:
            errors.append(RowError(n, str(exc)))
            continue
        key = (day, amount, description)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        txns.append(ParsedTxn(
            external_id=_external_id(day, amount, description, occurrence),
            date=day, description=description, amount=amount,
            check_number=_cell(row, mapping.check_number),
        ))
    return txns, errors


def _long_date(day: date_type) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def import_transactions(
    bank_account: BankAccount, txns: Sequence[ParsedTxn], *, source: TxnSource, user,
) -> ImportResult:
    """Save new rows. Rows that look like the same bank activity from the other source arrive EXCLUDED."""
    added = skipped = duplicates = 0
    other = TxnSource.FILE if source == TxnSource.PLAID else TxnSource.PLAID
    with transaction.atomic():
        existing_ids = set(
            BankTransaction.objects.filter(bank_account=bank_account).values_list("external_id", flat=True))
        for t in txns:
            if t.external_id in existing_ids:
                skipped += 1
                continue
            existing_ids.add(t.external_id)
            rows = BankTransaction.objects.filter(bank_account=bank_account, date=t.date, amount=t.amount)
            other_rows = list(rows.filter(source=other).order_by("date", "id")[:1])
            # Pair one-to-one: a second identical charge only counts as a duplicate if the other source has a second.
            is_duplicate = bool(other_rows) and rows.filter(source=source).count() < rows.filter(source=other).count()
            if is_duplicate:
                status = TxnStatus.EXCLUDED
                reason = f"Possible duplicate of a {other.label} transaction on {_long_date(other_rows[0].date)}."
                duplicates += 1
            else:
                status, reason = TxnStatus.NEW, ""
                added += 1
            BankTransaction.objects.create(
                bank_account=bank_account, external_id=t.external_id, date=t.date, description=t.description,
                amount=t.amount, check_number=t.check_number, source=source, status=status, exclude_reason=reason,
            )
    return ImportResult(added=added, skipped=skipped, possible_duplicates=duplicates)


def import_csv(bank_account: BankAccount, text: str, *, user) -> ImportResult:
    """Parse with the account's saved mapping; any bad row means nothing is saved."""
    txns, errors = parse_csv(text, CsvMapping(**bank_account.csv_mapping))
    if errors:
        raise CsvImportError(errors)
    return import_transactions(bank_account, txns, source=TxnSource.FILE, user=user)
