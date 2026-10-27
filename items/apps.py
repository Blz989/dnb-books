from django.apps import AppConfig


class ItemsConfig(AppConfig):
    name = "items"

    def ready(self):
        from ledger.periods import register_close_check

        from .reports import close_check

        register_close_check(close_check)
