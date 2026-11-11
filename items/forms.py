from django import forms

from ledger.forms import RangeForm

import re

from ledger.models import Account, AccountSubtype

from .stock import on_hand
from .models import (
    RESERVED_MESSAGE, RESERVED_SKUS, SKU_MESSAGE, SKU_PATTERN, Category, CostingMethod, InventorySettings, Item,
    ItemType,
)


class MovementsForm(RangeForm):
    item = forms.CharField(label="Item SKU", required=False)
    field_order = ["start", "end", "item"]

    def clean_item(self):
        sku = self.cleaned_data["item"].strip()
        if not sku:
            return None
        item = Item.objects.filter(sku__iexact=sku).first()
        if item is None:
            raise forms.ValidationError(f"No item has SKU {sku}.")
        return item


NOT_FIFO = "Lot tracking needs an item that uses FIFO costing."


class _ItemChoice(forms.ModelChoiceField):
    def label_from_instance(self, item):
        return f"{item.sku} · {item.name}"


class FifoLotsForm(RangeForm):
    # Every stock item is accepted here so a weighted-average one gets the plain message, not "invalid choice".
    item = _ItemChoice(Item.objects.filter(item_type=ItemType.STOCK).order_by("sku"), required=False,
                       label="Item", empty_label="Pick an item")
    field_order = ["item", "start", "end"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].widget.choices = [("", "Pick an item")] + [
            (i.pk, f"{i.sku} · {i.name}")
            for i in Item.objects.filter(item_type=ItemType.STOCK, costing_method=CostingMethod.FIFO).order_by("sku")]

    def clean_item(self):
        item = self.cleaned_data["item"]
        if item is not None and item.costing_method != CostingMethod.FIFO:
            raise forms.ValidationError(NOT_FIFO)
        return item


