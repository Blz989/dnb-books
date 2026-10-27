from django.apps import AppConfig


class BankingConfig(AppConfig):
    name = "banking"

    def ready(self):
        from ledger.locks import register_void_hook

        from .inbox import release_matches

        register_void_hook(release_matches)
