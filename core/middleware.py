from django.conf import settings

from .storage import current_version

YEAR = "public, max-age=31536000, immutable"


class StaticCacheMiddleware:
    """Let browsers and Cloudflare keep a static file for a year when its ?v= matches the file's current hash.

    Pages link static files with ?v=<hash> (core.storage), so a release changes the URL and nothing goes stale.
    WhiteNoise alone sends max-age=60, which makes every click over the tunnel re-ask for the CSS, JS, and fonts.
    Fonts are linked from app.css without ?v=, so .woff2 files get the year as well: a changed font must get a
    new file name. Sits above WhiteNoiseMiddleware so it sees WhiteNoise's responses.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.prefix = "/" + settings.STATIC_URL.lstrip("/")

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith(self.prefix) or response.status_code not in (200, 304):
            return response
        version = request.GET.get("v")
        if request.path.endswith(".woff2") or (version and current_version(request.path[len(self.prefix):]) == version):
            response["Cache-Control"] = YEAR
        return response
