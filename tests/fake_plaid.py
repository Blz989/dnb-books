"""A Plaid client that replays recorded responses from tests/fixtures/plaid, in order, keyed by cursor."""
import json
from pathlib import Path

from banking.plaid_client import PlaidError, parse_accounts, parse_sync_response

FIXTURES = Path(__file__).parent / "fixtures" / "plaid"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakePlaidClient:
    def __init__(self, pages=("sync_page1.json", "sync_page2.json"), error: PlaidError | None = None):
        # Page i is returned for the cursor that page i-1 handed out (None for the first).
        self._payloads = [load(p) for p in pages]
        self._cursors = [None] + [p["next_cursor"] for p in self._payloads[:-1]]
        self.error = error
        self.calls: list[tuple[str, str | None]] = []
        self.link_updates: list[str | None] = []

    def create_link_token(self, user_id, access_token=None):
        self.link_updates.append(access_token)
        return "link-sandbox-fake"

    def exchange_public_token(self, public_token):
        return "access-sandbox-fake-token", "item-fake-1"

    def get_accounts(self, access_token):
        return parse_accounts(load("accounts.json"))

    def transactions_sync(self, access_token, cursor):
        self.calls.append((access_token, cursor))
        if self.error:
            raise self.error
        return parse_sync_response(self._payloads[self._cursors.index(cursor)])
