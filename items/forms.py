from django import forms

from ledger.forms import RangeForm

from .models import Item


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
