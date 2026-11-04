"""Runs one tool call as the signed-in DNB Books user, turns known errors into plain messages, and logs the call."""
import logging
from decimal import Decimal, InvalidOperation

from auditlog.context import set_actor
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from mcp.server.mcpserver.exceptions import ToolError

from core.roles import is_admin
from items.errors import StockError
from ledger.errors import PostingError
from purchasing.errors import PurchasingError
from sales.errors import SalesError

from .access import Identity
from .models import McpCall

log = logging.getLogger(__name__)

USER_ERRORS = (SalesError, PurchasingError, PostingError, StockError)
HIDDEN_ARGUMENTS = {"tax_id"}


class Refused(Exception):
    """A plain-language refusal raised by a tool (unknown record, bad input)."""


def role_message(user) -> str:
    return f"Your role ({'Admin' if is_admin(user) else 'Standard'}) can't do this. Ask an admin."


def require_perm(user, perm: str) -> None:
    if not (user.has_perm(perm) or is_admin(user)):
        raise Refused(role_message(user))


def money(value: str | int | None, label: str = "amount") -> Decimal:
    if value is None or str(value).strip() == "":
        raise Refused(f"Enter the {label}, like 125.00.")
    try:
        d = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise Refused(f"Enter the {label} like 125.00.") from None
    if not d.is_finite():
        raise Refused(f"Enter the {label} like 125.00.")
    return d


def fmt(value) -> str:
    """Money for Claude: plain digits, two places, no thousands separators."""
    if value is None:
        return ""
    return f"{Decimal(value):.2f}"


def _clean_args(args: dict) -> dict:
    def clean(v):
        if isinstance(v, dict):
            return {k: ("(hidden)" if k in HIDDEN_ARGUMENTS else clean(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    return clean(args)


def tidy_connections() -> None:
    """Drop stale database connections between calls (each call runs on a worker thread). Inside a transaction,
    as in tests, there is nothing to tidy."""
    if not connection.in_atomic_block:
        close_old_connections()


def identity_from(ctx) -> Identity:
    request = getattr(ctx.request_context, "request", None)
    identity = getattr(getattr(request, "state", None), "dnb_identity", None) if request is not None else None
    if identity is None:
        raise ToolError("Sign in through Cloudflare Access first.")
    return identity


def run(ctx, tool: str, fn, **kwargs):
    """Call fn(user, **kwargs). fn returns a dict; a "document" key is recorded in the call log."""
    identity = identity_from(ctx)
    tidy_connections()
    user = None
    try:
        if identity.error:
            raise Refused(identity.error)
        user = get_user_model().objects.get(pk=identity.user_id)
        with set_actor(user):  # audit log entries name the person, as they do in the app
            result = fn(user, **kwargs)
        _record(identity, user, tool, kwargs, ok=True, document=(result or {}).get("document", ""))
        return result
    except Refused as exc:
        _record(identity, user, tool, kwargs, ok=False, error=str(exc))
        raise ToolError(str(exc)) from None
    except PermissionDenied:
        message = role_message(user)
        _record(identity, user, tool, kwargs, ok=False, error=message)
        raise ToolError(message) from None
    except USER_ERRORS as exc:
        _record(identity, user, tool, kwargs, ok=False, error=str(exc))
        raise ToolError(str(exc)) from None
    except ValidationError as exc:
        message = " ".join(exc.messages)
        _record(identity, user, tool, kwargs, ok=False, error=message)
        raise ToolError(message) from None
    except Exception:
        log.exception("MCP tool %s failed", tool)
        _record(identity, user, tool, kwargs, ok=False, error="Unexpected error.")
        raise ToolError("Something went wrong in DNB Books. The error was logged; try again or use the app.") from None
    finally:
        tidy_connections()


def _record(identity, user, tool, kwargs, *, ok, error="", document=""):
    try:
        McpCall.objects.create(user=user, email=identity.email, tool=tool, ok=ok, error=error[:2000],
                               document=document or "", arguments=_clean_args(kwargs))
    except Exception:  # the log must never break the call
        log.exception("Could not record MCP call")
