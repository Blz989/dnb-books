from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from contacts.models import Customer
from core.roles import is_admin, require_admin
from items.errors import NegativeStockWarning, StockError
from items.models import Item
from items.stock import on_hand
from ledger.errors import PostingError
from ledger.forms import date_field

from ..balances import (
    _TOLERANCE as UNPAID_TOLERANCE, invoice_applied, invoice_balance, invoice_total, invoices_with_balances,
    invoices_with_open_balance,
)
from ..errors import SalesError
from ..forms import InvoiceAdjustmentForm, InvoiceHeaderForm, ShipmentHeaderForm, VoidDocForm
from ..models import (
    CreditApplication, DocStatus, Fulfillment, Invoice, OrderStatus, PaymentApplication, SalesOrderLine, SalesSettings,
)
from ..pdf import invoice_pdf
from ..posting.deletion import delete_invoice
from ..posting.invoices import (
    InvoiceLineSpec, create_invoice, edit_invoice, invoice_adjustment, invoice_from_order, void_invoice,
)
from ..posting.orders import remaining
from ..posting.receipts import remove_application
from . import common
from .orders import order_or_404

PAGE_SIZE = 50
ALREADY_SAVED = "This invoice was already saved."


def invoice_or_404(number: str) -> Invoice:
    invoice = Invoice.objects.filter(number=number).select_related("customer", "sales_order", "journal_entry").first()
    if invoice is None:
        raise Http404("No invoice has that number.")
    return invoice


def _sku_options():
    return Item.objects.filter(is_active=True).order_by("sku").values("sku", "name")[:1000]


def _today():
    return timezone.localdate()


@login_required
def invoice_list(request):
    unpaid = request.GET.get("unpaid") == "1"
    # Unpaid: the SQL balance prefilter from sales.balances, so no invoice is loaded just to find it settled.
    invoices = invoices_with_open_balance(None).filter(_gap__gt=UNPAID_TOLERANCE) if unpaid else invoices_with_balances()
    invoices = invoices.filter(deleted_at__isnull=True)
    status = request.GET.get("status", "")
    if status in DocStatus.values:
        invoices = invoices.filter(status=status)
    customer = request.GET.get("customer", "")
    if customer.isdigit():
        invoices = invoices.filter(customer_id=customer)
    q = request.GET.get("q", "").strip()
    if q:
        invoices = invoices.filter(Q(number__icontains=q) | Q(customer__name__icontains=q))
    overdue = request.GET.get("overdue") == "1"
    if overdue:
        invoices = invoices.filter(status=DocStatus.OPEN, due_date__lt=_today())
    page = Paginator(invoices, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = []
    for invoice in page:
        balance = invoice_balance(invoice)
        rows.append({
            "invoice": invoice, "total": invoice_total(invoice), "balance": balance,
            "overdue": invoice.status == DocStatus.OPEN and balance > 0 and invoice.due_date < _today(),
        })
    qs = "&".join(f"{k}={v}" for k, v in (("status", status), ("customer", customer), ("q", q), ("overdue", "1" if overdue else ""),
                                              ("unpaid", "1" if unpaid else "")) if v)
    return render(request, "sales/invoice_list.html", {
        "rows": rows, "page": page, "status": status, "customer": customer, "q": q, "overdue": overdue, "unpaid": unpaid,
        "querystring": qs, "customers": Customer.objects.all(), "nav_active": "invoices",
    })


def _specs(parsed, order=None):
    specs = []
    for p in parsed:
        so_line = None
        if p["ref"].isdigit() and order is not None:
            so_line = SalesOrderLine.objects.filter(pk=int(p["ref"]), order=order).first()
        specs.append(InvoiceLineSpec(
            p["item"], p["quantity"], p["price"], description=p["description"], fulfillment=p["fulfillment"],
            drop_ship_vendor=p["vendor"], so_line=so_line))
    return specs


def _form_context(form, rows, token, invoice=None, adj_form=None, **extra):
    return {
        "form": form, "adj_form": adj_form or InvoiceAdjustmentForm(), "rows": rows, "token": token, "invoice": invoice, "shown": common.shown_count(rows),
        "sku_options": _sku_options(), "nav_active": "invoices", **extra,
    }


def _confirm(request, title, **extra):
    return render(request, "sales/invoice_confirm_negative.html", {
        "hidden": common.hidden_fields(request.POST), "action": request.path, "title": title,
        "warnings": extra.pop("warnings"), "nav_active": "invoices", **extra})


@login_required
def invoice_new(request):
    config = SalesSettings.load()
    if request.method != "POST":
        initial = {"date": _today().isoformat(), "notes": config.default_invoice_notes}
        if request.GET.get("customer", "").isdigit():
            initial["customer"] = request.GET["customer"]
        form = InvoiceHeaderForm(initial=initial)
        return render(request, "sales/invoice_form.html", _form_context(form, common.compact([]), common.new_token()))
    adj_form = InvoiceAdjustmentForm(request.POST)
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("sales:invoice-list"))
    form = InvoiceHeaderForm(request.POST)
    rows = common.compact(common.read_rows(request.POST))
    if request.POST.get("back"):
        return render(request, "sales/invoice_form.html", _form_context(form, rows, token, adj_form=adj_form))
    parsed, errors = common.parse_rows(rows)
    if form.is_valid() and adj_form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, ALREADY_SAVED)
                    return redirect(reverse("sales:invoice-list"))
                invoice = create_invoice(
                    customer=d["customer"], date=d["date"], lines=_specs(parsed), user=request.user,
                    terms=d["terms"] or None, ship_to=d["ship_to"] or None, notes=d["notes"],
                    adjustment=adj_form.spec(), allow_negative=request.POST.get("allow_negative") == "1")
        except NegativeStockWarning as warning:
            return _confirm(request, "Confirm invoice", warnings=warning.messages)
        except (SalesError, PostingError, StockError) as exc:
            return render(request, "sales/invoice_form.html", _form_context(
                form, rows, token, adj_form=adj_form, **common.error_context(exc)))
        messages.success(request, f"Saved {invoice.number}.")
        return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
    return render(request, "sales/invoice_form.html", _form_context(form, rows, token, adj_form=adj_form, errors=errors))


