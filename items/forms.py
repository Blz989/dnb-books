from django import forms

from ledger.forms import RangeForm

import re

from ledger.models import Account, AccountSubtype

from .stock import on_hand
from .models import (
    RESERVED_MESSAGE, RESERVED_SKUS, SKU_MESSAGE, SKU_PATTERN, CostingMethod, InventorySettings, Item, ItemType,
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


class ItemForm(forms.ModelForm):
    class Meta:
        model = Item
        fields = [
            "sku", "name", "description", "item_type", "category", "unit", "sale_price", "purchase_cost",
            "vendor_part_number", "costing_method", "income_account", "cogs_account", "inventory_account",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

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
        if not self.instance.pk:
            s = InventorySettings.load()
            self.initial.setdefault("income_account", s.default_income_account_id)
            self.initial.setdefault("cogs_account", s.default_cogs_account_id)
            self.initial.setdefault("inventory_account", s.default_inventory_account_id)
            self.initial.setdefault("costing_method", CostingMethod.FIFO)
            self.initial.setdefault("unit", "each")
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
        if self.locked:
            # A disabled field ignores what the browser sends; this catches a hand-made request.
            if self.data.get("costing_method") not in (None, "", self.instance.costing_method):
                self.add_error(None, "The costing method can't change after an item has stock history.")
            if self.data.get("item_type") not in (None, "", self.instance.item_type):
                self.add_error(None, "The item type can't change after an item has stock history.")
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
