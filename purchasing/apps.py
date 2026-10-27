from django.apps import AppConfig


class PurchasingConfig(AppConfig):
    name = "purchasing"

    def ready(self):
        from sales.models import DocType
        from sales.numbering import register_document

        from .models import Bill, PurchaseOrder, VendorCredit, VendorPayment

        register_document(DocType.PO, PurchaseOrder, "PO-", 5)
        register_document(DocType.BILL, Bill, "BILL-", 5)
        register_document(DocType.VP, VendorPayment, "VP-", 5)
        register_document(DocType.VC, VendorCredit, "VC-", 5)

        from sales import hooks

        from .dropship import on_invoice_changed

        hooks.register_invoice_changed(on_invoice_changed)
