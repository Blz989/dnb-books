from django.apps import AppConfig


class SalesConfig(AppConfig):
    name = "sales"

    def ready(self):
        from ledger.locks import register_document_source
        from ledger.periods import register_close_check

        from .checks import close_check

        register_close_check(close_check)
        for source_type, noun in (
            ("sales.invoice", "invoice"), ("sales.payment", "customer payment"),
            ("sales.credit_note", "credit note"), ("sales.refund", "refund"), ("sales.deposit", "deposit"),
        ):
            register_document_source(source_type, noun)
