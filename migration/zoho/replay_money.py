"""Replay handlers for money movements: payments, credits, deposits, expenses, journals, transfers,
stock adjustments, and the owner's opening balances and deposit pairs CSVs.

`MoneyReplay` is a mixin of `migration.zoho.replay.Replay`; it uses the helpers defined there
(doc_rows, record, account, item_for, customers, vendors, warnings, _note_negative).
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from items.models import MovementKind
from items.stock import StockLine, record_movements, record_value_adjustment
from ledger.posting import LineSpec, post_entry
from migration.models import ZohoRecord
from migration.zoho import mapping
from purchasing.balances import bill_balance, vendor_credit_unapplied
from purchasing.models import Bill, VendorCredit, VendorPaymentMethod
from purchasing.posting.credits import (
    AllowanceLineSpec,
    ReturnLineSpec,
    apply_vendor_credit,
    create_allowance_credit,
    create_return_credit,
)
from purchasing.posting.payments import apply_vendor_payment, pay_vendor
from sales.models import CreditNote, DocType, Invoice, Payment
from sales.numbering import use_number
from sales.posting.deposits import make_deposit, undeposited_payments
from sales.posting.receipts import (
    CreditLineSpec,
    apply_credit,
    apply_payment,
    create_credit_note,
    receive_payment,
)

OPENING_DATE = date(2020, 2, 1)
MAX_CANDIDATES = 20
ZERO = Decimal("0.00")


def _credit_reason(row: dict) -> str:
    """Zoho's credit note export has no reason column; Subject, then Notes, says why."""
    return (row["Subject"].strip() or row["Notes"].strip())[:200]


def _quantity(text: str) -> int:
    return int(Decimal((text or "0").replace(",", "").strip() or "0"))


def _date(text: str, default: date) -> date:
    text = (text or "").strip()
    return date.fromisoformat(text) if text else default


def exact_subset(amounts: list[Decimal], target: Decimal) -> list[int] | None:
    """Indices of the first subset of `amounts` that adds up to exactly `target`, or None.

    Depth-first in the given order (oldest first), so older payments are preferred. A branch stops
    when what is left is smaller than the smallest remaining amount, or larger than all of them together.
    """
    n = len(amounts)
    suffix_sum = [ZERO] * (n + 1)
    suffix_min = [None] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_sum[i] = suffix_sum[i + 1] + amounts[i]
        suffix_min[i] = amounts[i] if suffix_min[i + 1] is None else min(amounts[i], suffix_min[i + 1])
    chosen: list[int] = []

    def search(i: int, left: Decimal) -> bool:
        if left == 0:
            return True
        if i == n or left < suffix_min[i] or left > suffix_sum[i]:
            return False
        if amounts[i] <= left:
            chosen.append(i)
            if search(i + 1, left - amounts[i]):
                return True
            chosen.pop()
        return search(i + 1, left)

    return list(chosen) if target > 0 and search(0, target) else None


