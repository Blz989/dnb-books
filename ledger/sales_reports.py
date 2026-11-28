"""Sales and purchase reports (spec 2).

By-customer and by-vendor amounts come from posted ledger lines, grouped by the document behind each entry. By-item
reports read invoice and bill lines (the ledger posts income per account, not per item) and prove their total
against the ledger: a document counts on its date, and a voided one counts again, negative, on its void date,
which is how the ledger books a void (the original stays, the reversal posts on the void date).
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Q, Sum

from .balances import money
from .models import AccountType, JournalLine

ZERO = Decimal("0.00")
INVOICE, CREDIT_NOTE = "sales.invoice", "sales.credit_note"
BILL, VENDOR_CREDIT = "purchasing.bill", "purchasing.vendor_credit"


def _range(qs, start: date, end: date):
    return qs.filter(entry__date__gte=start, entry__date__lte=end)


def _average(amount: Decimal, quantity: int, places: str = "0.01") -> Decimal | None:
    if not quantity:
        return None
    return (amount / quantity).quantize(Decimal(places), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- sales by customer

@dataclass
class CustomerSalesRow:
    customer: object
    invoices: int = 0
    sales: Decimal = ZERO
    credits: Decimal = ZERO
    written_off: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return money(self.sales - self.credits)


@dataclass
class SalesByCustomer:
    start: date
    end: date
    rows: list
    total: CustomerSalesRow
    other_income: Decimal
    pnl_income: Decimal

    @property
    def checks(self):
        return [("Net sales", self.total.net), ("Income from other entries", self.other_income),
                ("Income on the profit and loss", self.pnl_income)]

    @property
    def difference(self) -> Decimal:
        return money(self.pnl_income - self.total.net - self.other_income)


def write_off_numbers() -> set[str]:
    """Credit note numbers that are bad debt write-offs (spec 5)."""
    from sales.models import CreditNote

    if not hasattr(CreditNote, "write_off_of"):
        return set()
    return set(CreditNote.objects.filter(write_off_of__isnull=False).values_list("number", flat=True))


def sales_by_customer(start: date, end: date) -> SalesByCustomer:
    from sales.models import CreditNote, DocStatus, Invoice

    ledger = _range(JournalLine.objects.in_ledger(), start, end)
    income = ledger.filter(account__type=AccountType.INCOME)
    grouped = (income.filter(entry__source_type__in=[INVOICE, CREDIT_NOTE])
               .values("entry__source_type", "entry__source_id").annotate(d=Sum("debit"), c=Sum("credit")))
    customer_of: dict[tuple[str, str], int] = {}
    numbers = defaultdict(set)
    for g in grouped:
        numbers[g["entry__source_type"]].add(g["entry__source_id"])
    for number, cid in Invoice.objects.filter(number__in=numbers[INVOICE]).values_list("number", "customer_id"):
        customer_of[(INVOICE, number)] = cid
    for number, cid in CreditNote.objects.filter(number__in=numbers[CREDIT_NOTE]).values_list("number", "customer_id"):
        customer_of[(CREDIT_NOTE, number)] = cid

    by_customer: dict[int, CustomerSalesRow] = {}

    def row(cid):
        if cid not in by_customer:
            by_customer[cid] = CustomerSalesRow(customer=None)
        return by_customer[cid]

    for g in grouped:
        key = (g["entry__source_type"], g["entry__source_id"])
        cid = customer_of.get(key)
        if cid is None:
            continue
        net_credit = money(g["c"]) - money(g["d"])
        if key[0] == INVOICE:
            row(cid).sales += net_credit
        else:
            row(cid).credits -= net_credit

    # Bad debt lines on write-off credit notes: shown, not part of net sales.
    offs = write_off_numbers()
    if offs:
        lines = (ledger.filter(entry__source_type=CREDIT_NOTE, entry__source_id__in=offs)
                 .exclude(account__type=AccountType.INCOME).exclude(account__subtype="ACCOUNTS_RECEIVABLE")
                 .values("entry__source_id").annotate(d=Sum("debit"), c=Sum("credit")))
        cids = dict(CreditNote.objects.filter(number__in=offs).values_list("number", "customer_id"))
        for g in lines:
            row(cids[g["entry__source_id"]]).written_off += money(g["d"]) - money(g["c"])

    counts = (Invoice.objects.filter(date__gte=start, date__lte=end).exclude(status=DocStatus.VOID)
              .values("customer_id").annotate(n=Count("id")))
    for c in counts:
        row(c["customer_id"]).invoices = c["n"]

    from contacts.models import Customer

    customers = Customer.objects.in_bulk(list(by_customer))
    rows = []
    for cid, r in by_customer.items():
        r.customer = customers[cid]
        r.sales, r.credits, r.written_off = money(r.sales), money(r.credits), money(r.written_off)
        if r.invoices or r.sales or r.credits or r.written_off:
            rows.append(r)
    rows.sort(key=lambda r: r.customer.name.lower())
    total = CustomerSalesRow(customer=None, invoices=sum(r.invoices for r in rows),
                             sales=money(sum((r.sales for r in rows), ZERO)),
                             credits=money(sum((r.credits for r in rows), ZERO)),
                             written_off=money(sum((r.written_off for r in rows), ZERO)))

    other = income.exclude(entry__source_type__in=[INVOICE, CREDIT_NOTE]).aggregate(d=Sum("debit"), c=Sum("credit"))
    other_income = money(money(other["c"]) - money(other["d"]))

    from .reports import profit_and_loss

    pnl = profit_and_loss(start, end)
    pnl_income = money(sum((r.amount for r in [*pnl.income, *pnl.other_income]), ZERO))
    return SalesByCustomer(start, end, rows, total, other_income, pnl_income)


# ---------------------------------------------------------------- purchases by vendor

def _ap_account_id():
    from purchasing.models import PurchasingSettings

    return PurchasingSettings.load().ap_account_id


def bill_ap_amounts(start: date, end: date) -> dict[tuple[str, str], Decimal]:
    """(source type, document number) -> A/P credits less debits in the range, for bills and vendor credits.

    A bill's stock lines credit A/P through stock movement entries (items.movement, and items.void when the bill is
    edited or voided), so those are traced back to the bill that made the movement.
    """
    from items.models import StockMovement

    ap = _ap_account_id()
    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    if ap is None:
        return out
    lines = (_range(JournalLine.objects.in_ledger(), start, end).filter(account_id=ap)
             .values("entry__source_type", "entry__source_id").annotate(d=Sum("debit"), c=Sum("credit")))
    movement_ids = set()
    pending = []
    for g in lines:
        st, sid = g["entry__source_type"], g["entry__source_id"]
        amount = money(g["c"]) - money(g["d"])
        if st in (BILL, VENDOR_CREDIT):
            out[(st, sid)] += amount
        elif st in ("items.movement", "items.void") and sid.isdigit():
            movement_ids.add(int(sid))
            pending.append((int(sid), amount))
    if pending:
        sources = {m.pk: (m.source_type, m.source_id)
                   for m in StockMovement.objects.filter(pk__in=movement_ids).only("source_type", "source_id")}
        for pk, amount in pending:
            st, sid = sources.get(pk, ("", ""))
            if st in (BILL, VENDOR_CREDIT):
                out[(st, sid)] += amount
    return out


@dataclass
class VendorPurchasesRow:
    vendor: object
    bills: int = 0
    purchases: Decimal = ZERO
    credits: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return money(self.purchases - self.credits)


@dataclass
class PurchasesByVendor:
    start: date
    end: date
    rows: list
    total: VendorPurchasesRow
    ap_total: Decimal

    @property
    def checks(self):
        return [("Net purchases", self.total.net), ("Bills and credits in A/P", self.ap_total)]

    @property
    def difference(self) -> Decimal:
        return money(self.ap_total - self.total.net)


def purchases_by_vendor(start: date, end: date) -> PurchasesByVendor:
    from contacts.models import Vendor
    from purchasing.models import Bill, DocStatus, VendorCredit

    amounts = bill_ap_amounts(start, end)
    vendor_of = {}
    bill_numbers = {n for (st, n) in amounts if st == BILL}
    credit_numbers = {n for (st, n) in amounts if st == VENDOR_CREDIT}
    vendor_of.update({(BILL, n): v for n, v in Bill.objects.filter(number__in=bill_numbers).values_list("number", "vendor_id")})
    vendor_of.update({(VENDOR_CREDIT, n): v for n, v in
                      VendorCredit.objects.filter(number__in=credit_numbers).values_list("number", "vendor_id")})
    rows: dict[int, VendorPurchasesRow] = {}
    for key, amount in amounts.items():
        vid = vendor_of.get(key)
        if vid is None:
            continue
        r = rows.setdefault(vid, VendorPurchasesRow(vendor=None))
        if key[0] == BILL:
            r.purchases += amount
        else:
            r.credits -= amount
    for c in (Bill.objects.filter(date__gte=start, date__lte=end).exclude(status=DocStatus.VOID)
              .values("vendor_id").annotate(n=Count("id"))):
        rows.setdefault(c["vendor_id"], VendorPurchasesRow(vendor=None)).bills = c["n"]
    vendors = Vendor.objects.in_bulk(list(rows))
    out = []
    for vid, r in rows.items():
        r.vendor = vendors[vid]
        r.purchases, r.credits = money(r.purchases), money(r.credits)
        if r.bills or r.purchases or r.credits:
            out.append(r)
    out.sort(key=lambda r: r.vendor.name.lower())
    total = VendorPurchasesRow(vendor=None, bills=sum(r.bills for r in out),
                               purchases=money(sum((r.purchases for r in out), ZERO)),
                               credits=money(sum((r.credits for r in out), ZERO)))
    ap_total = money(sum(amounts.values(), ZERO))
    return PurchasesByVendor(start, end, out, total, ap_total)


# ---------------------------------------------------------------- sales by item

@dataclass
class ItemSalesRow:
    item: object
    quantity: int = 0
    amount: Decimal = ZERO

    @property
    def average(self) -> Decimal | None:
        return _average(self.amount, self.quantity)


@dataclass
class SalesByItem:
    start: date
    end: date
    rows: list
    total: Decimal
    ledger_total: Decimal

    @property
    def checks(self):
        return [("Report total", self.total), ("Invoice total in the ledger", self.ledger_total)]

    @property
    def difference(self) -> Decimal:
        return money(self.ledger_total - self.total)


def _counted(model, start, end, extra: Q = Q()):
    """Documents whose lines count in the range: dated in it (any status), or voided in it (counted negative)."""
    dated = model.objects.filter(extra, date__gte=start, date__lte=end)
    voided = model.objects.filter(extra, voided_on__gte=start, voided_on__lte=end, status="VOID")
    return dated, voided


def sales_by_item(start: date, end: date, customer=None) -> SalesByItem:
    from items.models import Item
    from sales.models import Invoice, InvoiceLine
    from sales.models import SalesSettings

    extra = Q(customer=customer) if customer is not None else Q()
    dated, voided = _counted(Invoice, start, end, extra)
    rows: dict[int, ItemSalesRow] = {}
    for sign, docs in ((1, dated), (-1, voided)):
        for g in (InvoiceLine.objects.filter(invoice__in=docs).values("item_id")
                  .annotate(q=Sum("quantity"), a=Sum("amount"))):
            r = rows.setdefault(g["item_id"], ItemSalesRow(item=None))
            r.quantity += sign * (g["q"] or 0)
            r.amount += sign * money(g["a"])
    items = Item.objects.in_bulk(list(rows))
    out = []
    for iid, r in rows.items():
        r.item, r.amount = items[iid], money(r.amount)
        if r.quantity or r.amount:
            out.append(r)
    out.sort(key=lambda r: r.item.sku.lower())
    total = money(sum((r.amount for r in out), ZERO))

    ar = SalesSettings.load().ar_account_id
    ledger = _range(JournalLine.objects.in_ledger(), start, end).filter(account_id=ar, entry__source_type=INVOICE)
    if customer is not None:
        ledger = ledger.filter(entry__source_id__in=Invoice.objects.filter(customer=customer).values("number"))
    agg = ledger.aggregate(d=Sum("debit"), c=Sum("credit"))
    return SalesByItem(start, end, out, total, money(money(agg["d"]) - money(agg["c"])))


# ---------------------------------------------------------------- purchases by item

@dataclass
class ItemPurchaseRow:
    item: object = None
    account: object = None
    quantity: int | None = None
    amount: Decimal = ZERO

    @property
    def average(self) -> Decimal | None:
        return _average(self.amount, self.quantity, "0.0001") if self.quantity else None


@dataclass
class PurchasesByItem:
    start: date
    end: date
    rows: list
    total: Decimal
    ledger_total: Decimal

    @property
    def checks(self):
        return [("Report total", self.total), ("Bill total in the ledger", self.ledger_total)]

    @property
    def difference(self) -> Decimal:
        return money(self.ledger_total - self.total)


def purchases_by_item(start: date, end: date, vendor=None) -> PurchasesByItem:
    from items.models import Item
    from ledger.models import Account
    from purchasing.models import Bill, BillExpenseLine, BillItemLine

    extra = Q(vendor=vendor) if vendor is not None else Q()
    dated, voided = _counted(Bill, start, end, extra)
    by_item: dict[int, ItemPurchaseRow] = {}
    by_account: dict[int, ItemPurchaseRow] = {}
    for sign, docs in ((1, dated), (-1, voided)):
        for g in (BillItemLine.objects.filter(bill__in=docs).values("item_id")
                  .annotate(q=Sum("quantity"), a=Sum("amount"))):
            r = by_item.setdefault(g["item_id"], ItemPurchaseRow(quantity=0))
            r.quantity += sign * (g["q"] or 0)
            r.amount += sign * money(g["a"])
        for g in BillExpenseLine.objects.filter(bill__in=docs).values("account_id").annotate(a=Sum("amount")):
            r = by_account.setdefault(g["account_id"], ItemPurchaseRow())
            r.amount += sign * money(g["a"])
    items, accounts = Item.objects.in_bulk(list(by_item)), Account.objects.in_bulk(list(by_account))
    item_rows = []
    for iid, r in by_item.items():
        r.item, r.amount = items[iid], money(r.amount)
        if r.quantity or r.amount:
            item_rows.append(r)
    account_rows = []
    for aid, r in by_account.items():
        r.account, r.amount = accounts[aid], money(r.amount)
        if r.amount:
            account_rows.append(r)
    item_rows.sort(key=lambda r: r.item.sku.lower())
    account_rows.sort(key=lambda r: r.account.number)
    rows = item_rows + account_rows
    total = money(sum((r.amount for r in rows), ZERO))

    amounts = bill_ap_amounts(start, end)
    numbers = None
    if vendor is not None:
        numbers = set(Bill.objects.filter(vendor=vendor).values_list("number", flat=True))
    ledger_total = money(sum((a for (st, n), a in amounts.items() if st == BILL and (numbers is None or n in numbers)),
                             ZERO))
    return PurchasesByItem(start, end, rows, total, ledger_total)
