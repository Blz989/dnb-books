"""Who is calling: the Cloudflare Access token on each request, mapped to a DNB Books user.

Cloudflare Access signs in the person (Managed OAuth) and adds a signed token in the Cf-Access-Jwt-Assertion
header. The token is checked here against the team's public keys, the issuer, the application's audience (AUD)
tag, and the expiry, so a request that reaches the service some other way still fails.
"""
from dataclasses import dataclass
from functools import lru_cache

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model

from .models import McpAccess, McpSettings

HEADER = "cf-access-jwt-assertion"

NOT_LINKED = ("This Cloudflare login ({email}) isn't linked to a DNB Books user. "
              "Ask an admin to add it in Settings → Users and roles.")
TURNED_OFF = "Claude access to DNB Books is turned off. An admin can turn it on in Settings."


class TokenError(Exception):
    """The request has no valid Access token. Answered with 401 before any tool runs."""


@dataclass(frozen=True)
class Identity:
    email: str
    user_id: int | None = None
    error: str = ""


def team_domain() -> str:
    domain = settings.DNB_CF_ACCESS_TEAM_DOMAIN.strip().rstrip("/")
    if domain and not domain.startswith("https://"):
        domain = "https://" + domain.removeprefix("http://")
    return domain


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # Keys are cached for an hour; an unknown key id refetches, which covers Cloudflare's key rotation.
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)


def signing_key(token: str):
    """The public key that signed this token. Tests replace this function."""
    return _jwks_client(f"{team_domain()}/cdn-cgi/access/certs").get_signing_key_from_jwt(token).key


def verify(token: str) -> dict:
    aud, issuer = settings.DNB_CF_ACCESS_AUD.strip(), team_domain()
    if not aud or not issuer:
        raise TokenError("The MCP service isn't set up: DNB_CF_ACCESS_AUD and DNB_CF_ACCESS_TEAM_DOMAIN are required.")
    if not token:
        raise TokenError("Sign in through Cloudflare Access first.")
    try:
        claims = jwt.decode(
            token, signing_key(token), algorithms=["RS256"], audience=aud, issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud"]}, leeway=60)
    except (jwt.PyJWTError, ValueError) as exc:
        raise TokenError("The Cloudflare Access token isn't valid. Sign in again.") from exc
    if not claims.get("email"):
        raise TokenError("The Cloudflare Access token has no email. Sign in as a person, not a service token.")
    return claims


def identify(token: str) -> Identity:
    """Raises TokenError for a bad token. A good token from someone not allowed in gets an Identity with an error,
    so they see a plain message in Claude instead of a broken connection."""
    email = verify(token)["email"].strip()
    if not McpSettings.load().enabled:
        return Identity(email, error=TURNED_OFF)
    users = list(get_user_model().objects.filter(email__iexact=email, is_active=True)[:2])
    user = users[0] if len(users) == 1 else None  # two users sharing an email can't be told apart
    if user is None or not McpAccess.objects.filter(user=user, enabled=True).exists():
        return Identity(email, error=NOT_LINKED.format(email=email))
    return Identity(email, user_id=user.pk)
