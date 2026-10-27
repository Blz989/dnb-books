from django.apps import AppConfig


class SalesConfig(AppConfig):
    name = "sales"

    def ready(self):
        from ledger.periods import register_close_check

        from .checks import close_check

        register_close_check(close_check)
