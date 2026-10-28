from django.apps import AppConfig


class HandlingConfig(AppConfig):
    name = "handling"

    def ready(self):
        from ledger.locks import register_lock_check
        from ledger.periods import register_close_check

        from .checks import close_check, run_lock

        register_lock_check(run_lock)
        register_close_check(close_check)
