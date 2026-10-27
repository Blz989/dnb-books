from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from banking.importers import (
    CsvImportError, CsvMapping, ParsedTxn, import_csv, import_transactions, parse_csv, parse_ofx,
)
from banking.models import BankTransaction, TxnSource, TxnStatus

D = Decimal
FIX = Path(__file__).parent / "fixtures" / "bank"
SIMPLE = CsvMapping(date="Date", description="Description", amount="Amount")


def test_parse_ofx_checking():
    txns = parse_ofx((FIX / "checking.ofx").read_bytes())
    assert txns == [
        ParsedTxn("C001", date(2026, 1, 3), "ACH DEPOSIT ACME BUILDERS", D("1500.00")),
        ParsedTxn("C002", date(2026, 1, 5), "CHECK 2050", D("-420.00"), check_number="2050"),
        ParsedTxn("C003", date(2026, 1, 10), "MONTHLY SERVICE FEE", D("-15.00")),
    ]


def test_parse_qfx_card_purchases_are_negative():
    txns = parse_ofx((FIX / "card.qfx").read_bytes())
    assert [(t.external_id, t.date, t.description, t.amount) for t in txns] == [
        ("F001", date(2026, 1, 4), "OFFICE DEPOT", D("-86.40")),
        ("F002", date(2026, 1, 12), "FUEL STOP", D("-230.00")),
        ("F003", date(2026, 1, 20), "PAYMENT THANK YOU", D("500.00")),
    ]


def test_parse_ofx_garbage_gives_message():
    with pytest.raises(ValueError, match="That file isn't a readable OFX or QFX statement."):
        parse_ofx(b"this is not a bank file")


def test_parse_csv_single_amount_column():
    txns, errors = parse_csv((FIX / "checking.csv").read_text(), SIMPLE)
    assert errors == []
    assert [(t.date, t.description, t.amount) for t in txns] == [
        (date(2026, 1, 3), "ACH DEPOSIT ACME BUILDERS", D("1500.00")),
        (date(2026, 1, 5), "CHECK 2050", D("-420.00")),
        (date(2026, 1, 10), "MONTHLY SERVICE FEE", D("-15.00")),
    ]


def test_parse_csv_debit_and_credit_columns():
    mapping = CsvMapping(date="Posting Date", description="Payee", debit="Debit", credit="Credit",
                         check_number="Check #")
    txns, errors = parse_csv((FIX / "debit_credit.csv").read_text(), mapping)
    assert errors == []
    assert [(t.amount, t.check_number) for t in txns] == [
        (D("1500.00"), ""), (D("-420.00"), "2050"), (D("-15.00"), "")]


def test_messy_csv_with_bom_symbols_parentheses_and_total_row():
    text = (FIX / "messy.csv").read_bytes().decode("utf-8")
    assert text.startswith("﻿")
    txns, errors = parse_csv(text, SIMPLE)
    assert errors == []
    assert [t.amount for t in txns] == [D("1500.00"), D("-420.00"), D("-15.00"), D("-4.50"), D("-4.50")]
    assert len({t.external_id for t in txns}) == 5  # the two identical coffees stay distinct


def test_bad_amount_reports_row_and_saves_nothing(checking, admin_user):
    text = "Date,Description,Amount\n" + "\n".join(
        f"01/0{n}/2026,Row {n},{n}.00" for n in range(1, 6)) + "\n01/06/2026,Bad row,twelve\n01/07/2026,After,1.00\n"
    _, errors = parse_csv(text, SIMPLE)
    assert [str(e) for e in errors] == ["Row 7: amount must be a number."]
    checking.csv_mapping = {"date": "Date", "description": "Description", "amount": "Amount"}
    with pytest.raises(CsvImportError) as exc:
        import_csv(checking, text, user=admin_user)
    assert [str(e) for e in exc.value.errors] == ["Row 7: amount must be a number."]
    assert BankTransaction.objects.count() == 0


def test_bad_date_reports_row():
    text = "Date,Description,Amount\n13/45/2026,Oops,1.00\n"
    _, errors = parse_csv(text, SIMPLE)
    assert [str(e) for e in errors] == ["Row 2: date must look like MM/DD/YYYY."]


def test_three_decimal_places_rejected():
    _, errors = parse_csv("Date,Description,Amount\n01/01/2026,x,1.005\n", SIMPLE)
    assert [str(e) for e in errors] == ["Row 2: amount can have at most 2 decimal places."]


