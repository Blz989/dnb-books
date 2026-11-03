from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.text import slugify

from contacts.models import Customer
from ledger.forms import RangeForm
from ledger.views import _data

from .. import statements
from ..pdf import statement_pdf

DEFAULT_DAYS = 90


def _customer_or_404(pk) -> Customer:
    customer = Customer.objects.filter(pk=pk).first()
    if customer is None:
        raise Http404("No customer has that id.")
    return customer


def _statement(request, pk):
    customer = _customer_or_404(pk)
    today = timezone.localdate()
    form = RangeForm(_data(request, {"start": (today - timedelta(days=DEFAULT_DAYS)).isoformat(), "end": today.isoformat()}))
    statement = None
    if form.is_valid():
        statement = statements.build_statement(customer, form.cleaned_data["start"], form.cleaned_data["end"])
    return customer, form, statement


@login_required
def statement(request, pk):
    customer, form, result = _statement(request, pk)
    return render(request, "sales/statement.html", {
        "customer": customer, "form": form, "statement": result, "nav_active": "customers",
    })


@login_required
def statement_pdf_view(request, pk):
    customer, form, result = _statement(request, pk)
    if result is None:
        return render(request, "sales/statement.html", {
            "customer": customer, "form": form, "statement": None, "nav_active": "customers"}, status=400)
    response = HttpResponse(statement_pdf(result), content_type="application/pdf")
    name = slugify(customer.name) or "customer"
    response["Content-Disposition"] = f'inline; filename="Statement-{name}-{result.end.isoformat()}.pdf"'
    return response
