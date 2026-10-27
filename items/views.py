from datetime import date
from decimal import Decimal

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ledger.forms import AsOfForm
from ledger.views import _data, _today, _wants_csv

from . import csv_export, reports
from core.roles import require_admin
from ledger.errors import PostingError
from ledger.models import Account, AccountType

from . import counts as count_service
from . import imports as import_service
from .errors import CountError, CountNeedsConfirmation, NegativeStockWarning, SettingsMissingError, StockError
from .forms import (
    REASONS, AdjustDateForm, CountPostForm, CountStartForm, ItemForm, MovementsForm, SettingsForm, ValueAdjustForm,
    VoidMovementForm,
)
from .models import (
    Category, CountStatus, InventorySettings, Item, ItemType, MovementKind, StockCount, StockMovement, SubmittedToken,
)
from .stock import (
    StockLine, current_unit_cost, effective_total, on_hand, record_movements, record_value_adjustment, stock_value,
    void_movement,
)

PAGE_SIZE = 50


def _posted(request):
    """Bind a form only on POST; `request.POST or None` would treat an empty POST as unbound."""
    return request.POST if request.method == "POST" else None


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
    form = ItemForm(_posted(request))
    if request.method == "POST":
        response = _save_item_form(request, form)
        if response:
            return response
    return render(request, "items/item_form.html", {"form": form, "item": None, "nav_active": "items"})


@login_required
@permission_required("items.change_item", raise_exception=True)
def item_edit(request, sku):
    item = _item_or_404(sku)
    form = ItemForm(_posted(request), instance=item)
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


# ---- adjustments ----

MAX_LINES = 20
ALREADY_SAVED = "This adjustment was already saved."


def _claim_token(token: str) -> bool:
    """Record a form token inside the caller's transaction. False when it was already used."""
    if not token:
        return True
    try:
        with transaction.atomic():
            SubmittedToken.objects.create(token=token)
    except IntegrityError:
        return False
    return True


def _token_used(token: str) -> bool:
    return bool(token) and SubmittedToken.objects.filter(token=token).exists()


def _error_messages(exc) -> dict:
    return {"errors": [str(exc)], "settings_link": isinstance(exc, SettingsMissingError)}


def _adjust_accounts():
    return Account.objects.filter(type=AccountType.EXPENSE, is_active=True).order_by("number")


def _read_rows(post):
    return [
        {"index": i, "sku": post.get(f"lines-{i}-sku", "").strip(), "quantity": post.get(f"lines-{i}-quantity", "").strip(),
         "reason": post.get(f"lines-{i}-reason", "Damaged"), "note": post.get(f"lines-{i}-note", "").strip(),
         "account": post.get(f"lines-{i}-account", "")}
        for i in range(MAX_LINES)
    ]


def _build_lines(rows):
    lines, errors = [], []
    n = 0
    for row in rows:
        if not row["sku"] and not row["quantity"]:
            continue
        n += 1
        item = Item.objects.filter(sku__iexact=row["sku"]).first() if row["sku"] else None
        if not row["sku"]:
            errors.append(f"Line {n}: choose an item.")
        elif item is None:
            errors.append(f"Line {n}: no item has SKU {row['sku']}.")
        try:
            quantity = int(row["quantity"])
        except ValueError:
            errors.append(f"Line {n}: quantity must be a whole number.")
            continue
        if row["reason"] not in REASONS:
            errors.append(f"Line {n}: choose a reason.")
            continue
        account = None
        if row["account"]:
            account = _adjust_accounts().filter(pk=row["account"]).first() if row["account"].isdigit() else None
            if account is None:
                errors.append(f"Line {n}: choose an expense account from the list.")
                continue
        if item is None:
            continue
        reason = f"{row['reason']}: {row['note']}" if row["note"] else row["reason"]
        lines.append(StockLine(item, quantity, reason=reason, account=account))
    if not lines and not errors:
        errors.append("Add at least one line.")
    return lines, errors