def test_import_twice_skips_everything(checking, admin_user):
    txns = parse_ofx((FIX / "checking.ofx").read_bytes())
    first = import_transactions(checking, txns, source=TxnSource.FILE, user=admin_user)
    assert (first.added, first.skipped, first.possible_duplicates) == (3, 0, 0)
    second = import_transactions(checking, txns, source=TxnSource.FILE, user=admin_user)
    assert (second.added, second.skipped, second.possible_duplicates) == (0, 3, 0)
    assert BankTransaction.objects.count() == 3
    t = BankTransaction.objects.get(external_id="C002")
    assert (t.check_number, t.source, t.status) == ("2050", TxnSource.FILE, TxnStatus.NEW)


def test_two_identical_same_day_charges_both_import(checking, admin_user):
    text = (FIX / "messy.csv").read_bytes().decode("utf-8")
    txns, _ = parse_csv(text, SIMPLE)
    result = import_transactions(checking, txns, source=TxnSource.FILE, user=admin_user)
    assert result.added == 5
    assert BankTransaction.objects.filter(description="COFFEE SHOP").count() == 2


def test_reimporting_csv_with_identical_rows_skips_all(checking, admin_user):
    text = (FIX / "messy.csv").read_bytes().decode("utf-8")
    txns, _ = parse_csv(text, SIMPLE)
    import_transactions(checking, txns, source=TxnSource.FILE, user=admin_user)
    again = import_transactions(checking, txns, source=TxnSource.FILE, user=admin_user)
    assert (again.added, again.skipped) == (0, 5)


def test_file_row_overlapping_plaid_is_excluded_with_reason(checking, admin_user):
    BankTransaction.objects.create(
        bank_account=checking, external_id="plaid-1", date=date(2026, 1, 10), description="Service fee",
        amount=D("-15.00"), source=TxnSource.PLAID)
    txns = parse_ofx((FIX / "checking.ofx").read_bytes())
    result = import_transactions(checking, txns, source=TxnSource.FILE, user=admin_user)
    assert (result.added, result.possible_duplicates) == (2, 1)
    dup = BankTransaction.objects.get(external_id="C003")
    assert dup.status == TxnStatus.EXCLUDED
    assert dup.exclude_reason == "Possible duplicate of a Plaid transaction on Jan 10, 2026."


def test_plaid_row_overlapping_file_is_excluded_with_reason(checking, admin_user):
    import_transactions(checking, parse_ofx((FIX / "checking.ofx").read_bytes()), source=TxnSource.FILE,
                        user=admin_user)
    result = import_transactions(
        checking, [ParsedTxn("plaid-9", date(2026, 1, 5), "Check", D("-420.00"))],
        source=TxnSource.PLAID, user=admin_user)
    assert result.possible_duplicates == 1
    dup = BankTransaction.objects.get(external_id="plaid-9")
    assert dup.status == TxnStatus.EXCLUDED
    assert dup.exclude_reason == "Possible duplicate of a file transaction on Jan 5, 2026."


def test_one_plaid_row_excludes_only_one_of_two_file_rows(checking, admin_user):
    BankTransaction.objects.create(
        bank_account=checking, external_id="plaid-1", date=date(2026, 1, 12), description="Coffee",
        amount=D("-4.50"), source=TxnSource.PLAID)
    txns, _ = parse_csv((FIX / "messy.csv").read_bytes().decode("utf-8"), SIMPLE)
    result = import_transactions(checking, txns, source=TxnSource.FILE, user=admin_user)
    assert (result.added, result.possible_duplicates) == (4, 1)


def test_same_date_and_amount_on_another_account_is_not_a_duplicate(checking, card_account, admin_user):
    BankTransaction.objects.create(
        bank_account=card_account, external_id="p", date=date(2026, 1, 10), description="x",
        amount=D("-15.00"), source=TxnSource.PLAID)
    result = import_transactions(checking, parse_ofx((FIX / "checking.ofx").read_bytes()),
                                 source=TxnSource.FILE, user=admin_user)
    assert result.possible_duplicates == 0


def test_card_file_is_read_as_exported(card_account, admin_user):
    import_transactions(card_account, parse_ofx((FIX / "card.qfx").read_bytes()), source=TxnSource.FILE,
                        user=admin_user)
    assert BankTransaction.objects.get(external_id="F001").amount == D("-86.40")
    assert BankTransaction.objects.get(external_id="F003").amount == D("500.00")
