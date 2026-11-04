from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the Claude (MCP) service: Streamable HTTP at /mcp, behind Cloudflare Access."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=8001)

    def handle(self, *args, host, port, **options):
        import uvicorn
        from django.conf import settings

        from mcpserver.server import build_app

        if not settings.DNB_CF_ACCESS_AUD or not settings.DNB_CF_ACCESS_TEAM_DOMAIN:
            self.stderr.write("DNB_CF_ACCESS_AUD and DNB_CF_ACCESS_TEAM_DOMAIN are not set. Every request will be refused.")
        hosts = ", ".join(settings.DNB_MCP_ALLOWED_HOSTS)
        self.stdout.write(f"DNB Books MCP running on http://{host}:{port}/mcp for {hosts} (Ctrl+C to stop)")
        # proxy_headers off: the tunnel connects directly, and nothing here trusts X-Forwarded-For.
        uvicorn.run(build_app(), host=host, port=port, proxy_headers=False, server_header=False, log_level="info")
