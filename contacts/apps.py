from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ContactsConfig(AppConfig):
    name = "contacts"

    def ready(self):
        from .terms import ensure_default_terms

        post_migrate.connect(ensure_default_terms, sender=self, dispatch_uid="contacts.default_terms")