@login_required
def invoice_edit(request, number):
    invoice = invoice_or_404(number)
    if invoice.status == DocStatus.VOID:
        messages.error(request, f"{invoice.number} is void and can't be edited.")
        return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
    if request.method != "POST":
        form = InvoiceHeaderForm(editing=True, initial={
            "customer": invoice.customer_id, "date": invoice.date.isoformat(), "terms": invoice.terms,
            "ship_to": invoice.ship_to, "notes": invoice.notes})
        rows = common.rows_from_lines(
            invoice.lines.select_related("item").filter(is_adjustment=False), ref=lambda l: l.so_line_id)
        adj_form = InvoiceAdjustmentForm(initial=InvoiceAdjustmentForm.initial_for(invoice_adjustment(invoice)))
        return render(request, "sales/invoice_form.html", _form_context(
            form, rows, common.new_token(), invoice, adj_form=adj_form))
    adj_form = InvoiceAdjustmentForm(request.POST)
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "These changes were already saved.")
        return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
    form = InvoiceHeaderForm(request.POST, editing=True)
    rows = common.compact(common.read_rows(request.POST))
    if request.POST.get("back"):
        return render(request, "sales/invoice_form.html", _form_context(form, rows, token, invoice, adj_form=adj_form))
    parsed, errors = common.parse_rows(rows)
    if form.is_valid() and adj_form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "These changes were already saved.")
                    return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
                edit_invoice(
                    invoice, user=request.user, date=d["date"], lines=_specs(parsed, invoice.sales_order),
                    terms=d["terms"] or None, ship_to=d["ship_to"], notes=d["notes"], customer=d["customer"],
                    reason=d["reason"], adjustment=adj_form.spec(),
                    allow_negative=request.POST.get("allow_negative") == "1")
        except NegativeStockWarning as warning:
            return _confirm(request, "Confirm changes", warnings=warning.messages, invoice=invoice)
        except (SalesError, PostingError, StockError) as exc:
            return render(request, "sales/invoice_form.html", _form_context(
                form, rows, token, invoice, adj_form=adj_form, **common.error_context(exc)))
        messages.success(request, f"Saved {invoice.number}.")
        return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
    return render(request, "sales/invoice_form.html", _form_context(
        form, rows, token, invoice, adj_form=adj_form, errors=errors))


def _shipment_rows(order, post=None):
    rows = []
    for line in order.lines.select_related("item"):
        left = remaining(line)
        if post is None:
            value = str(left) if left > 0 else "0"
        else:
            value = post.get(f"qty-{line.pk}", "").strip()
        rows.append({
            "line": line, "left": left, "value": value,
            "on_hand": on_hand(line.item) if line.item.is_stock and line.fulfillment == Fulfillment.STOCK else None,
        })
    return rows


