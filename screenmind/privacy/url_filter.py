"""
URL sanitizer for stored browser URLs (activities.active_url, ui_events.url).

Browser URLs often carry secrets: OAuth state and nonces, email
verification tokens, password-reset keys, signed download links. We store
only what tells us "which page": scheme, host and path. Query strings and
fragments are dropped, except for a few harmless parameters. Sign-in and
token pages keep only scheme and host.
"""

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only pick a view and never carry secrets.
# Search terms (q=, search=) are left out on purpose: not secrets, but private.
_SAFE_PARAMS = {"tab", "view", "page", "sort", "lang", "hl"}

# Hosts that only serve sign-in / account flows.
_AUTH_HOSTS = {
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
    "login.microsoft.com", "appleid.apple.com", "idmsa.apple.com",
    "auth.openai.com", "login.salesforce.com", "id.atlassian.com",
}
_AUTH_HOST_PREFIXES = ("auth.", "login.", "sso.", "accounts.", "signin.", "id.", "idp.", "oauth.")
_AUTH_HOST_SUFFIXES = (".okta.com", ".auth0.com", ".onelogin.com", ".b2clogin.com")

# Path parts of sign-in, callback and token pages.
_AUTH_PATH_RE = re.compile(
    r"/(login-actions|oauth2?|openid-connect|authorize|authenticate|signin|sign-in|sign_in|"
    r"login|logout|callback|sso|saml2?|token|verify|verification|confirm|reset-password|"
    r"password-reset|reset_password|magic-link|magiclink|activate|invite|unsubscribe)(/|$)",
    re.IGNORECASE,
)

# A path segment that is a JWT or a long random token.
_TOKEN_SEGMENT_RE = re.compile(
    r"^(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*|[A-Za-z0-9_\-]{64,})$"
)


def _is_auth_host(host: str) -> bool:
    return (
        host in _AUTH_HOSTS
        or host.startswith(_AUTH_HOST_PREFIXES)
        or host.endswith(_AUTH_HOST_SUFFIXES)
    )


def sanitize_url(url: Optional[str]) -> Optional[str]:
    """Return a URL that is safe to store, or None if it is not a web URL.

    - http(s): scheme + host + path, plus only _SAFE_PARAMS from the query.
      No fragment, no user:password.
    - Sign-in hosts and auth/token paths: scheme + host only.
    - Path segments that look like tokens are replaced by "<token>".
    - Everything else (file://, chrome:, javascript:, ...): None. A file://
      URL leaks a local path and is never a work page.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    netloc = host if parts.port is None else f"{host}:{parts.port}"

    if _is_auth_host(host) or _AUTH_PATH_RE.search(parts.path):
        return urlunsplit((scheme, netloc, "/", "", ""))

    path = "/".join(
        "<token>" if _TOKEN_SEGMENT_RE.match(seg) else seg
        for seg in parts.path.split("/")
    )
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k.lower() in _SAFE_PARAMS])
    return urlunsplit((scheme, netloc, path, query, ""))
