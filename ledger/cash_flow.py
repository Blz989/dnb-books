"""Statement of cash flows, indirect method (spec 4), read only from posted ledger lines.

Net income, then each balance sheet account's movement in the range, signed for cash: an asset going up used cash
(negative), a liability or equity account going up provided it (positive). Sections follow the account subtype.
Bank accounts are the cash; a move between two bank accounts is not a cash flow.

Depreciation (ASC 230): a fixed or other asset movement in an entry that also hits an expense account and touches
no bank account is a non-cash charge. It is added back in operating activities, not shown as investing.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q, Sum

from .balances import money
from .models import Account, AccountSubtype as S, AccountType, JournalLine
from .reports import profit_and_loss

ZERO = Decimal("0.00")
OPERATING = {S.ACCOUNTS_RECEIVABLE, S.INVENTORY, S.OTHER_CURRENT_ASSET, S.ACCOUNTS_PAYABLE, S.CREDIT_CARD,
             S.OTHER_CURRENT_LIABILITY}
INVESTING = {S.FIXED_ASSET, S.OTHER_ASSET}
FINANCING = {S.LONG_TERM_LIABILITY, S.EQUITY}


@dataclass
class CashFlowLine:
    account: Account
    amount: Decimal


@dataclass
class CashFlow:
    start: date
    end: date
    net_income: Decimal
    noncash: Decimal
    operating: list
    investing: list
    financing: list
    cash_start: Decimal
    cash_end: Decimal

    @staticmethod
    def _sum(lines) -> Decimal:
        return money(sum((l.amount for l in lines), ZERO))

    @property
    def total_operating(self) -> Decimal:
        return money(self.net_income + self.noncash + self._sum(self.operating))

    @property
    def total_investing(self) -> Decimal:
        return self._sum(self.investing)

    @property
    def total_financing(self) -> Decimal:
        return self._sum(self.financing)

    @property
    def net_change(self) -> Decimal:
        return money(self.total_operating + self.total_investing + self.total_financing)

    @property
    def bank_change(self) -> Decimal:
        return money(self.cash_end - self.cash_start)

    @property
    def difference(self) -> Decimal:
        return money(self.bank_change - self.net_change)

    @property
    def checks(self):
        return [("Net change in cash", self.net_change), ("Change in bank accounts", self.bank_change)]


def _bank_balance(through: date) -> Decimal:
    row = (JournalLine.objects.in_ledger().filter(account__subtype=S.BANK, entry__date__lte=through)
           .aggregate(d=Sum("debit"), c=Sum("credit")))
    return money(money(row["d"]) - money(row["c"]))


def _noncash_entries(lines) -> set[int]:
    """Entries in the range that touch a fixed or other asset and an expense account, and no bank account."""
    touches = defaultdict(set)
    for entry_id, subtype, type_ in lines.values_list("entry_id", "account__subtype", "account__type").distinct():
        touches[entry_id].add("asset" if subtype in INVESTING else "bank" if subtype == S.BANK
                              else "expense" if type_ == AccountType.EXPENSE else "other")
    return {e for e, kinds in touches.items() if "asset" in kinds and "expense" in kinds and "bank" not in kinds}


def cash_flow(start: date, end: date) -> CashFlow:
    lines = JournalLine.objects.in_ledger().filter(entry__date__gte=start, entry__date__lte=end)
    balance_sheet = lines.filter(account__type__in=[AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY])
    noncash_ids = _noncash_entries(balance_sheet.filter(Q(account__subtype__in=INVESTING) | Q(account__subtype=S.BANK))
                                   | lines.filter(account__type=AccountType.EXPENSE))

    noncash_q = Q(entry_id__in=noncash_ids, account__subtype__in=INVESTING)
    noncash_row = balance_sheet.filter(noncash_q).aggregate(d=Sum("debit"), c=Sum("credit"))
    noncash = money(money(noncash_row["c"]) - money(noncash_row["d"]))

    totals = (balance_sheet.exclude(account__subtype=S.BANK).exclude(noncash_q)
              .values("account_id").annotate(d=Sum("debit"), c=Sum("credit")))
    accounts = Account.objects.in_bulk([t["account_id"] for t in totals])
    operating, investing, financing = [], [], []
    for t in totals:
        account = accounts[t["account_id"]]
        amount = money(money(t["c"]) - money(t["d"]))  # cash effect: credit balance movement provides cash
        if amount == 0:
            continue
        line = CashFlowLine(account, amount)
        if account.subtype in OPERATING:
            operating.append(line)
        elif account.subtype in INVESTING:
            investing.append(line)
        elif account.subtype in FINANCING:
            financing.append(line)
    for section in (operating, investing, financing):
        section.sort(key=lambda l: l.account.number)

    return CashFlow(
        start=start, end=end, net_income=profit_and_loss(start, end).net_income, noncash=noncash,
        operating=operating, investing=investing, financing=financing,
        cash_start=_bank_balance(start - timedelta(days=1)), cash_end=_bank_balance(end))
