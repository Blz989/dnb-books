from django.apps import AppConfig


class BankingConfig(AppConfig):
    name = "banking"

    def ready(self):
        from ledger.locks import register_lock_check, register_void_hook

        from .inbox import release_matches
        from .reconcile import entry_lock

        register_void_hook(release_matches)
        register_lock_check(entry_lock)