class MoneyReplay:
    deposit_pairs: dict[tuple[date, Decimal], list[str]]

    # ---- lookups ----
    def _invoice(self, number: str) -> Invoice:
        return Invoice.objects.get(number=number.strip())

    def _bill(self, vendor, number: str, amount: Decimal) -> Bill:
        """The imported bill with this vendor and Zoho Bill Number; with duplicates, the first still owing enough."""
        imported = ZohoRecord.objects.filter(
            kind="bill", content_type=ContentType.objects.get_for_model(Bill)).values("object_id")
        bills = list(Bill.objects.filter(
            pk__in=imported, vendor=vendor, vendor_invoice_number=number.strip()).order_by("date", "id"))
        if not bills:
            raise ValueError(f'Bill "{number}" from {vendor.name} was not imported.')
        return next((b for b in bills if bill_balance(b) >= amount), bills[0])

    # ---- customer side ----
    def post_customer_payment(self, event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        deposit_to = first["Deposit To"].strip()
        mode, ref = first["Mode"].strip(), first["Reference Number"].strip()
        to_bank = deposit_to != mapping.UNDEPOSITED
        with transaction.atomic():
            with use_number(DocType.PMT, event.number):
                payment = receive_payment(
                    customer=self.customer_for("customer_payments", first), date=event.date,
                    method=mapping.customer_method(mode, deposit_to), amount=mapping.money(first["Amount"]),
                    user=self.user,
                    # Straight to the bank it becomes ACH, so Zoho's Mode is kept in the reference (spec 4.2).
                    reference=" ".join(p for p in (mode, ref) if p) if to_bank else ref,
                    bank_account=self.account(deposit_to) if to_bank else None)
            self.record("customer_payment", event.zoho_id, payment)
            # Unused Amount is simply what is not applied here; it stays on the payment.
            for row in rows:
                applied = mapping.money(row["Amount Applied to Invoice"])
                if applied <= 0:
                    continue
                when = _date(row["Invoice Payment Applied Date"], event.date)
                number = row["Invoice Number"].strip()
                if Invoice.objects.filter(number=number).exists():
                    invoice = self._invoice(number)
                    apply_payment(payment, invoice, applied, date=max(when, invoice.date), user=self.user)
                else:
                    # Paid before the invoice existed (online orders): applied when the invoice posts.
                    self.pending_applications.setdefault(number, []).append((payment, applied, when))
            charges = mapping.money(first["Bank Charges"])
            if charges > 0 and not to_bank:
                # No payment backs a charge taken out of Undeposited Funds, so posting it would break
                # the Undeposited check; the tie-out lists it instead (controller ruling, fix round 1).
                self.warnings.append(
                    f"Bank charges on payment {event.number}: {charges} not posted (deposited to Undeposited Funds).")
            elif charges > 0:
                entry = post_entry(
                    date=event.date, user=self.user, memo=f"Bank charges on payment {event.number}",
                    reference=ref, source_type="migration.expense", source_id=event.zoho_id,
                    lines=[LineSpec(self.account("Bank Fees and Charges"), debit=charges),
                           LineSpec(payment.deposited_to, credit=charges)])
                self.record("bank_charges", event.zoho_id, entry)

    def post_credit_note(self, event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        lines = [
            CreditLineSpec(
                row["Item Name"].strip() or _credit_reason(first) or "Credit", mapping.money(row["Item Total"]),
                self.account(row["Account"]) if row["Account"].strip() else None)
            for row in rows if mapping.money(row["Item Total"])
        ]
        with transaction.atomic():
            with use_number(DocType.CN, event.number):
                note = create_credit_note(
                    customer=self.customer_for("credit_notes", first), date=event.date, lines=lines,
                    reason=_credit_reason(first), user=self.user)
            self.record("credit_note", event.zoho_id, note)

    def post_credit_application(self, event) -> None:
        with transaction.atomic():
            for row in self.doc_rows(event.kind, event.zoho_id):
                note = CreditNote.objects.get(number=row["Credit Note Number"].strip())
                app = apply_credit(
                    note, self._invoice(row["Invoice Number"]), mapping.money(row["Amount"]),
                    date=event.date, user=self.user)
                self.record("credit_application", event.zoho_id, app)

    def post_undeposited_transfer(self, event) -> None:
        """Zoho moved cash out of Undeposited Funds by transfer; here it becomes a real deposit (spec 4.2)."""
        row = self.doc_rows(event.kind, event.zoho_id)[0]
        total = mapping.money(row["Total"])
        candidates = list(
            undeposited_payments().filter(date__lte=event.date, amount__lte=total)[:MAX_CANDIDATES])
        found = exact_subset([p.amount for p in candidates], total)
        payments = [candidates[i] for i in found] if found is not None else self._paired(event.date, total)
        if not payments:
            self.warnings.append(
                f"Undeposited transfer {event.date.isoformat()} {total}: no exact set of payments.")
            return
        with transaction.atomic():
            deposit = make_deposit(
                payments=payments, bank_account=self.account(row["To Account"]), date=event.date, user=self.user)
            self.record("undeposited_transfer", event.zoho_id, deposit)

    def _paired(self, when: date, total: Decimal) -> list[Payment] | None:
        numbers = getattr(self, "deposit_pairs", {}).get((when, total))
        if not numbers:
            return None
        payments = list(Payment.objects.filter(number__in=numbers))
        if len(payments) != len(set(numbers)) or sum((p.amount for p in payments), ZERO) != total:
            return None
        return payments

    def load_deposit_pairs(self, path: Path) -> None:
        """deposit_pairs.csv: date,amount,payment_numbers (numbers separated by ';')."""
        self.deposit_pairs = {}
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if not (row.get("date") or "").strip():
                    continue
                key = (date.fromisoformat(row["date"].strip()), mapping.money(row["amount"]))
                self.deposit_pairs[key] = [n.strip() for n in row["payment_numbers"].split(";") if n.strip()]

    # ---- vendor side ----
    def post_vendor_payment(self, event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        vendor = self.vendors[first["Vendor Name"]]
        paid_from = self.account(first["Paid Through"])
        method = mapping.vendor_method(first["Mode"], paid_from.subtype)
        ref = first["Reference Number"].strip()
        applications, later = [], []
        for row in rows:
            amount = mapping.money(row["Bill Amount"])
            number = row["Bill Number"].strip()
            if not number or amount <= 0:
                continue
            if self._bill_imported(vendor, number):
                applications.append((self._bill(vendor, number, amount), amount))
            else:
                later.append((number, amount))  # paid before the bill's date: applied when the bill posts
        is_check = method == VendorPaymentMethod.CHECK
        if is_check and not ref:
            # A check with no number is "waiting to be printed" in DNB Books; these were written long ago.
            ref = f"none-{event.number}"
            self.warnings.append(f"Vendor payment {event.number}: Zoho has no check number; recorded as {ref}.")
        with transaction.atomic():
            with use_number(DocType.VP, event.number):
                # pay_vendor registers the check number as handwritten.
                payment = pay_vendor(
                    vendor=vendor, date=event.date, method=method, amount=mapping.money(first["Amount"]),
                    paid_from=paid_from, user=self.user, check_number=ref if is_check else "",
                    reference="" if is_check else ref, applications=applications)
            self.record("vendor_payment", event.zoho_id, payment)
            for number, amount in later:
                self.pending_bill_applications.setdefault((vendor.pk, number), []).append((payment, amount))

    def post_vendor_credit(self, event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        vendor = self.vendors[first["Vendor Name"]]
        returns, allowances = [], []
        for row in rows:
            amount = mapping.money(row["Item Total"])
            if not amount:
                continue  # an application-only row
            has_item = row["SKU"].strip() or row["Item Name"].strip()
            item = self.item_for(row) if has_item else None
            if item is not None and item.is_stock:
                returns.append(ReturnLineSpec(item, _quantity(row["Quantity"]), amount))
            else:
                account = self.account(row["Account"]) if row["Account"].strip() else None
                allowances.append(AllowanceLineSpec(account, amount, row["Item Name"].strip()))
        reason = "Imported from Zoho"
        with transaction.atomic():
            credits = []
            with use_number(DocType.VC, event.number):
                if returns:
                    credits.append(create_return_credit(
                        vendor=vendor, date=event.date, lines=returns, reason=reason, user=self.user,
                        allow_negative=True))
                if allowances:
                    # With returns too, the allowance part is a second credit under our own next number.
                    credits.append(create_allowance_credit(
                        vendor=vendor, date=event.date, lines=allowances, reason=reason, user=self.user))
            if not credits:
                return
            if returns and allowances:
                self.warnings.append(
                    f"{event.number}: lines without a stock item became vendor credit {credits[1].number}.")
            self.record("vendor_credit", event.zoho_id, credits[0])
            if len(credits) > 1:
                self.record("vendor_credit", f"{event.zoho_id}:allowance", credits[1])
        if returns:
            self._note_negative([l.item for l in returns], event)

    def apply_vendor_credits(self) -> None:
        """Zoho's vendor credit export doesn't say which bills a credit was applied to. Each bill does carry
        Zoho's remaining balance, so after everything else has posted, each credit's applied part (Total minus
        Balance) goes to that vendor's bills that still owe more here than in Zoho, oldest bill first."""
        zoho_balance = {}
        for row in self.export.files["bills"]:
            zoho_balance.setdefault(row["Bill ID"], mapping.money(row["Balance"]))
        bill_type = ContentType.objects.get_for_model(Bill)
        bills = {r.object_id: r.zoho_id for r in ZohoRecord.objects.filter(kind="bill", content_type=bill_type)}
        excess = {}
        for bill in Bill.objects.filter(pk__in=bills).order_by("date", "id"):
            over = bill_balance(bill) - zoho_balance[bills[bill.pk]]
            if over > 0:
                excess[bill.pk] = [bill, over]
        credit_type = ContentType.objects.get_for_model(VendorCredit)
        records = ZohoRecord.objects.filter(kind="vendor_credit", content_type=credit_type)
        credits = VendorCredit.objects.filter(pk__in=records.values("object_id")).order_by("date", "id")
        zoho_id = {r.object_id: r.zoho_id.split(":")[0] for r in records}
        for credit in credits:
            first = self.doc_rows("vendor_credit", zoho_id[credit.pk])[0]
            wanted = min(mapping.money(first["Total"]) - mapping.money(first["Balance"]),
                         vendor_credit_unapplied(credit))
            with transaction.atomic():
                for bill_pk, slot in excess.items():
                    bill, over = slot
                    if wanted <= 0:
                        break
                    if bill.vendor_id != credit.vendor_id or over <= 0:
                        continue
                    take = min(wanted, over)
                    apply_vendor_credit(credit, bill, take, date=max(credit.date, bill.date), user=self.user)
                    slot[1] -= take
                    wanted -= take
            if wanted > 0:
                self.warnings.append(f"{credit.number}: {wanted:,.2f} of the credit matched no bill.")

    def _bill_imported(self, vendor, number: str) -> bool:
        imported = ZohoRecord.objects.filter(
            kind="bill", content_type=ContentType.objects.get_for_model(Bill)).values("object_id")
        return Bill.objects.filter(pk__in=imported, vendor=vendor, vendor_invoice_number=number).exists()

    def apply_pending_bill(self, bill) -> None:
        """Vendor payments Zoho made before this bill's date, applied on the bill date at the earliest."""
        for payment, amount in self.pending_bill_applications.pop((bill.vendor_id, bill.vendor_invoice_number), []):
            apply_vendor_payment(payment, bill, amount, date=max(payment.date, bill.date), user=self.user)

    def apply_pending(self, invoice) -> None:
        """Payments Zoho received before this invoice existed, applied on the invoice date at the earliest."""
        for payment, amount, when in self.pending_applications.pop(invoice.number, []):
            apply_payment(payment, invoice, amount, date=max(when, invoice.date), user=self.user)

    # ---- entries ----
    def _post(self, event, source_type: str, lines: list[LineSpec], *, memo: str, reference: str = "") -> None:
        with transaction.atomic():
            entry = post_entry(
                date=event.date, lines=lines, user=self.user, memo=memo, reference=reference,
                source_type=source_type, source_id=event.zoho_id)
            self.record(event.kind, event.zoho_id, entry)

    def post_expense(self, event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        first = rows[0]
        if len(rows) == 1:
            debits = [LineSpec(self.account(first["Expense Account"]), debit=mapping.money(first["Total"]),
                               description=first["Expense Description"])]
        else:  # an itemized expense: one debit per row
            debits = [LineSpec(self.account(r["Expense Account"]), debit=mapping.money(r["Expense Amount"]),
                               description=r["Expense Description"]) for r in rows]
        total = sum((l.debit for l in debits), ZERO)
        self._post(
            event, "migration.expense", debits + [LineSpec(self.account(first["Paid Through"]), credit=total)],
            memo=first["Expense Description"] or "Expense", reference=first["Reference#"].strip())

    def post_journal(self, event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        lines = []
        for row in rows:
            debit, credit = mapping.money(row["Debit"]), mapping.money(row["Credit"])
            if debit or credit:
                lines.append(LineSpec(self.account(row["Account"]), debit=debit, credit=credit,
                                      description=row["Description"]))
        self._post(event, "migration.journal", lines, memo=rows[0]["Notes"] or f"Journal {event.number}",
                   reference=rows[0]["Journal Number"].strip())

    def _between_accounts(self, event, source_type: str) -> None:
        row = self.doc_rows(event.kind, event.zoho_id)[0]
        total = mapping.money(row["Total"])
        self._post(
            event, source_type,
            [LineSpec(self.account(row["To Account"]), debit=total),
             LineSpec(self.account(row["From Account"]), credit=total)],
            memo=row["Description"] or row["Transaction Type"], reference=row["Reference"].strip())

    def post_transfer(self, event) -> None:
        self._between_accounts(event, "migration.transfer")

    def post_other_deposit(self, event) -> None:
        self._between_accounts(event, "migration.deposit")

    # ---- stock ----
    def post_adjustment(self, event) -> None:
        rows = self.doc_rows(event.kind, event.zoho_id)
        items = []
        with transaction.atomic():
            for i, row in enumerate(rows):
                item = self.item_for(row)
                account = self.account(row["Account"]) if row["Account"].strip() else None
                reason = row["Reason"].strip()
                if row["Adjustment Type"].strip().lower() == "value":
                    target = record_value_adjustment(
                        item=item, date=event.date, amount=mapping.money(row["Value Adjusted"]), reason=reason,
                        user=self.user, account=account)
                else:
                    target, = record_movements(
                        kind=MovementKind.ADJUSTMENT, date=event.date, user=self.user, allow_negative=True,
                        lines=[StockLine(item, _quantity(row["Quantity Adjusted"]), reason, account)])
                self.record("adjustment", event.zoho_id if i == 0 else f"{event.zoho_id}:{i + 1}", target)
                items.append(item)
        self._note_negative(items, event)

    # ---- opening balances ----
    def load_opening_balances(self, path: Path) -> None:
        """opening_balances.csv (account,debit,credit) as one entry on the opening date, balanced against
        "Opening Balance Offset" when the rows don't balance."""
        lines = []
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = (row.get("account") or "").strip()
                net = mapping.money(row.get("debit")) - mapping.money(row.get("credit"))
                if not name or not net:
                    continue
                account = self.account(name)
                lines.append(LineSpec(account, debit=net) if net > 0 else LineSpec(account, credit=-net))
        difference = sum((l.debit - l.credit for l in lines), ZERO)
        if difference:
            offset = self.account("Opening Balance Offset")
            lines.append(LineSpec(offset, credit=difference) if difference > 0 else LineSpec(offset, debit=-difference))
        if not lines:
            return
        with transaction.atomic():
            entry = post_entry(
                date=OPENING_DATE, lines=lines, user=self.user, memo="Opening balances from Zoho",
                source_type="migration.opening", source_id=Path(path).name)
            self.record("opening_balances", Path(path).name, entry)
