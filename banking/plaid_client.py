"""The only module that imports plaid. Tests replace get_client() with a fake built from recorded responses.

Plaid reports money out as positive. Everything leaving this module uses banking's sign: money in is positive.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)


class PlaidError(Exception):
    """A Plaid API error. The message comes from Plaid, with any secrets scrubbed out."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlaidAccount:
    account_id: str
    name: str
    mask: str
    type: str
    subtype: str


@dataclass(frozen=True)
class PlaidTxn:
    transaction_id: str
    account_id: str
    date: date_type
    name: str
    amount: Decimal
    pending: bool
    check_number: str = ""


@dataclass(frozen=True)
class SyncPage:
    added: list[PlaidTxn] = field(default_factory=list)
    modified: list[PlaidTxn] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    next_cursor: str = ""
    has_more: bool = False


def _to_date(value) -> date_type:
    return value if isinstance(value, date_type) else date_type.fromisoformat(str(value))


def parse_txn(raw: dict) -> PlaidTxn:
    return PlaidTxn(
        transaction_id=raw["transaction_id"],
        account_id=raw["account_id"],
        date=_to_date(raw["date"]),
        name=raw.get("name") or raw.get("merchant_name") or "",
        amount=-Decimal(str(raw["amount"])),
        pending=bool(raw.get("pending")),
        check_number=raw.get("check_number") or "",
    )


def parse_sync_response(payload: dict) -> SyncPage:
    return SyncPage(
        added=[parse_txn(t) for t in payload.get("added", [])],
        modified=[parse_txn(t) for t in payload.get("modified", [])],
        removed=[r["transaction_id"] for r in payload.get("removed", [])],
        next_cursor=payload.get("next_cursor") or "",
        has_more=bool(payload.get("has_more")),
    )


def parse_accounts(payload: dict) -> list[PlaidAccount]:
    return [
        PlaidAccount(
            account_id=a["account_id"], name=a.get("name") or "", mask=a.get("mask") or "",
            type=str(a.get("type") or ""), subtype=str(a.get("subtype") or ""),
        )
        for a in payload.get("accounts", [])
    ]


class PlaidClient:
    def __init__(self, client_id: str, secret: str, env: str):
        import plaid
        from plaid.api import plaid_api

        host = plaid.Environment.Production if env == "production" else plaid.Environment.Sandbox
        configuration = plaid.Configuration(host=host, api_key={"clientId": client_id, "secret": secret})
        self._api = plaid_api.PlaidApi(plaid.ApiClient(configuration))
        self._secrets = [s for s in (secret,) if s]

    # -- errors --
    def _scrub(self, text: str, extra: tuple[str, ...] = ()) -> str:
        for secret in (*self._secrets, *extra):
            if secret:
                text = text.replace(secret, "[hidden]")
        return text

    def _error(self, exc: Exception, *secrets: str) -> PlaidError:
        import plaid

        if isinstance(exc, plaid.ApiException):
            try:
                body = json.loads(exc.body or "{}")
            except ValueError:
                body = {}
            code = body.get("error_code") or "API_ERROR"
            message = body.get("error_message") or "Plaid could not complete the request."
        else:
            code, message = "CONNECTION_ERROR", "Could not reach Plaid."
        return PlaidError(code, self._scrub(message, secrets))

    def _call(self, fn, request, *secrets: str):
        try:
            return fn(request).to_dict()
        except Exception as exc:  # noqa: BLE001 - re-raised as PlaidError without the request details
            raise self._error(exc, *secrets) from None

    # -- API --
    def create_link_token(self, user_id: str, access_token: str | None = None) -> str:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.link_token_transactions import LinkTokenTransactions
        from plaid.model.products import Products

        fields = dict(
            client_name="DNB Books", language="en", country_codes=[CountryCode("US")],
            user=LinkTokenCreateRequestUser(client_user_id=str(user_id)),
        )
        if access_token:
            fields["access_token"] = access_token  # update mode: no products
        else:
            fields["products"] = [Products("transactions")]
            fields["transactions"] = LinkTokenTransactions(days_requested=365)  # a year of history on first link
        data = self._call(self._api.link_token_create, LinkTokenCreateRequest(**fields), access_token or "")
        return data["link_token"]

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

        data = self._call(
            self._api.item_public_token_exchange, ItemPublicTokenExchangeRequest(public_token=public_token),
            public_token)
        return data["access_token"], data["item_id"]

    def get_accounts(self, access_token: str) -> list[PlaidAccount]:
        from plaid.model.accounts_get_request import AccountsGetRequest

        return parse_accounts(
            self._call(self._api.accounts_get, AccountsGetRequest(access_token=access_token), access_token))

    def transactions_sync(self, access_token: str, cursor: str | None) -> SyncPage:
        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        fields = {"access_token": access_token}
        if cursor:
            fields["cursor"] = cursor
        return parse_sync_response(
            self._call(self._api.transactions_sync, TransactionsSyncRequest(**fields), access_token))

    def create_sandbox_public_token(self, institution_id: str) -> str:
        from plaid.model.products import Products
        from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest

        data = self._call(
            self._api.sandbox_public_token_create,
            SandboxPublicTokenCreateRequest(institution_id=institution_id, initial_products=[Products("transactions")]))
        return data["public_token"]


def plaid_configured() -> bool:
    return bool(settings.PLAID_CLIENT_ID and settings.PLAID_SECRET)


def get_client() -> PlaidClient:
    return PlaidClient(settings.PLAID_CLIENT_ID, settings.PLAID_SECRET, settings.PLAID_ENV)
