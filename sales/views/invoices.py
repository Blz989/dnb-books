from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from ..models import Invoice
from ..pdf import invoice_pdf


@login_required
def invoice_pdf_view(request, number):
    invoice = get_object_or_404(Invoice.objects.select_related("customer", "sales_order"), number=number)
    response = HttpResponse(invoice_pdf(invoice), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{invoice.number}.pdf"'
    return response