@login_required
def order_invoice(request, number):
    order = order_or_404(number)
    config = SalesSettings.load()
    context = {"order": order, "nav_active": "orders"}
    if order.status == OrderStatus.CLOSED:
        messages.error(request, f"{order.number} is closed. Reopen it to invoice it.")
        return redirect(reverse("sales:order-detail", args=[order.number]))
    if request.method != "POST":
        form = ShipmentHeaderForm(initial={
            "date": _today().isoformat(), "ship_to": order.ship_to, "notes": config.default_invoice_notes})
        return render(request, "sales/invoice_from_order.html", {
            **context, "form": form, "adj_form": InvoiceAdjustmentForm(), "rows": _shipment_rows(order),
            "token": common.new_token()})
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("sales:order-detail", args=[order.number]))
    form = ShipmentHeaderForm(request.POST)
    rows = _shipment_rows(order, request.POST)
    adj_form = InvoiceAdjustmentForm(request.POST)
    page = {**context, "form": form, "adj_form": adj_form, "rows": rows, "token": token}
    if request.POST.get("back"):
        return render(request, "sales/invoice_from_order.html", page)
    quantities, errors = {}, []
    for n, r in enumerate(rows, 1):
        raw = r["value"] or "0"
        try:
            quantities[r["line"].pk] = int(raw)
        except ValueError:
            errors.append(f"Line {n}: enter a quantity above zero, or 0 to skip it.")
    if form.is_valid() and adj_form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, ALREADY_SAVED)
                    return redirect(reverse("sales:order-detail", args=[order.number]))
                invoice = invoice_from_order(
                    order, date=d["date"], quantities=quantities, user=request.user, ship_to=d["ship_to"],
                    notes=d["notes"], terms=d["terms"] or None, adjustment=adj_form.spec(), allow_negative=request.POST.get("allow_negative") == "1")
        except NegativeStockWarning as warning:
            return _confirm(request, "Confirm invoice", warnings=warning.messages, order=order)
        except (SalesError, PostingError, StockError) as exc:
            return render(request, "sales/invoice_from_order.html", {**page, **common.error_context(exc)})
        messages.success(request, f"Saved {invoice.number}.")
        return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
    return render(request, "sales/invoice_from_order.html", {**page, "errors": errors})


@login_required
def invoice_detail(request, number):
    invoice = invoice_or_404(number)
    all_lines = list(invoice.lines.select_related("item", "stock_movement__journal_entry"))
    lines = [l for l in all_lines if not l.is_adjustment]
    adjustment = next((l for l in all_lines if l.is_adjustment), None)
    total = invoice_total(invoice)
    payment_apps = list(invoice.payment_applications.select_related("payment").order_by("applied_on", "id"))
    credit_apps = list(invoice.credit_applications.select_related("credit_note").order_by("applied_on", "id"))
    entries = [("Invoice", invoice.journal_entry)] if invoice.journal_entry_id else []
    for line in lines:
        m = line.stock_movement
        if m and m.journal_entry_id:
            entries.append((f"Cost of {line.item.sku}", m.journal_entry))
    return render(request, "sales/invoice_detail.html", {
        "invoice": invoice, "lines": lines, "total": total, "adjustment": adjustment,
        "subtotal": total - (adjustment.amount if adjustment else 0), "applied": invoice_applied(invoice),
        "balance": invoice_balance(invoice), "payment_apps": payment_apps, "credit_apps": credit_apps,
        "revisions": invoice.revisions.all(), "entries": entries, "is_void": invoice.status == DocStatus.VOID,
        "is_admin": is_admin(request.user), "nav_active": "invoices",
    })


@login_required
def invoice_revision(request, number, revision):
    invoice = invoice_or_404(number)
    rev = get_object_or_404(invoice.revisions, revision=revision)
    snap = rev.snapshot
    customer = Customer.objects.filter(pk=snap["customer_id"]).first()
    return render(request, "sales/invoice_revision.html", {
        "invoice": invoice, "rev": rev, "snap": snap, "customer": customer, "nav_active": "invoices"})


