from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from contacts.models import Vendor
from items.errors import StockError
from ledger.errors import PostingError
from sales.models import DocStatus, OrderStatus, SalesOrder

from ..balances import bill_total
from ..errors import PurchasingError, UnresolvedVendors
from ..forms import PurchaseOrderHeaderForm
from ..models import PurchaseOrder, PurchaseOrderLine
from ..posting.orders import (
    POLineSpec, billed_quantity, close_po, create_drop_ship_pos, create_po, drop_ship_candidates, po_remaining,
    reopen_po, update_po,
)
from . import common

PAGE_SIZE = 50
ALREADY_SAVED = "This purchase order was already saved."


def po_or_404(number: str) -> PurchaseOrder:
    po = PurchaseOrder.objects.filter(number=number).select_related("vendor", "sales_order").first()
    if po is None:
        raise Http404("No purchase order has that number.")
    return po


@login_required
def po_list(request):
    orders = PurchaseOrder.objects.select_related("vendor")
    status = request.GET.get("status", "")
    if status in OrderStatus.values:
        orders = orders.filter(status=status)
    vendor = request.GET.get("vendor", "")
    if vendor.isdigit():
        orders = orders.filter(vendor_id=vendor)
    q = request.GET.get("q", "").strip()
    if q:
        orders = orders.filter(Q(number__icontains=q) | Q(vendor__name__icontains=q))
    page = Paginator(orders, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = []
    for order in page:
        left = sum(max(po_remaining(l), 0) for l in order.lines.all()) if order.status == OrderStatus.OPEN else 0
        rows.append({"order": order, "left": left})
    qs = "&".join(f"{k}={v}" for k, v in (("status", status), ("vendor", vendor), ("q", q)) if v)
    return render(request, "purchasing/po_list.html", {
        "rows": rows, "page": page, "status": status, "vendor": vendor, "q": q, "querystring": qs,
        "vendors": Vendor.objects.all(), "nav_active": "purchase-orders",
    })


def _form_context(form, rows, token, order=None, **extra):
    return {
        "form": form, "rows": rows, "token": token, "order": order, "shown": common.shown_count(rows),
        "sku_options": common.sku_options(), "nav_active": "purchase-orders", **extra,
    }


def _specs(parsed):
    return [
        POLineSpec(p["item"], p["quantity"], p["price"], description=p["description"],
                   line_id=int(p["ref"]) if p["ref"].isdigit() else None)
        for p in parsed
    ]


@login_required
def po_new(request):
    if request.method != "POST":
        initial = {"date": timezone.localdate().isoformat()}
        if request.GET.get("vendor", "").isdigit():
            initial["vendor"] = request.GET["vendor"]
        form = PurchaseOrderHeaderForm(initial=initial)
        return render(request, "purchasing/po_form.html", _form_context(form, common.compact([]), common.new_token()))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("purchasing:po-list"))
    form = PurchaseOrderHeaderForm(request.POST)
    rows = common.compact(common.read_rows(request.POST))
    parsed, errors = common.parse_item_rows(rows)
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, ALREADY_SAVED)
                    return redirect(reverse("purchasing:po-list"))
                po = create_po(
                    vendor=d["vendor"], date=d["date"], lines=_specs(parsed), user=request.user,
                    expected_date=d["expected_date"], notes=d["notes"])
        except (PurchasingError, PostingError, StockError) as exc:
            return render(request, "purchasing/po_form.html", _form_context(form, rows, token, **common.error_context(exc)))
        messages.success(request, f"Saved {po.number}.")
        return redirect(reverse("purchasing:po-detail", args=[po.number]))
    return render(request, "purchasing/po_form.html", _form_context(form, rows, token, errors=errors))


