"""Demo banking data for seed_demo: bank, savings, card, and loan accounts, statement rows that line up with the
demo documents, rules, and two vendor checks waiting to be printed."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from contacts.models import Vendor
from ledger.models import Account, AccountSubtype, AccountType
from ledger.posting import LineSpec, post_entry
from purchasing.models import VendorPayment, VendorPaymentMethod as M
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import PayLine, pay_bills
from sales.models import Deposit, DocStatus, Payment, PaymentMethod

from .importers import ParsedTxn, import_transactions
from .models import AccountKind, BankAccount, PatternType, Rule, TxnSource

D = Decimal


def _account(number, name, type_, subtype):
    return Account.objects.get_or_create(number=number, defaults={"name": name, "type": type_, "subtype": subtype})[0]


def seed_banking(user) -> None:
    today = timezone.localdate()
    by_number = {a.number: a for a in Account.objects.all()}
    bank, card = by_number["1000"], by_number["2100"]
    savings_ledger = _account("1010", "Savings", AccountType.ASSET, AccountSubtype.BANK)
    loan_ledger = _account("2500", "Equipment loan", AccountType.LIABILITY, AccountSubtype.LONG_TERM_LIABILITY)
    interest_exp = _account("6800", "Interest expense", AccountType.EXPENSE, AccountSubtype.EXPENSE)
    fees = _account("6900", "Bank fees", AccountType.EXPENSE, AccountSubtype.EXPENSE)
    interest_inc = _account("7000", "Interest income", AccountType.INCOME, AccountSubtype.OTHER_INCOME)

    checking = BankAccount.objects.create(ledger_account=bank, kind=AccountKind.CHECKING, mask="4821",
                                          next_check_number=3302)
    savings = BankAccount.objects.create(ledger_account=savings_ledger, kind=AccountKind.SAVINGS, mask="7730")
    card_account = BankAccount.objects.create(ledger_account=card, kind=AccountKind.CARD, mask="9876")
    loan = BankAccount.objects.create(ledger_account=loan_ledger, kind=AccountKind.LOAN, mask="5502")

    Rule.objects.create(position=1, pattern="SERVICE FEE", pattern_type=PatternType.CONTAINS, account=fees,
                        memo="Monthly bank fee")
    Rule.objects.create(position=2, pattern="INTEREST PAID", pattern_type=PatternType.STARTS_WITH,
                        account=interest_inc, bank_account=savings, memo="Savings interest")

    # The owner moved 2,000 to savings and made a card payment and a loan payment; savings had 20,000 in it.
    post_entry(date=today - timedelta(days=80), user=user, memo="Opening balance for savings", lines=[
        LineSpec(savings_ledger, debit=D("20000.00")), LineSpec(by_number["3900"], credit=D("20000.00"))])
    post_entry(date=today - timedelta(days=75), user=user, memo="Equipment loan proceeds", lines=[
        LineSpec(bank, debit=D("12000.00")), LineSpec(loan_ledger, credit=D("12000.00"))])
    post_entry(date=today - timedelta(days=50), user=user, memo="Card charge, office supplies", lines=[
        LineSpec(by_number["6000"], debit=D("182.37")), LineSpec(card, credit=D("182.37"))])

    def day(n):
        return today - timedelta(days=n)

    def row(key, n, description, amount, check=""):
        return ParsedTxn(f"demo-{key}", day(n), description, D(amount), check)

    checking_rows = [row("fee", 12, "MONTHLY SERVICE FEE", "-15.00"),
                     row("xfer", 9, "ONLINE TRANSFER TO SAV 7730", "-2000.00"),
                     row("loan", 8, "LOAN PAYMENT 5502", "-400.00"),
                     row("visa", 7, "CARD PAYMENT THANK YOU", "-500.00"),
                     row("mystery", 5, "POS DEBIT HARDWARE HANK 4412", "-63.18")]
    for p in Payment.objects.filter(status=DocStatus.OPEN, method__in=(PaymentMethod.ACH, PaymentMethod.WIRE)):
        checking_rows.append(ParsedTxn(f"demo-pmt-{p.pk}", p.date, f"ACH CREDIT {p.customer.name.upper()}", p.amount))
    for d in Deposit.objects.filter(status=DocStatus.OPEN):
        total = d.journal_entry.lines.get(account=bank).debit
        checking_rows.append(ParsedTxn(f"demo-dep-{d.pk}", d.date + timedelta(days=1), "DEPOSIT", total))
    for v in VendorPayment.objects.filter(status=DocStatus.OPEN, paid_from=bank):
        label = f"CHECK {v.check_number}" if v.check_number else f"ACH DEBIT {v.vendor.name.upper()}"
        checking_rows.append(ParsedTxn(f"demo-vp-{v.pk}", v.date, label, -v.amount, v.check_number))
    import_transactions(checking, checking_rows, source=TxnSource.FILE, user=user)

    import_transactions(savings, [
        row("s-in", 9, "ONLINE TRANSFER FROM CHK 4821", "2000.00"),
        row("s-int", 4, "INTEREST PAID", "21.40")], source=TxnSource.FILE, user=user)
    import_transactions(card_account, [
        row("c-pay", 6, "PAYMENT THANK YOU", "500.00"),
        row("c-fuel", 10, "SHELL OIL 57444", "-61.20"),
        row("c-office", 14, "OFFICE DEPOT #1182", "-86.40")] + [
        ParsedTxn(f"demo-card-{v.pk}", v.date, v.vendor.name.upper(), -v.amount)
        for v in VendorPayment.objects.filter(status=DocStatus.OPEN, paid_from=card)],
        source=TxnSource.FILE, user=user)
    import_transactions(loan, [row("l-pay", 8, "PAYMENT RECEIVED", "400.00")], source=TxnSource.FILE, user=user)

    # Two vendor checks waiting to be printed on the stock.
    for name, invoice, amount in (("Delta Faucet Supply", "DF-7721", "812.60"), ("Midwest Fastener Co", "MF-20977", "264.15")):
        vendor = Vendor.objects.get(name=name)
        bill = create_bill(vendor=vendor, date=day(5), vendor_invoice_number=invoice, user=user,
                           expense_lines=[BillExpenseSpec(by_number["6150"], D(amount), "Freight and handling")])
        pay_bills(date=day(1), method=M.CHECK, paid_from=bank, user=user, lines=[PayLine(bill, D(amount))])
