from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ledger.forms import AsOfForm
from ledger.views import _data, _today, _wants_csv

from . import csv_export, reports
from .forms import ItemForm, MovementsForm
from .models import Category, Item, ItemType, StockMovement
from .stock import current_unit_cost, effective_total, on_hand

PAGE_SIZE = 50


def _item_or_404(sku: str) -> Item:
    item = Item.objects.filter(sku__iexact=sku).select_related("category").first()
    if item is None:
        raise Http404("No item has that SKU.")
    return item


# ---- items ----

@login_required
def item_list(request):
    items = Item.objects.select_related("category").order_by("sku")
    q = request.GET.get("q", "").strip()
    if q:
        items = items.filter(Q(sku__icontains=q) | Q(name__icontains=q) | Q(vendor_part_number__icontains=q))
    category = request.GET.get("category", "")
    if category.isdigit():
        items = items.filter(category_id=int(category))
    kind = request.GET.get("type", "")
    if kind == "stock":
        items = items.filter(item_type=ItemType.STOCK)
    elif kind == "non-stock":
        items = items.filter(item_type=ItemType.NON_STOCK)
    show_inactive = request.GET.get("inactive") == "1"
    if not show_inactive:
        items = items.filter(is_active=True)

    page = Paginator(items, PAGE_SIZE).get_page(request.GET.get("page"))
    totals = reports.item_totals([i.pk for i in page.object_list])
    rows = []
    for item in page.object_list:
        quantity, value = totals.get(item.pk, (0, Decimal("0.00")))
        rows.append({"item": item, "quantity": quantity, "value": value,
                     "unit_cost": current_unit_cost(item) if item.is_stock else None})

    all_totals = reports.item_totals()
    active = Item.objects.filter(is_active=True)
    below_zero = reports.negative_item_count()
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "items/item_list.html", {
        "rows": rows, "page": page, "nav_active": "items", "q": q, "category": category, "type": kind,
        "show_inactive": show_inactive, "categories": Category.objects.all(), "querystring": query.urlencode(),
        "stock_value": sum((v for _, v in all_totals.values()), Decimal("0.00")),
        "stock_count": active.filter(item_type=ItemType.STOCK).count(),
        "non_stock_count": active.filter(item_type=ItemType.NON_STOCK).count(),
        "below_zero": below_zero,
        "active_count": active.count(),
    })


@login_required
def item_detail(request, sku):
    item = _item_or_404(sku)
    history = []
    running = 0
    movements = item.movements.select_related("journal_entry", "created_by").prefetch_related(
        "revisions__journal_entry").order_by("date", "id")
    for m in movements:
        if not m.voided:
            running += m.quantity
        history.append({"movement": m, "running": running, "value": effective_total(m), "revisions": list(m.revisions.all())})
    history.reverse()
    quantity = on_hand(item) if item.is_stock else None
    totals = reports.item_totals([item.pk]).get(item.pk, (0, Decimal("0.00")))
    return render(request, "items/item_detail.html", {
        "item": item, "history": history, "quantity": quantity, "value": totals[1],
        "unit_cost": current_unit_cost(item) if item.is_stock else None, "nav_active": "items",
    })


def _save_item_form(request, form, item=None):
    if form.is_valid():
        saved = form.save()
        messages.success(request, f"Saved {saved.sku}.")
        return redirect(reverse("items:detail", args=[saved.sku]))
    return None


@login_required
@permission_required("items.add_item", raise_exception=True)
def item_new(request):
    form = ItemForm(request.POST or None)
    if request.method == "POST":
        response = _save_item_form(request, form)
        if response:
            return response
    return render(request, "items/item_form.html", {"form": form, "item": None, "nav_active": "items"})


@login_required
@permission_required("items.change_item", raise_exception=True)
def item_edit(request, sku):
    item = _item_or_404(sku)
    form = ItemForm(request.POST or None, instance=item)
    if request.method == "POST":
        response = _save_item_form(request, form, item)
        if response:
            return response
    return render(request, "items/item_form.html", {"form": form, "item": item, "nav_active": "items"})


@login_required
@permission_required("items.change_item", raise_exception=True)
@require_POST
def item_deactivate(request, sku):
    item = _item_or_404(sku)
    held = on_hand(item) if item.is_stock else 0
    if held != 0:
        messages.error(request, f"{item.sku} still has {held} on hand. Adjust it to zero before deactivating.")
    else:
        item.is_active = False
        item.save()
        messages.success(request, f"{item.sku} is now inactive.")
    return redirect(reverse("items:detail", args=[item.sku]))


# ---- reports ----

@login_required
def inventory_valuation(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    report = reports.inventory_valuation(form.cleaned_data["as_of"]) if form.is_valid() else None
    if report and _wants_csv(request):
        return csv_export.valuation_csv(report)
    return render(request, "items/inventory_valuation.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def stock_movements(request):
    today = _today()
    form = MovementsForm(_data(request, {"start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()}))
    rows = None
    if form.is_valid():
        d = form.cleaned_data
        rows = reports.stock_movements(d["start"], d["end"], d["item"])
        if _wants_csv(request):
            return csv_export.movements_csv(rows, d["start"], d["end"])
    return render(request, "items/stock_movements.html", {"form": form, "rows": rows, "nav_active": "reports"})


@login_required
def negative_stock(request):
    rows = reports.negative_stock()
    if _wants_csv(request):
        return csv_export.negative_stock_csv(rows)
    return render(request, "items/negative_stock.html", {"rows": rows, "nav_active": "reports"})