@login_required
def invoice_void(request, number):
    require_admin(request.user)
    invoice = invoice_or_404(number)
    context = {"invoice": invoice, "nav_active": "invoices"}
    if request.method != "POST":
        form = VoidDocForm(initial={"date": invoice.date.isoformat()})
        return render(request, "sales/invoice_void.html", {**context, "form": form})
    form = VoidDocForm(request.POST)
    if form.is_valid():
        try:
            void_invoice(invoice, user=request.user, reason=form.cleaned_data["reason"], date=form.cleaned_data["date"])
        except (SalesError, PostingError, StockError) as exc:
            return render(request, "sales/invoice_void.html", {**context, "form": form, **common.error_context(exc)})
        messages.success(request, f"{invoice.number} was voided.")
        return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
    return render(request, "sales/invoice_void.html", {**context, "form": form})


@login_required
def invoice_delete(request, number):
    require_admin(request.user)
    invoice = invoice_or_404(number)
    back = reverse("sales:invoice-detail", args=[invoice.number])
    errors = []
    if request.method == "POST":
        try:
            delete_invoice(invoice, user=request.user)
        except SalesError as exc:
            errors = [str(exc)]
        else:
            messages.success(request, f"{invoice.number} was deleted.")
            return redirect(reverse("sales:invoice-list"))
    blocker = None
    if invoice.deleted_at:
        blocker = f"{invoice.number} is already deleted."
    elif invoice.status != DocStatus.VOID:
        blocker = "Void this invoice first. Only a voided invoice can be deleted."
    return render(request, "core/delete_confirm.html", {
        "doc": invoice, "who": invoice.customer.name, "kind": "invoice", "back": back, "nav_active": "invoices",
        "intro": f"Delete voided invoice {invoice.number}? It leaves the invoice list. Its ledger entries were "
                 "already reversed by the void, so no balances change.",
        "blocker": blocker, "errors": errors or ([blocker] if blocker else []),
    })


@login_required
@require_POST
def application_remove(request, number, kind, pk):
    invoice = invoice_or_404(number)
    model = {"payment": PaymentApplication, "credit": CreditApplication}.get(kind)
    if model is None:
        raise Http404
    app = get_object_or_404(model, pk=pk, invoice=invoice)
    try:
        remove_application(app, date=max(_today(), app.applied_on), user=request.user)
    except (SalesError, PostingError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Application removed.")
    return redirect(reverse("sales:invoice-detail", args=[invoice.number]))


@login_required
def invoice_pdf_view(request, number):
    invoice = get_object_or_404(Invoice.objects.select_related("customer", "sales_order"), number=number)
    response = HttpResponse(invoice_pdf(invoice), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{invoice.number}.pdf"'
    return response


@login_required
def packing_slip_view(request, number):
    from ..pdf import packing_slip_pdf

    invoice = get_object_or_404(Invoice.objects.select_related("customer", "sales_order"), number=number)
    response = HttpResponse(packing_slip_pdf(invoice), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{invoice.number}-packing-slip.pdf"'
    return response


class WriteOffForm(forms.Form):
    date = date_field("Write-off date")
    amount = forms.DecimalField(label="Amount", max_digits=14, decimal_places=2,
                                error_messages={"required": "Enter an amount above zero.",
                                                "invalid": "Enter an amount like 125.00."})
    reason = forms.CharField(label="Reason", required=False, max_length=150,
                             help_text="Shown on the credit note, for example \"Customer closed its business\".")


@login_required
def invoice_write_off(request, number):
    """Admin only: write the balance (or part of it) off to the bad debt account. Spec 5."""
    from ..posting.writeoff import write_off

    require_admin(request.user)
    invoice = invoice_or_404(number)
    balance = invoice_balance(invoice)
    context = {"invoice": invoice, "balance": balance, "nav_active": "invoices",
               "account": SalesSettings.load().bad_debt_account}
    if context["account"] is None:
        context["errors"] = ["Set the bad debt account in Settings, Sales first."]
        context["settings_link"] = "/sales/settings/"
    if request.method != "POST":
        form = WriteOffForm(initial={"date": timezone.localdate().isoformat(), "amount": f"{balance:.2f}"})
        return render(request, "sales/invoice_write_off.html", {**context, "form": form})
    form = WriteOffForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            note = write_off(invoice, date=d["date"], amount=d["amount"], reason=d["reason"], user=request.user)
        except (SalesError, PostingError) as exc:
            return render(request, "sales/invoice_write_off.html", {**context, "form": form, **common.error_context(exc)})
        messages.success(request, f"{d['amount']:,.2f} of {invoice.number} was written off as {note.number}.")
        return redirect(reverse("sales:invoice-detail", args=[invoice.number]))
    return render(request, "sales/invoice_write_off.html", {**context, "form": form})