def _adjust_context(request, rows, token, date_form, **extra):
    shown = max([r["index"] for r in rows if r["sku"] or r["quantity"]] + [3]) + 1
    settings_row = InventorySettings.load()
    return {
        "rows": rows, "token": token, "date_form": date_form, "accounts": _adjust_accounts(), "reasons": REASONS,
        "default_account": settings_row.shrinkage_account, "shown": shown, "nav_active": "adjustments", **extra,
    }


@login_required
def adjust(request):
    if request.method != "POST":
        rows = _read_rows({})
        prefill = _item_or_none(request.GET.get("item", ""))
        if prefill:
            rows[0]["sku"] = prefill.sku
        form = AdjustDateForm(initial={"date": _today().isoformat()})
        return render(request, "items/adjust.html", _adjust_context(request, rows, str(uuid.uuid4()), form))

    token = request.POST.get("token", "")
    rows = _read_rows(request.POST)
    if _token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("items:adjustments"))
    date_form = AdjustDateForm(request.POST)
    if request.POST.get("back"):
        return render(request, "items/adjust.html", _adjust_context(request, rows, token, date_form))
    lines, errors = _build_lines(rows)
    if not date_form.is_valid():
        errors = [e for e in date_form.errors["date"]] + errors
    if errors:
        return render(request, "items/adjust.html", _adjust_context(request, rows, token, date_form, errors=errors))
    try:
        with transaction.atomic():
            if not _claim_token(token):
                messages.info(request, ALREADY_SAVED)
                return redirect(reverse("items:adjustments"))
            record_movements(
                kind=MovementKind.ADJUSTMENT, date=date_form.cleaned_data["date"], lines=lines, user=request.user,
                allow_negative=request.POST.get("allow_negative") == "1")
    except NegativeStockWarning as warning:
        return render(request, "items/adjust_confirm.html", _adjust_context(
            request, rows, token, date_form, warnings=warning.messages))
    except (StockError, PostingError) as exc:
        return render(request, "items/adjust.html", _adjust_context(request, rows, token, date_form, **_error_messages(exc)))
    messages.success(request, f"Saved {len(lines)} adjustment{'s' if len(lines) != 1 else ''}.")
    if len(lines) == 1:
        return redirect(reverse("items:detail", args=[lines[0].item.sku]))
    return redirect(reverse("items:adjustments"))


def _item_or_none(sku: str):
    return Item.objects.filter(sku__iexact=sku.strip()).first() if sku.strip() else None


@login_required
def adjustment_list(request):
    movements = StockMovement.objects.filter(
        kind__in=[MovementKind.ADJUSTMENT, MovementKind.VALUE_ADJUSTMENT]
    ).select_related("item", "journal_entry", "created_by").order_by("-date", "-id")
    page = Paginator(movements, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "items/adjustment_list.html", {"page": page, "nav_active": "adjustments"})


@login_required
def value_adjust(request, sku):
    require_admin(request.user)
    item = _item_or_404(sku)
    if not item.is_stock:
        raise Http404("Not a stock item.")
    held = on_hand(item)
    value = stock_value(item)
    context = {"item": item, "on_hand": held, "value": value, "nav_active": "items"}
    if request.method != "POST":
        form = ValueAdjustForm(initial={"date": _today().isoformat(), "mode": "change", "token": str(uuid.uuid4())})
        return render(request, "items/value_adjust.html", {**context, "form": form})
    form = ValueAdjustForm(request.POST)
    token = request.POST.get("token", "")
    if _token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("items:detail", args=[item.sku]))
    if not form.is_valid():
        return render(request, "items/value_adjust.html", {**context, "form": form})
    data = form.cleaned_data
    amount = data["amount"] - value if data["mode"] == "total" else data["amount"]
    try:
        with transaction.atomic():
            if not _claim_token(token):
                messages.info(request, ALREADY_SAVED)
                return redirect(reverse("items:detail", args=[item.sku]))
            record_value_adjustment(item=item, date=data["date"], amount=amount, reason=data["reason"], user=request.user)
    except (StockError, PostingError) as exc:
        return render(request, "items/value_adjust.html", {**context, "form": form, **_error_messages(exc)})
    messages.success(request, f"Value adjustment saved for {item.sku}.")
    return redirect(reverse("items:detail", args=[item.sku]))