@login_required
def po_edit(request, number):
    po = po_or_404(number)
    if request.method != "POST":
        form = PurchaseOrderHeaderForm(all_vendors=True, initial={
            "vendor": po.vendor_id, "date": po.date.isoformat(),
            "expected_date": po.expected_date.isoformat() if po.expected_date else "", "notes": po.notes})
        rows = common.item_rows_from_lines(po.lines.select_related("item"))
        return render(request, "purchasing/po_form.html", _form_context(form, rows, common.new_token(), po))
    token = request.POST.get("token", "")
    form = PurchaseOrderHeaderForm(request.POST, all_vendors=True)
    rows = common.compact(common.read_rows(request.POST))
    parsed, errors = common.parse_item_rows(rows)
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            if d["vendor"].pk != po.vendor_id:
                raise PurchasingError("The vendor on a purchase order can't be changed.")
            update_po(po, lines=_specs(parsed), user=request.user, date=d["date"],
                      expected_date=d["expected_date"], notes=d["notes"])
        except (PurchasingError, PostingError, StockError) as exc:
            return render(request, "purchasing/po_form.html", _form_context(form, rows, token, po, **common.error_context(exc)))
        messages.success(request, f"Saved {po.number}.")
        return redirect(reverse("purchasing:po-detail", args=[po.number]))
    return render(request, "purchasing/po_form.html", _form_context(form, rows, token, po, errors=errors))


@login_required
def po_detail(request, number):
    po = po_or_404(number)
    lines = []
    for line in po.lines.select_related("item", "so_line"):
        lines.append({"line": line, "billed": billed_quantity(line), "left": po_remaining(line)})
    bills = [{"bill": b, "total": bill_total(b)} for b in po.bills.prefetch_related("item_lines", "expense_lines")]
    return render(request, "purchasing/po_detail.html", {
        "order": po, "lines": lines, "bills": bills,
        "can_bill": po.status == OrderStatus.OPEN and any(l["left"] > 0 for l in lines),
        "nav_active": "purchase-orders",
    })


@login_required
@require_POST
def po_close(request, number):
    po = po_or_404(number)
    if po.status == OrderStatus.OPEN:
        close_po(po, request.user)
        messages.success(request, f"{po.number} is closed.")
    else:
        reopen_po(po, request.user)
        messages.success(request, f"{po.number} is open again.")
    return redirect(reverse("purchasing:po-detail", args=[po.number]))


# ---- drop-ship POs from a sales order ----

def _drop_ship_rows(order, post=None):
    rows = []
    for name, lines in drop_ship_candidates(order).items():
        matched = Vendor.find_by_name(name)
        chosen = None
        if post is not None and post.get(f"vendor-{name}", "").isdigit():
            chosen = Vendor.objects.filter(pk=post[f"vendor-{name}"]).first()
        rows.append({"name": name, "lines": lines, "matched": matched, "chosen": chosen or matched})
    return rows


@login_required
def drop_ship_po(request, number):
    order = SalesOrder.objects.filter(number=number).select_related("customer").first()
    if order is None:
        raise Http404("No sales order has that number.")
    context = {"order": order, "nav_active": "orders"}
    if not drop_ship_candidates(order):
        messages.info(request, "Every drop-ship line on this order is already on a purchase order.")
        return redirect(reverse("sales:order-detail", args=[order.number]))
    if request.method != "POST":
        return render(request, "purchasing/drop_ship_po.html", {
            **context, "rows": _drop_ship_rows(order), "vendors": Vendor.objects.filter(is_active=True)})
    choices = {}
    for name in drop_ship_candidates(order):
        value = request.POST.get(f"vendor-{name}", "")
        vendor = Vendor.objects.filter(pk=value).first() if value.isdigit() else None
        if vendor is not None:
            choices[name] = vendor
    try:
        pos = create_drop_ship_pos(order, user=request.user, date=timezone.localdate(), vendor_choices=choices)
    except UnresolvedVendors as exc:
        return render(request, "purchasing/drop_ship_po.html", {
            **context, "rows": _drop_ship_rows(order, request.POST), "vendors": Vendor.objects.filter(is_active=True),
            "errors": [str(exc)], "unresolved": exc.unresolved})
    except (PurchasingError, PostingError) as exc:
        return render(request, "purchasing/drop_ship_po.html", {
            **context, "rows": _drop_ship_rows(order, request.POST), "vendors": Vendor.objects.filter(is_active=True),
            **common.error_context(exc)})
    messages.success(request, "Created " + ", ".join(p.number for p in pos) + ".")
    return redirect(reverse("sales:order-detail", args=[order.number]))
