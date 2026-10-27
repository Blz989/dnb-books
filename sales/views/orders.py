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

from contacts.models import Customer
from items.errors import StockError
from items.models import Item
from items.stock import on_hand
from ledger.errors import PostingError

from ..balances import invoice_total
from ..errors import SalesError
from ..forms import OrderHeaderForm
from ..models import OrderStatus, SalesOrder
from ..posting.orders import (
    OrderLineSpec, close_order, create_order, invoiced_quantity, remaining, reopen_order, update_order,
)
from . import common

PAGE_SIZE = 50
ALREADY_SAVED = "This order was already saved."


def order_or_404(number: str) -> SalesOrder:
    order = SalesOrder.objects.filter(number=number).select_related("customer").first()
    if order is None:
        raise Http404("No sales order has that number.")
    return order


def _sku_options():
    return Item.objects.filter(is_active=True).order_by("sku").values("sku", "name")[:1000]


@login_required
def order_list(request):
    orders = SalesOrder.objects.select_related("customer")
    status = request.GET.get("status", "")
    if status in OrderStatus.values:
        orders = orders.filter(status=status)
    customer = request.GET.get("customer", "")
    if customer.isdigit():
        orders = orders.filter(customer_id=customer)
    q = request.GET.get("q", "").strip()
    if q:
        orders = orders.filter(Q(number__icontains=q) | Q(customer__name__icontains=q))
    page = Paginator(orders, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = []
    for order in page:
        left = sum(max(remaining(l), 0) for l in order.lines.all()) if order.status == OrderStatus.OPEN else 0
        rows.append({"order": order, "left": left})
    qs = "&".join(f"{k}={v}" for k, v in (("status", status), ("customer", customer), ("q", q)) if v)
    return render(request, "sales/order_list.html", {
        "rows": rows, "page": page, "status": status, "customer": customer, "q": q, "querystring": qs,
        "customers": Customer.objects.all(), "nav_active": "orders",
    })


def _form_context(request, form, rows, token, order=None, **extra):
    return {
        "form": form, "rows": rows, "token": token, "order": order, "shown": common.shown_count(rows),
        "sku_options": _sku_options(), "nav_active": "orders", **extra,
    }


def _specs(parsed):
    return [
        OrderLineSpec(
            p["item"], p["quantity"], p["price"], description=p["description"], fulfillment=p["fulfillment"],
            drop_ship_vendor=p["vendor"], line_id=int(p["ref"]) if p["ref"].isdigit() else None,
        )
        for p in parsed
    ]


@login_required
def order_new(request):
    if request.method != "POST":
        initial = {"date": timezone.localdate().isoformat()}
        if request.GET.get("customer", "").isdigit():
            initial["customer"] = request.GET["customer"]
        form = OrderHeaderForm(initial=initial)
        return render(request, "sales/order_form.html", _form_context(request, form, common.compact([]), common.new_token()))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("sales:order-list"))
    form = OrderHeaderForm(request.POST)
    rows = common.compact(common.read_rows(request.POST))
    parsed, errors = common.parse_rows(rows)
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, ALREADY_SAVED)
                    return redirect(reverse("sales:order-list"))
                order = create_order(
                    customer=d["customer"], date=d["date"], lines=_specs(parsed), user=request.user,
                    ship_to=d["ship_to"] or None, notes=d["notes"])
        except (SalesError, PostingError, StockError) as exc:
            return render(request, "sales/order_form.html", _form_context(request, form, rows, token, **common.error_context(exc)))
        messages.success(request, f"Saved {order.number}.")
        return redirect(reverse("sales:order-detail", args=[order.number]))
    return render(request, "sales/order_form.html", _form_context(request, form, rows, token, errors=errors))


@login_required
def order_edit(request, number):
    order = order_or_404(number)
    if request.method != "POST":
        form = OrderHeaderForm(initial={
            "customer": order.customer_id, "date": order.date.isoformat(), "ship_to": order.ship_to, "notes": order.notes,
        }, all_customers=True)
        rows = common.rows_from_lines(order.lines.select_related("item"))
        return render(request, "sales/order_form.html", _form_context(request, form, rows, common.new_token(), order))
    form = OrderHeaderForm(request.POST, all_customers=True)
    rows = common.compact(common.read_rows(request.POST))
    parsed, errors = common.parse_rows(rows)
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            if d["customer"].pk != order.customer_id:
                raise SalesError("The customer on an order can't be changed.")
            update_order(
                order, lines=_specs(parsed), user=request.user, date=d["date"],
                ship_to=d["ship_to"] or order.customer.ship_to_address, notes=d["notes"])
        except (SalesError, PostingError, StockError) as exc:
            return render(request, "sales/order_form.html", _form_context(
                request, form, rows, request.POST.get("token", ""), order, **common.error_context(exc)))
        messages.success(request, f"Saved {order.number}.")
        return redirect(reverse("sales:order-detail", args=[order.number]))
    return render(request, "sales/order_form.html", _form_context(
        request, form, rows, request.POST.get("token", ""), order, errors=errors))


@login_required
def order_detail(request, number):
    order = order_or_404(number)
    lines = []
    for line in order.lines.select_related("item"):
        lines.append({
            "line": line, "invoiced": invoiced_quantity(line), "left": remaining(line),
            "on_hand": on_hand(line.item) if line.item.is_stock else None,
        })
    invoices = [{"invoice": i, "total": invoice_total(i)} for i in order.invoices.order_by("date", "id")]
    return render(request, "sales/order_detail.html", {
        "order": order, "lines": lines, "invoices": invoices,
        "can_invoice": order.status == OrderStatus.OPEN and any(l["left"] > 0 for l in lines),
        "nav_active": "orders",
    })


@login_required
@require_POST
def order_close(request, number):
    order = order_or_404(number)
    if order.status == OrderStatus.OPEN:
        close_order(order, request.user)
        messages.success(request, f"{order.number} is closed.")
    else:
        reopen_order(order, request.user)
        messages.success(request, f"{order.number} is open again.")
    return redirect(reverse("sales:order-detail", args=[order.number]))