@login_required
def void_movement_view(request, pk):
    require_admin(request.user)
    movement = get_object_or_404(StockMovement.objects.select_related("item"), pk=pk)
    context = {"movement": movement, "item": movement.item, "nav_active": "items"}
    if request.method != "POST":
        form = VoidMovementForm(initial={"date": _today().isoformat()})
        return render(request, "items/void_movement.html", {**context, "form": form})
    form = VoidMovementForm(request.POST)
    if not form.is_valid():
        return render(request, "items/void_movement.html", {**context, "form": form})
    try:
        void_movement(movement, user=request.user, reason=form.cleaned_data["reason"], date=form.cleaned_data["date"],
                      allow_negative=request.POST.get("allow_negative") == "1")
    except NegativeStockWarning as exc:
        return render(request, "items/void_movement.html", {**context, "form": form, "warnings": exc.messages})
    except (StockError, PostingError) as exc:
        return render(request, "items/void_movement.html", {**context, "form": form, **_error_messages(exc)})
    messages.success(request, f"Voided the {movement.get_kind_display().lower()} for {movement.item.sku}.")
    return redirect(reverse("items:detail", args=[movement.item.sku]))


# ---- counts ----

@login_required
def count_list(request):
    counts = StockCount.objects.select_related("started_by", "category").annotate(
        line_total=Count("lines"), line_counted=Count("lines", filter=Q(lines__counted_quantity__isnull=False)))
    return render(request, "items/count_list.html", {"counts": counts, "nav_active": "counts"})


@login_required
def count_start(request):
    form = CountStartForm(_posted(request), initial={"name": f"Count {_today().isoformat()}", "scope": "ALL"})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        category = d["category"] if d["scope"] == "CATEGORY" else None
        count = count_service.start_count(name=d["name"], category=category, user=request.user)
        messages.success(request, f"Started {count.name}. Count the shelves, then enter the numbers here.")
        return redirect(reverse("items:count-enter", args=[count.pk]))
    return render(request, "items/count_start.html", {"form": form, "nav_active": "counts"})


def _open_count_or_redirect(request, pk):
    count = get_object_or_404(StockCount, pk=pk)
    if count.status != CountStatus.OPEN:
        messages.error(request, f"This count is already {count.get_status_display()}.")
        return count, redirect(reverse("items:counts"))
    return count, None


@login_required
def count_enter(request, pk):
    count, redirected = _open_count_or_redirect(request, pk)
    if redirected:
        return redirected
    q = request.GET.get("q", "").strip()
    lines = count.lines.select_related("item")
    if q:
        lines = lines.filter(Q(item__sku__icontains=q) | Q(item__name__icontains=q))
    page = Paginator(lines, PAGE_SIZE).get_page(request.GET.get("page"))
    errors = []
    if request.method == "POST":
        by_item = {l.item_id: l for l in page.object_list}
        entered: dict[int, int | None] = {}
        for key, value in request.POST.items():
            if not key.startswith("count-") or not key[6:].isdigit() or int(key[6:]) not in by_item:
                continue
            item_id, value = int(key[6:]), value.strip()
            if value == "":
                entered[item_id] = None
                continue
            try:
                entered[item_id] = int(value)
            except ValueError:
                errors.append(f"{by_item[item_id].item.sku}: quantity must be a whole number.")
        if not errors:
            try:
                count_service.enter_counts(count, entered, request.user)
            except CountError as exc:
                errors.append(str(exc))
        if not errors:
            if "review" in request.POST:
                return redirect(reverse("items:count-review", args=[count.pk]))
            messages.success(request, "Counts saved.")
            return redirect(request.get_full_path())
    for line in page.object_list:
        # After a bad entry, show what the user typed instead of what is saved.
        line.typed = request.POST.get(f"count-{line.item_id}") if errors else None
    counted = count.lines.filter(counted_quantity__isnull=False).count()
    return render(request, "items/count_enter.html", {
        "count": count, "page": page, "q": q, "errors": errors, "counted": counted, "total": count.lines.count(),
        "nav_active": "counts"})


