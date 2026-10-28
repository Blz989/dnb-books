"""Replays a Zoho export through DNB Books' own posting functions.

Master data (accounts, settings, bank accounts, items, contacts, opening stock) is built by
`Replay.load_master`. Documents are posted day by day by `Replay.post`, which hands each event to
the `post_<kind>` method of the same name. Orders, bills, and invoices are handled here; payments,
credits, deposits, entries, and stock adjustments in `replay_money.MoneyReplay`. Every object made gets a
`ZohoRecord` through `Replay.record`, so any Zoho row can be traced to what it became.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from banking.models import BankAccount, Reconciliation, ReconciliationItem, ReconStatus
from contacts.models import Customer, Vendor
from items.costing import unit_4dp
from items.models import Category, CostingMethod, InventorySettings, Item, ItemType
from items.stock import StockLine, on_hand, record_opening_batch
from handling.profit import order_profit
from ledger.models import Account, AccountSubtype, JournalLine
from migration.models import ZohoRecord
from migration.zoho import mapping
from migration.zoho.reader import ID_COLUMNS, ZohoExport
from migration.zoho.replay_money import OPENING_DATE, MoneyReplay, _quantity
from purchasing.models import Bill, PurchaseOrder, PurchasingSettings
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, create_bill
from purchasing.posting.orders import POLineSpec, close_po, create_po
from sales.balances import invoice_total
from sales.models import (
    CreditNote, DocType, Fulfillment, Invoice, OrderStatus, SalesOrder, SalesSettings,
)
from sales.numbering import set_sequence, use_number
from sales.posting.deposits import undeposited_payments
from sales.posting.invoices import InvoiceLineSpec, create_invoice, void_invoice
from sales.posting.orders import OrderLineSpec, close_order, create_order, remaining

RECONCILE_DATE = date(2025, 12, 31)
UNBILLED_FROM = date(2024, 3, 29)  # Zoho's handling bills start with the orders of this date
HANDLING_PAYEE = "Cordon"
# Zoho prefix -> the model whose numbers carry it
ZOHO_SEQUENCES = (
    (DocType.SO, "SO-", SalesOrder), (DocType.INV, "INV-", Invoice),
    (DocType.CN, "CN-", CreditNote), (DocType.PO, "PO-", PurchaseOrder),
)

# Non-stock items for the extra amounts on Zoho documents: sku, name, Zoho income account.
SERVICE_ITEMS = (
    ("SHIP", "Shipping charge", "Shipping Charge"),
    ("ADJ", "Adjustment", "Sales"),
    ("TAX", "Sales tax (Zoho)", "Tax Payable"),
)


def _handling(profit: Decimal, rate: str) -> Decimal:
    return max(Decimal("0.00"), (profit * Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class AfterSummary:
    unbilled_orders: list  # (SalesOrder, profit, at_50, at_55)
    undeposited_by_year: dict  # year -> (count, total)
    negative_items: list  # (Item, on_hand)


@dataclass(frozen=True)
class Event:
    date: date
    rank: int
    number: str
    kind: str
    zoho_id: str


# kind -> (short name, date column, number column or "" when the file has none)
EVENT_SOURCES = {
    "sales_order": ("sales_orders", "Order Date", "SalesOrder Number"),
    "purchase_order": ("purchase_orders", "Purchase Order Date", "Purchase Order Number"),
    "bill": ("bills", "Bill Date", "Bill Number"),
    "adjustment": ("adjustments", "Date", ""),
    "invoice": ("invoices", "Invoice Date", "Invoice Number"),
    "customer_payment": ("customer_payments", "Date", "Payment Number"),
    "vendor_payment": ("vendor_payments", "Date", "Payment Number"),
    "credit_note": ("credit_notes", "Credit Note Date", "Credit Note Number"),
    "credit_application": ("credit_applications", "Date", "Credit Note Number"),
    "vendor_credit": ("vendor_credits", "Vendor Credit Date", "Vendor Credit Number"),
    "expense": ("expenses", "Expense Date", ""),
    "journal": ("journals", "Journal Date", "Journal Number"),
    "transfer": ("transfers", "Transaction Date", "Reference"),
    "other_deposit": ("deposits", "Transaction Date", "Reference"),
}
# Spec 4.3: the order of posting within one day.
RANKS = {
    "sales_order": 1, "purchase_order": 1,
    "bill": 2,  # adjustments that add stock are also 2; those that remove it are 7
    "invoice": 3,
    "customer_payment": 4, "vendor_payment": 4, "credit_note": 4, "credit_application": 4,
    "vendor_credit": 4,
    "undeposited_transfer": 5,
    "expense": 6, "journal": 6, "transfer": 6, "other_deposit": 6,
}


class Replay(MoneyReplay):
    def __init__(self, export: ZohoExport, user):
        self.export = export
        self.user = user
        self.accounts: dict[str, Account] = {}
        self.items: dict[str, Item] = {}
        self.customers: dict[str, Customer] = {}
        self.vendors: dict[str, Vendor] = {}
        self.bank_accounts: dict[str, BankAccount] = {}
        self.warnings: list[str] = []
        self.orders: dict[str, SalesOrder] = {}  # Zoho sales order number -> order
        self.negative_items: set[str] = set()  # skus whose stock went below zero during replay
        self._doc_rows: dict[tuple[str, str], list[dict]] = {}
        self.deposit_pairs: dict[tuple[date, Decimal], list[str]] = {}  # from load_deposit_pairs

    # ---- helpers shared with the document tasks ----
    def rows(self, short: str) -> list[dict]:
        return self.export.files.get(short, [])

    def record(self, kind: str, zoho_id: str, obj) -> None:
        ZohoRecord.objects.create(
            kind=kind, zoho_id=zoho_id, content_type=ContentType.objects.get_for_model(obj),
            object_id=obj.pk,
        )

    def account(self, name: str) -> Account:
        try:
            return self.accounts[name]
        except KeyError:
            raise ValueError(f'The export has no account named "{name}".') from None

    def item_for(self, row: dict) -> Item:
        return self.items[row["SKU"].strip() or row["Item Name"]]

    def is_item_line(self, row: dict) -> bool:
        """Zoho lets a bill line carry a free-text name with an expense account (Cordon's "Shipping …" lines).
        Only a SKU, or a name that is a real item, makes it an item line."""
        if row["SKU"].strip():
            return True
        return bool(row["Item Name"].strip()) and row["Item Name"] in self.items

    def doc_rows(self, kind: str, zoho_id: str) -> list[dict]:
        """The export rows of one event (all lines of one document)."""
        short = EVENT_SOURCES["transfer" if kind == "undeposited_transfer" else kind][0]
        if not self._doc_rows:
            self._index_rows()
        return self._doc_rows[(short, zoho_id)]

    def _index_rows(self) -> None:
        for short, *_ in EVENT_SOURCES.values():
            id_column = ID_COLUMNS[short]
            for row in self.rows(short):
                key = row[id_column] if id_column else row["_id"]
                self._doc_rows.setdefault((short, key), []).append(row)

    # ---- events ----
    def events(self, kinds=None) -> list[Event]:
        """Every document in the export as one event, sorted by (date, rank, number) as spec 4.3 says."""
        if not self._doc_rows:
            self._index_rows()
        events = []
        for kind, (short, date_column, number_column) in EVENT_SOURCES.items():
            id_column = ID_COLUMNS[short]
            seen = set()
            for row in self.rows(short):
                zoho_id = row[id_column] if id_column else row["_id"]
                if zoho_id in seen:
                    continue
                seen.add(zoho_id)
                event_kind, rank = kind, RANKS.get(kind)
                if kind == "adjustment":
                    adds = _quantity(row["Quantity Adjusted"]) > 0 or mapping.money(row["Value Adjusted"]) > 0
                    rank = 2 if adds else 7
                elif kind == "transfer" and row["From Account"].strip() == mapping.UNDEPOSITED:
                    event_kind, rank = "undeposited_transfer", RANKS["undeposited_transfer"]
                if kinds is not None and event_kind not in kinds:
                    continue
                number = row[number_column].strip() if number_column else ""
                events.append(Event(
                    date.fromisoformat(row[date_column].strip()), rank, number or zoho_id, event_kind, zoho_id))
        return sorted(events, key=lambda e: (e.date, e.rank, e.number))

    def post(self, event) -> None:
        """Posts one event. Each kind is a `post_<kind>` method added by the document tasks."""
        handler = getattr(self, f"post_{event.kind}", None)
        if handler is None:
            raise NotImplementedError(f"Replaying {event.kind} is not built yet.")
        handler(event)

    # ---- master data ----
    def load_master(self) -> None:
        self._load_accounts()
        self._configure_settings()
        self._load_bank_accounts()
        self._load_items()
        self._load_contacts()
        self._post_opening_stock()

    def _load_accounts(self) -> None:
        rows = {r["Account Name"]: r for r in self.rows("accounts")}
        for plan in mapping.account_plan(list(rows.values())):
            row = rows[plan.name]
            account = Account.objects.create(
                number=plan.number, name=plan.name, type=plan.type, subtype=plan.subtype,
                is_active=plan.is_active, description=row.get("Description", ""),
            )
            self.accounts[plan.name] = account
            self.record("account", row["Account ID"], account)

    def _configure_settings(self) -> None:
        inv = InventorySettings.load()
        inv.default_inventory_account = self.account("Inventory Asset")
        inv.default_cogs_account = self.account("Cost of Goods Sold")
        inv.default_income_account = self.account("Sales")
        inv.shrinkage_account = inv.writedown_account = self.account("Cost of Goods Sold")
        inv.opening_equity_account = self.account("Opening Balance Offset")
        inv.save()
        sales = SalesSettings.load()
        sales.ar_account = self.account("Accounts Receivable")
        sales.undeposited_account = self.account(mapping.UNDEPOSITED)
        sales.returns_account = self.account("Sales")
        sales.save()
        purchasing = PurchasingSettings.load()
        purchasing.ap_account = self.account("Accounts Payable")
        purchasing.save()

    def _load_bank_accounts(self) -> None:
        for row in self.rows("accounts"):
            name = row["Account Name"]
            if row["Account Status"].strip().lower() == "inactive":
                continue
            kind = mapping.bank_kind(name, row["Account Type"].strip())
            if kind is None:
                continue
            bank = BankAccount.objects.create(ledger_account=self.accounts[name], kind=kind)
            self.bank_accounts[name] = bank
            self.record("bank_account", row["Account ID"], bank)

    def _item_account(self, row: dict, column: str, subtypes, default: Account) -> Account:
        name = row[column].strip()
        if not name:
            return default
        account = self.accounts.get(name)
        if account is None or account.subtype not in subtypes:
            self.warnings.append(
                f'Item "{row["Item Name"]}": account "{name}" cannot be used for {column}; '
                f"{default.name} was used instead."
            )
            return default
        return account

    def _load_items(self) -> None:
        defaults = InventorySettings.load()
        income, cogs, inventory = (
            defaults.default_income_account, defaults.default_cogs_account,
            defaults.default_inventory_account,
        )
        categories: dict[str, Category] = {}
        for row in self.rows("items"):
            stock = row["Item Type"].strip().lower() == "inventory"
            sku = row["SKU"].strip()
            if not sku:
                sku = f"ZOHO-{row['Item ID']}"
                self.warnings.append(f'Item "{row["Item Name"]}" has no SKU; {sku} was used.')
            category = None
            if row["Category Name"].strip():
                key = row["Category Name"].strip().lower()
                category = categories.get(key) or Category.objects.filter(name__iexact=key).first()
                if category is None:
                    category = Category.objects.create(name=row["Category Name"].strip())
                categories[key] = category
            item = Item.objects.create(
                sku=sku, name=row["Item Name"], description=row["Description"],
                item_type=ItemType.STOCK if stock else ItemType.NON_STOCK, category=category,
                unit=row["Usage unit"].strip() or "each",
                sale_price=mapping.money(row["Rate"]), purchase_cost=mapping.money(row["Purchase Rate"]),
                vendor_part_number=row["CF.Vendor SKU"].strip(),
                costing_method=CostingMethod.FIFO if stock else "",
                income_account=self._item_account(
                    row, "Account", (AccountSubtype.INCOME, AccountSubtype.OTHER_INCOME), income),
                cogs_account=self._item_account(
                    row, "Purchase Account", (AccountSubtype.COST_OF_GOODS_SOLD,), cogs) if stock else None,
                inventory_account=self._item_account(
                    row, "Inventory Account", (AccountSubtype.INVENTORY,), inventory) if stock else None,
                is_active=row["Status"].strip().lower() != "inactive",
            )
            self.items[sku if row["SKU"].strip() else row["Item Name"]] = item
            self.record("item", row["Item ID"], item)
        for sku, name, account_name in SERVICE_ITEMS:
            target = self.account(account_name)
            item = Item.objects.create(
                sku=sku, name=name, item_type=ItemType.NON_STOCK,
                income_account=target if target.subtype == AccountSubtype.INCOME else defaults.default_income_account,
            )
            if item.income_account_id != target.pk:
                # Zoho books sales tax to a liability account (Tax Payable). Item.clean only allows
                # income accounts, so the account is set below it; invoices then credit Tax Payable.
                Item.objects.filter(pk=item.pk).update(income_account=target)
                item.refresh_from_db()
            self.items[sku] = item

    def _load_contacts(self) -> None:
        for row in self.rows("customers"):
            name = row["Display Name"].strip()
            company = row["Company Name"].strip()
            customer = Customer.objects.create(
                name=name, contact_name=company if company != name else "",
                email=row["EmailID"].strip(), phone=row["Phone"].strip(),
                billing_address=mapping.address(row, "Billing"),
                ship_to_address=mapping.address(row, "Shipping"),
                terms=mapping.terms(row["Payment Terms Label"]), notes=row["Notes"],
                is_active=row["Status"].strip().lower() != "inactive",
            )
            self.customers[row["Display Name"]] = customer
            self.record("customer", row["Contact ID"], customer)
        for row in self.rows("vendors"):
            vendor = Vendor.objects.create(
                name=row["Display Name"].strip(), email=row["EmailID"].strip(),
                phone=row["Phone"].strip(), address=mapping.address(row, "Billing"),
                terms=mapping.terms(row["Payment Terms Label"]),
                is_active=row["Status"].strip().lower() != "inactive",
            )
            self.vendors[row["Display Name"]] = vendor
            self.record("vendor", row["Contact ID"], vendor)

    def _post_opening_stock(self) -> None:
        lines, owners, values = [], [], []
        for row in self.rows("items"):
            quantity = int(Decimal(row["Opening Stock"].replace(",", "") or "0"))
            if quantity <= 0:
                continue
            value = mapping.money(row["Opening Stock Value"])
            item = self.items[row["SKU"].strip() or row["Item Name"]]
            unit_cost = unit_4dp(value / quantity)
            lines.append(StockLine(item, quantity, reason="Opening stock from Zoho", unit_cost=unit_cost))
            owners.append(row["Item ID"])
            values.append(value)
        if not lines:
            return
        movements = record_opening_batch(date=OPENING_DATE, lines=lines, user=self.user)
        for zoho_id, movement, value in zip(owners, movements, values):
            self.record("opening_stock", zoho_id, movement)
            if movement.total_cost != value:
                # Nothing extra is posted; the tie-out expects Zoho's value, so the owner is told.
                self.warnings.append(
                    f"Opening stock {movement.item.sku}: value {movement.total_cost} vs Zoho {value}")

    # ---- documents (Task 5) ----
    def _note_negative(self, items, event: Event) -> None:
        """One warning per item, the first time its stock goes below zero."""
        for item in items:
            if item.is_stock and item.sku not in self.negative_items and on_hand(item) < 0:
                self.negative_items.add(item.sku)
                self.warnings.append(
                    f"{item.sku}: stock went below zero on {event.date.isoformat()} ({event.number}).")

    def post_sales_order(self, event: Event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        lines = []
        for row in rows:
            quantity = _quantity(row["QuantityOrdered"])
            price, _ = mapping.line_price(mapping.money(row["Item Total"]), quantity)
            lines.append(OrderLineSpec(self.item_for(row), quantity, price, description=row["Item Desc"]))
        void = first["Status"].strip().lower() == "void"
        notes = first["Notes"].strip()
        if void:
            # Orders have no void in DNB Books; a void Zoho order comes over closed (plan Task 5).
            notes = "\n".join(p for p in (notes, "Void in Zoho") if p)
        with transaction.atomic(), use_number(DocType.SO, event.number):
            order = create_order(
                customer=self.customers[first["Customer Name"]], date=event.date, lines=lines,
                user=self.user, notes=notes)
            if void:
                close_order(order, self.user)
            self.orders[event.number] = order
            self.record("sales_order", event.zoho_id, order)

    def post_purchase_order(self, event: Event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        lines = []
        for row in rows:
            quantity = _quantity(row["QuantityOrdered"])
            price, _ = mapping.line_price(mapping.money(row["Item Total"]), quantity)
            lines.append(POLineSpec(self.item_for(row), quantity, price))
        with transaction.atomic(), use_number(DocType.PO, event.number):
            po = create_po(
                vendor=self.vendors[rows[0]["Vendor Name"]], date=event.date, lines=lines, user=self.user,
                notes=f"Imported from Zoho (ID {event.zoho_id})")
            # Purchase orders come over closed, for reference; stock enters through bills only.
            close_po(po, self.user)
            self.record("purchase_order", event.zoho_id, po)

    def post_bill(self, event: Event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        item_lines, expense_lines = [], []
        leftover = Decimal("0.00")
        for row in rows:
            total = mapping.money(row["Item Total"])
            if self.is_item_line(row):
                quantity = _quantity(row["Quantity"])
                price, cents = mapping.line_price(total, quantity)
                leftover += cents
                item_lines.append(BillItemSpec(self.item_for(row), quantity, price, description=row["Description"]))
            else:
                description = row["Description"].strip() or row["Item Name"].strip()
                expense_lines.append(BillExpenseSpec(self.account(row["Account"]), total, description))
        # Zoho's own adjustment and every cent lost to 4-dp unit prices, so the bill totals Zoho's Total.
        extra = mapping.money(first["Adjustment"]) + leftover
        if extra:
            name = first["Adjustment Account"].strip() or "Cost of Goods Sold"
            expense_lines.append(BillExpenseSpec(self.account(name), extra, "Adjustment"))
        notes = f"Imported from Zoho (ID {event.zoho_id})"
        if first["CF.Sales Order"].strip():
            notes += f"\nSales order {first['CF.Sales Order'].strip()}"
        bill = create_bill(
            vendor=self.vendors[first["Vendor Name"]], date=event.date, vendor_invoice_number=event.number,
            item_lines=item_lines, expense_lines=expense_lines, user=self.user,
            terms=mapping.terms(first["Payment Terms Label"], first["Bill Date"], first["Due Date"]), notes=notes,
            allow_duplicate=True, allow_negative=True, allow_negative_adjustment=True)
        self.record("bill", event.zoho_id, bill)
        ours = sum((l.amount for l in [*bill.item_lines.all(), *bill.expense_lines.all()]), Decimal("0.00"))
        theirs = mapping.money(first["Total"])
        if ours != theirs:
            self.warnings.append(f"{event.number}: total {ours} vs Zoho {theirs}")
        self._note_negative([s.item for s in item_lines], event)

    def _service_item(self, base_sku: str, account_name: str) -> Item:
        """SHIP or ADJ, or a copy of it that posts to the Zoho account the document names (spec 4.2)."""
        default = self.items[base_sku]
        name = (account_name or "").strip()
        if not name or self.account(name).pk == default.income_account_id:
            return default
        key = f"{base_sku}:{name}"
        if key not in self.items:
            account = self.account(name)
            item = Item.objects.create(
                sku=f"{base_sku}-{account.number}", name=f"{default.name} ({name})",
                item_type=ItemType.NON_STOCK, income_account=account)
            self.items[key] = item
            self.record("service_item", key, item)
        return self.items[key]

    def _so_line(self, order: SalesOrder | None, item: Item, quantity: int, taken: dict) -> object:
        """The order's first line for this item with enough left to invoice, counting this invoice's lines."""
        if order is None:
            return None
        for line in order.lines.all():
            if line.item_id == item.pk and remaining(line) - taken[line.pk] >= quantity:
                taken[line.pk] += quantity
                return line
        return None

    def post_invoice(self, event: Event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        order = None
        so_number = first["Sales Order Number"].strip()
        if so_number:
            order = self.orders.get(so_number)
            if order is not None:
                order.refresh_from_db()
                if order.status == OrderStatus.CLOSED:
                    self.warnings.append(f"{event.number}: {so_number} is closed; the invoice is not linked to it.")
                    order = None
        lines, taken = [], defaultdict(int)
        leftover = tax = Decimal("0.00")
        for row in rows:
            item = self.item_for(row)
            quantity = _quantity(row["Quantity"])
            price, cents = mapping.line_price(mapping.money(row["Item Total"]), quantity)
            leftover += cents
            tax += mapping.money(row["Item Tax Amount"])
            so_line = self._so_line(order, item, quantity, taken)
            # Zoho tracked every one of these lines as stock, so fulfillment is STOCK.
            lines.append(InvoiceLineSpec(
                item, quantity, price, description=row["Item Desc"], fulfillment=Fulfillment.STOCK,
                so_line=so_line))
        shipping = mapping.money(first["Shipping Charge"])
        adjustment = mapping.money(first["Adjustment"]) + leftover
        extras = (
            (self._service_item("SHIP", first["Shipping Charge Account"]), shipping),
            (self._service_item("ADJ", first["Adjustment Account"]), adjustment),
            (self.items["TAX"], tax),
        )
        for item, amount in extras:
            if amount:
                lines.append(InvoiceLineSpec(item, 1, amount, description=item.name))
        with transaction.atomic(), use_number(DocType.INV, event.number):
            invoice = create_invoice(
                customer=self.customers[first["Customer Name"]], date=event.date, lines=lines,
                user=self.user, sales_order=order, terms=mapping.terms(first["Payment Terms Label"], first["Invoice Date"], first["Due Date"]),
                notes=first["Notes"], allow_negative=True, allow_negative_adjustment=True)
            self.record("invoice", event.zoho_id, invoice)
            if first["Invoice Status"].strip().lower() == "void":
                # Created, then voided on the same date: the number stays used and the ledger nets to zero.
                void_invoice(invoice, user=self.user, reason="Void in Zoho", date=event.date)
        ours, theirs = invoice_total(invoice), mapping.money(first["Total"])
        if ours != theirs:
            self.warnings.append(f"{event.number}: total {ours} vs Zoho {theirs}")
        self._note_negative([s.item for s in lines], event)

    # ---- after the replay (Task 7) ----
    def finish(self) -> AfterSummary:
        self.apply_vendor_credits()
        with transaction.atomic():
            self._open_reconciliations()
            self._set_sequences()
        return AfterSummary(
            unbilled_orders=self._unbilled_orders(),
            undeposited_by_year=self._undeposited_by_year(),
            negative_items=self._negative_items(),
        )

    def _open_reconciliations(self) -> None:
        """One open reconciliation per bank account, all ticked; the owner types in the Dec 31 statement balance."""
        for bank in self.bank_accounts.values():
            lines = list(
                JournalLine.objects.in_ledger()
                .filter(account=bank.ledger_account, entry__date__lte=RECONCILE_DATE))
            if not lines:
                continue
            rec = Reconciliation.objects.create(
                bank_account=bank, statement_date=RECONCILE_DATE, statement_balance=None,
                status=ReconStatus.IN_PROGRESS, started_by=self.user)
            ReconciliationItem.objects.bulk_create(
                [ReconciliationItem(reconciliation=rec, journal_line=line, cleared=True) for line in lines])

    def _set_sequences(self) -> None:
        """Next number continues after Zoho's highest. PMT, VP, VC, and BILL keep our own prefixes."""
        for doc_type, prefix, model in ZOHO_SEQUENCES:
            highest = 0
            for number in model.objects.filter(number__startswith=prefix).values_list("number", flat=True):
                tail = number[len(prefix):]
                if tail.isdigit():
                    highest = max(highest, int(tail))
            if highest:
                set_sequence(doc_type, prefix=prefix, next_number=highest + 1, user=self.user)

    def _unbilled_orders(self) -> list:
        """Sales orders with no Cordon bill, each with its profit and the handling it would earn at 50% and 55%."""
        billed = set()
        bills = Bill.objects.filter(vendor__name__icontains=HANDLING_PAYEE).prefetch_related("expense_lines")
        for bill in bills:
            for line in bill.notes.splitlines():
                if line.startswith("Sales order "):
                    billed.add(line[len("Sales order "):].strip())
            # The Zoho handling bill names its order in the expense line ("Shipping for Order SO-0006").
            for expense in bill.expense_lines.all():
                if "Order " in expense.description:
                    billed.add(expense.description.rsplit("Order ", 1)[1].strip())
        today = timezone.localdate()
        result = []
        for order in SalesOrder.objects.filter(date__gte=UNBILLED_FROM).exclude(notes__contains="Void in Zoho").order_by("date", "number"):
            if order.number in billed:
                continue
            profit = order_profit(order.number, as_of=today).profit
            result.append((order, profit, _handling(profit, "0.50"),
                           _handling(profit, "0.55")))
        return result

    def _undeposited_by_year(self) -> dict:
        years: dict[int, tuple[int, Decimal]] = {}
        for payment in undeposited_payments():
            count, total = years.get(payment.date.year, (0, Decimal("0.00")))
            years[payment.date.year] = (count + 1, total + payment.amount)
        return dict(sorted(years.items()))

    def _negative_items(self) -> list:
        found = []
        for item in Item.objects.filter(item_type=ItemType.STOCK).order_by("sku"):
            quantity = on_hand(item)
            if quantity < 0:
                found.append((item, quantity))
        return found
