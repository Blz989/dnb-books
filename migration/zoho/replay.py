"""Replays a Zoho export through DNB Books' own posting functions.

Master data (accounts, settings, bank accounts, items, contacts, opening stock) is built by
`Replay.load_master`. Documents are posted day by day by `Replay.post`, which hands each event to
the `post_<kind>` method of the same name (added by later tasks). Every object made gets a
`ZohoRecord` through `Replay.record`, so any Zoho row can be traced to what it became.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from banking.models import BankAccount
from contacts.models import Customer, Vendor
from items.models import Category, CostingMethod, InventorySettings, Item, ItemType
from items.stock import StockLine, record_opening_batch
from ledger.models import Account, AccountSubtype
from migration.models import ZohoRecord
from migration.zoho import mapping
from migration.zoho.reader import ZohoExport
from purchasing.models import PurchasingSettings
from sales.models import SalesSettings

OPENING_DATE = date(2020, 2, 1)
FEED_START = date(2026, 1, 1)  # becomes BankAccount.feed_starts in Task 10; the field does not exist yet
UNIT_STEP = Decimal("0.0001")

# Non-stock items for the extra amounts on Zoho documents: sku, name, Zoho income account.
SERVICE_ITEMS = (
    ("SHIP", "Shipping charge", "Shipping Charge"),
    ("ADJ", "Adjustment", "Sales"),
    ("TAX", "Sales tax (Zoho)", "Tax Payable"),
)


class Replay:
    def __init__(self, export: ZohoExport, user):
        self.export = export
        self.user = user
        self.accounts: dict[str, Account] = {}
        self.items: dict[str, Item] = {}
        self.customers: dict[str, Customer] = {}
        self.vendors: dict[str, Vendor] = {}
        self.bank_accounts: dict[str, BankAccount] = {}
        self.warnings: list[str] = []

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
            # feed_starts=FEED_START joins this call in Task 10, when the field exists.
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
        lines, owners = [], []
        for row in self.rows("items"):
            quantity = int(Decimal(row["Opening Stock"].replace(",", "") or "0"))
            if quantity <= 0:
                continue
            value = mapping.money(row["Opening Stock Value"])
            item = self.items[row["SKU"].strip() or row["Item Name"]]
            unit_cost = (value / quantity).quantize(UNIT_STEP)
            lines.append(StockLine(item, quantity, reason="Opening stock from Zoho", unit_cost=unit_cost))
            owners.append(row["Item ID"])
        if not lines:
            return
        movements = record_opening_batch(date=OPENING_DATE, lines=lines, user=self.user)
        for zoho_id, movement in zip(owners, movements):
            self.record("opening_stock", zoho_id, movement)