@login_required
def count_review(request, pk):
    count = get_object_or_404(StockCount, pk=pk)
    open_count = count.status == CountStatus.OPEN
    diffs = count_service.review(count)
    flagged = [d for d in diffs if d.moved_since_start]
    form = CountPostForm(_posted(request), initial={"date": _today().isoformat()})
    context = {
        "count": count, "diffs": diffs, "flagged": flagged, "form": form, "open": open_count, "nav_active": "counts",
        "uncounted": count.lines.filter(counted_quantity__isnull=True).count(),
        "changes": sum(1 for d in diffs if d.difference != 0),
    }
    if request.method == "POST" and open_count and form.is_valid():
        try:
            movements = count_service.post_count(
                count, date=form.cleaned_data["date"], user=request.user, confirm_moved=form.cleaned_data["confirm_moved"])
        except CountNeedsConfirmation as exc:
            context["errors"] = [
                f"These items moved since the count started: {', '.join(exc.skus)}. "
                "Check them, then tick the box to post."]
        except (StockError, PostingError) as exc:
            context.update(_error_messages(exc))
        else:
            n = len(movements)
            messages.success(request, f"Posted {count.name}: {n} adjustment{'s' if n != 1 else ''}.")
            return redirect(reverse("items:counts"))
    return render(request, "items/count_review.html", context)


@login_required
@require_POST
def count_cancel(request, pk):
    count, redirected = _open_count_or_redirect(request, pk)
    if redirected:
        return redirected
    count_service.cancel_count(count, request.user)
    messages.success(request, f"Canceled {count.name}.")
    return redirect(reverse("items:counts"))


# ---- import and settings ----

MAX_UPLOAD = 2 * 1024 * 1024
IMPORT_SESSION_KEY = "items_import_csv"


def _import_page(request, **extra):
    return render(request, "items/import_upload.html", {"headers": import_service.HEADERS, "nav_active": "items", **extra})


@login_required
def import_items(request):
    require_admin(request.user)
    if request.method != "POST":
        return _import_page(request)
    upload = request.FILES.get("file")
    if upload is None:
        return _import_page(request, errors=["Choose a CSV file to upload."])
    if upload.size > MAX_UPLOAD:
        return _import_page(request, errors=["The file is too large (2 MB limit)."])
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _import_page(request, errors=["The file isn't UTF-8 text. Save it as CSV (UTF-8) and try again."])
    preview = import_service.parse_items_csv(text)
    request.session[IMPORT_SESSION_KEY] = text
    stock_rows = [r for r in preview.rows if r.item_type == ItemType.STOCK]
    opening = [r for r in preview.rows if r.opening_quantity]
    summary = {
        "items": len(preview.rows), "stock_items": len(stock_rows), "with_opening": len(opening),
        "opening_value": sum((r.opening_quantity * r.opening_unit_cost for r in opening), Decimal("0")).quantize(Decimal("0.01")),
    }
    return render(request, "items/import_preview.html", {
        "preview": preview, "summary": summary, "filename": upload.name, "nav_active": "items"})


@login_required
@require_POST
def import_confirm(request):
    require_admin(request.user)
    text = request.session.get(IMPORT_SESSION_KEY)
    if not text:
        messages.error(request, "Upload a file first.")
        return redirect(reverse("items:import"))
    preview = import_service.parse_items_csv(text)  # parsed again so a stale preview can't post
    if preview.errors:
        request.session.pop(IMPORT_SESSION_KEY, None)
        return render(request, "items/import_preview.html", {
            "preview": preview, "summary": None, "filename": "", "nav_active": "items"})
    try:
        items = import_service.apply_import(preview, user=request.user)
    except (StockError, PostingError) as exc:
        return _import_page(request, **_error_messages(exc))
    request.session.pop(IMPORT_SESSION_KEY, None)
    messages.success(request, f"Imported {len(items)} item{'s' if len(items) != 1 else ''}.")
    return redirect(reverse("items:list"))


@login_required
def inventory_settings(request):
    require_admin(request.user)
    form = SettingsForm(_posted(request), instance=InventorySettings.load())
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect(reverse("items:settings"))
    return render(request, "items/settings.html", {"form": form, "nav_active": "settings"})