class ItemForm(forms.ModelForm):
    class Meta:
        model = Item
        fields = [
            "sku", "name", "description", "item_type", "category", "unit", "packaging", "sale_price", "purchase_cost",
            "vendor_part_number", "costing_method", "income_account", "cogs_account", "inventory_account",
            "reorder_point",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {"reorder_point": "Reorder point"}
        help_texts = {"reorder_point": "The item shows on Low stock when on hand plus on order falls to this. "
                                       "Leave blank to skip. Stock items only."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["income_account"].queryset = Account.objects.filter(
            subtype__in=[AccountSubtype.INCOME, AccountSubtype.OTHER_INCOME], is_active=True)
        self.fields["cogs_account"].queryset = Account.objects.filter(
            subtype=AccountSubtype.COST_OF_GOODS_SOLD, is_active=True)
        self.fields["inventory_account"].queryset = Account.objects.filter(
            subtype=AccountSubtype.INVENTORY, is_active=True)
        self.fields["costing_method"].choices = CostingMethod.choices
        self.fields["sku"].label = "SKU"
        self.fields["cogs_account"].label = "Cost of goods sold account"
        self.fields["vendor_part_number"].label = "Vendor part #"
        self.fields["costing_method"].required = False
        self.fields["cogs_account"].required = False
        self.fields["inventory_account"].required = False
        self.fields["category"].required = False
        self.fields["unit"].required = False
        self.locked = bool(self.instance.pk and self.instance.has_movements)
        if self.locked:
            self.fields["item_type"].disabled = True
            self.fields["costing_method"].disabled = True
            self.fields["inventory_account"].disabled = True  # stock is tied to this account in the ledger
        if not self.instance.pk:
            s = InventorySettings.load()
            self.initial.setdefault("income_account", s.default_income_account_id)
            self.initial.setdefault("cogs_account", s.default_cogs_account_id)
            self.initial.setdefault("inventory_account", s.default_inventory_account_id)
            self.initial.setdefault("costing_method", CostingMethod.FIFO)
            self.initial.setdefault("unit", "each")
            from .sku import next_sku

            suggested = next_sku(s)
            if suggested:
                self.initial.setdefault("sku", suggested)
                self.fields["sku"].help_text = "Next in your SKU series. You can type a different one."
        if self.instance.pk:
            self.fields["is_active"] = forms.BooleanField(required=False, label="Active", initial=self.instance.is_active)

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        if not re.match(SKU_PATTERN, sku):
            raise forms.ValidationError(SKU_MESSAGE)
        if sku.lower() in RESERVED_SKUS:
            raise forms.ValidationError(RESERVED_MESSAGE)
        taken = Item.objects.filter(sku__iexact=sku).exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError(f"SKU {sku} already exists.")
        return sku

    def clean(self):
        data = super().clean()
        if data.get("item_type") != ItemType.STOCK:
            data["reorder_point"] = None
        if self.locked:
            # A disabled field ignores what the caller sends; this catches a hand-made request. Values are compared
            # as text because a browser posts "5" while the Claude tools pass the account's number 5.
            current = {"costing_method": self.instance.costing_method, "item_type": self.instance.item_type,
                       "inventory_account": self.instance.inventory_account_id}
            messages = {"costing_method": "The costing method can't change after an item has stock history.",
                        "item_type": "The item type can't change after an item has stock history.",
                        "inventory_account": "The inventory account can't change after an item has stock history."}
            for name, message in messages.items():
                sent = self.data.get(name)
                if sent not in (None, "") and str(sent) != str(current[name]):
                    self.add_error(None, message)
        if self.instance.pk and "is_active" in data and not data["is_active"] and self.instance.is_active:
            held = on_hand(self.instance)
            if held != 0:
                self.add_error(None, f"{self.instance.sku} still has {held} on hand. Adjust it to zero before deactivating.")
        item_type = data.get("item_type")
        if item_type == ItemType.NON_STOCK:
            data["cogs_account"] = None
            data["inventory_account"] = None
            data["costing_method"] = ""
        elif item_type == ItemType.STOCK:
            if not data.get("costing_method"):
                data["costing_method"] = CostingMethod.FIFO
            for name, label in (("cogs_account", "cost of goods sold"), ("inventory_account", "inventory")):
                if not data.get(name):
                    self.add_error(name, f"Choose an {label} account." if name == "inventory_account" else f"Choose a {label} account.")
        return data

    def save(self, commit=True):
        item = super().save(commit=False)
        if "is_active" in self.fields:
            item.is_active = self.cleaned_data["is_active"]
        if commit:
            item.save()
        return item


DATE_ERROR = "Enter a date like 2026-01-31."
REASONS = ["Damaged", "Lost", "Sample", "Correction", "Other"]


class ValueAdjustForm(forms.Form):
    date = forms.DateField(
        label="Date", input_formats=["%Y-%m-%d"], widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        error_messages={"invalid": DATE_ERROR, "required": DATE_ERROR})
    mode = forms.ChoiceField(label="Amount is", choices=[("total", "New total value"), ("change", "Change by (+ or -)")])
    amount = forms.DecimalField(label="Amount", decimal_places=2, max_digits=14, error_messages={
        "invalid": "Enter an amount.", "required": "Enter an amount.", "max_decimal_places": "Enter an amount with at most 2 decimal places.",
        "max_digits": "Enter a smaller amount.", "max_whole_digits": "Enter a smaller amount."})
    reason = forms.CharField(label="Reason", max_length=200, error_messages={"required": "Enter a reason."})
    token = forms.CharField(widget=forms.HiddenInput, required=False)


class VoidMovementForm(forms.Form):
    date = forms.DateField(
        label="Void date", input_formats=["%Y-%m-%d"], widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        error_messages={"invalid": DATE_ERROR, "required": DATE_ERROR})
    reason = forms.CharField(label="Reason", max_length=200, error_messages={"required": "Enter a reason."})


class AdjustDateForm(forms.Form):
    date = forms.DateField(
        label="Date", input_formats=["%Y-%m-%d"], widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        error_messages={"invalid": DATE_ERROR, "required": DATE_ERROR})


class CountStartForm(forms.Form):
    name = forms.CharField(label="Count name", max_length=100, error_messages={"required": "Enter a name for this count."})
    scope = forms.ChoiceField(label="Count", choices=[("ALL", "All active stock items"), ("CATEGORY", "One category")])
    category = forms.ModelChoiceField(Category.objects.all(), required=False, label="Category")

    def clean(self):
        data = super().clean()
        if data.get("scope") == "CATEGORY" and not data.get("category"):
            self.add_error("category", "Choose a category.")
        return data


class CountPostForm(forms.Form):
    date = forms.DateField(
        label="Post on", input_formats=["%Y-%m-%d"], widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        error_messages={"invalid": DATE_ERROR, "required": DATE_ERROR})
    confirm_moved = forms.BooleanField(required=False, label="I've checked the flagged items")


class SettingsForm(forms.ModelForm):
    class Meta:
        model = InventorySettings
        fields = [
            "default_inventory_account", "default_cogs_account", "default_income_account",
            "shrinkage_account", "writedown_account", "opening_equity_account",
        ]
        labels = {
            "default_inventory_account": "Inventory account (default)",
            "default_cogs_account": "Cost of goods sold account (default)",
            "default_income_account": "Income account (default)",
            "shrinkage_account": "Shrinkage account",
            "writedown_account": "Write-down account",
            "opening_equity_account": "Opening balance equity account",
        }
        help_texts = {
            "shrinkage_account": "Used for quantity adjustments and counts.",
            "writedown_account": "Used for value adjustments.",
            "opening_equity_account": "The other side of opening stock.",
        }

    SUBTYPES = {
        "default_inventory_account": [AccountSubtype.INVENTORY],
        "default_cogs_account": [AccountSubtype.COST_OF_GOODS_SOLD],
        "default_income_account": [AccountSubtype.INCOME, AccountSubtype.OTHER_INCOME],
        "shrinkage_account": [AccountSubtype.EXPENSE, AccountSubtype.OTHER_EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD],
        "writedown_account": [AccountSubtype.EXPENSE, AccountSubtype.OTHER_EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD],
        "opening_equity_account": [AccountSubtype.EQUITY],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, subtypes in self.SUBTYPES.items():
            field = self.fields[name]
            field.queryset = Account.objects.filter(subtype__in=subtypes, is_active=True).order_by("number")
            field.required = True
            field.error_messages["required"] = "Choose an account."
            field.error_messages["invalid_choice"] = "Choose an account from the list."


class SkuSeriesForm(forms.ModelForm):
    class Meta:
        model = InventorySettings
        fields = ["sku_prefix", "sku_next", "sku_digits"]
        labels = {"sku_prefix": "Base SKU", "sku_next": "Next number", "sku_digits": "Digits"}
        help_texts = {
            "sku_prefix": "The start of every new SKU, for example DNB- . Leave blank to turn the series off.",
            "sku_next": "The number the next new item gets.",
            "sku_digits": "Numbers are padded with zeros to this many digits (4 gives 0001).",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sku_prefix"].required = False
        self.fields["sku_next"].error_messages["required"] = "Enter a next number of 1 or more."
        self.fields["sku_digits"].error_messages["required"] = "Enter 1 to 8 digits."

    def clean_sku_prefix(self):
        prefix = self.cleaned_data.get("sku_prefix", "").strip()
        if prefix and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$", prefix):
            raise forms.ValidationError("The base SKU can use letters, numbers, dots, dashes, and underscores.")
        return prefix

    def clean_sku_next(self):
        n = self.cleaned_data.get("sku_next")
        if n is None or n < 1:
            raise forms.ValidationError("Enter a next number of 1 or more.")
        return n

    def clean_sku_digits(self):
        d = self.cleaned_data.get("sku_digits")
        if d is None or not 1 <= d <= 8:
            raise forms.ValidationError("Enter 1 to 8 digits.")
        return d

    def clean(self):
        data = super().clean()
        prefix, n, d = data.get("sku_prefix"), data.get("sku_next"), data.get("sku_digits")
        if prefix and n and d and len(f"{prefix}{n:0{d}d}") > 50:
            raise forms.ValidationError("That makes SKUs longer than 50 characters. Use a shorter base.")
        return data
