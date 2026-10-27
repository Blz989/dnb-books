"""Encrypts Plaid access tokens with DNB_FIELD_KEY (a Fernet key)."""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

MESSAGE = "Plaid tokens can't be read. Check DNB_FIELD_KEY or re-link the accounts."


class FieldKeyError(Exception):
    """The key is missing or wrong. The message is safe to show to users."""


def _fernet() -> Fernet:
    key = getattr(settings, "DNB_FIELD_KEY", "")
    if not key:
        raise FieldKeyError(MESSAGE)
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise FieldKeyError(MESSAGE) from None


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise FieldKeyError(MESSAGE) from None
