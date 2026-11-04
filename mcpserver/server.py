"""The MCP endpoint: the SDK's MCPServer over Streamable HTTP (stateless, JSON), behind the Access token check."""
from asgiref.sync import sync_to_async
from django.conf import settings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from . import access
from .runtime import tidy_connections

INSTRUCTIONS = """DNB Books is this company's accounting and inventory system.
Records are named by their visible numbers and names: invoice INV-000123, bill BILL-00045, SKU, customer name.
Amounts are strings like "125.00". Dates are YYYY-MM-DD.
Changes are two steps. A create or save tool only returns a preview and a confirmation_id; nothing is saved.
Show the preview to the person, and call confirm_change with the confirmation_id only after they say yes.
A preview expires after 15 minutes. Voids, refunds, credit notes, deposits, journal entries, and month close are done in the app."""


def build_mcp() -> MCPServer:
    from . import tools

    mcp = MCPServer(name="DNB Books", instructions=INSTRUCTIONS, version="1")
    tools.register(mcp)
    return mcp


def _identify(token: str) -> access.Identity:
    tidy_connections()
    try:
        return access.identify(token)
    finally:
        tidy_connections()


class AccessMiddleware:
    """Every HTTP request needs a valid Cloudflare Access token; the caller's identity rides on request.state."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/favicon.ico" and scope.get("method") in ("GET", "HEAD"):
            # The same icon as the web app, read from the same database; it carries nothing private.
            from core.favicon import render_ico

            body = await sync_to_async(render_ico, thread_sensitive=True)()
            await Response(body, media_type="image/x-icon",
                           headers={"Cache-Control": "public, max-age=86400"})(scope, receive, send)
            return
        token = Headers(scope=scope).get(access.HEADER, "")
        try:
            identity = await sync_to_async(_identify, thread_sensitive=True)(token)
        except access.TokenError as exc:
            await JSONResponse({"error": str(exc)}, status_code=401)(scope, receive, send)
            return
        scope.setdefault("state", {})["dnb_identity"] = identity
        await self.app(scope, receive, send)


def build_app(mcp: MCPServer | None = None):
    mcp = mcp or build_mcp()
    hosts = list(settings.DNB_MCP_ALLOWED_HOSTS)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts + [f"{h}:*" for h in hosts],
        allowed_origins=[f"https://{h}" for h in hosts] + ["https://claude.ai", "https://claude.com"],
    )
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True, transport_security=security,
        host="0.0.0.0")
    return AccessMiddleware(app)
