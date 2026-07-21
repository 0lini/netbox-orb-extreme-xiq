"""URL validation helpers for outbound API clients."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# Plaintext http:// is only allowed for these hostnames (plus loopback IPs
# and *.local). The Docker service hostname ``netbox`` is listed explicitly —
# do not treat every single-label name as safe.
_LOCAL_HTTP_HOSTS = frozenset({"localhost", "netbox"})


def _is_local_dev_host(hostname: str | None) -> bool:
    """True for loopback and explicitly allowlisted local-dev hostnames.

    Allows plaintext ``http://`` only when tokens cannot leave the machine
    (loopback, ``*.local`` mDNS, or a local Docker ``netbox`` hostname).
    Public/remote hosts still require HTTPS.
    """
    if not hostname:
        return False
    host = hostname.strip().lower().rstrip(".")
    if host in _LOCAL_HTTP_HOSTS or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_https_url(url: str, *, what: str) -> str:
    """Return a cleaned base URL safe for sending API tokens.

    Requires ``https://`` with a host for remote endpoints. Plaintext
    ``http://`` is allowed only for local/dev hosts (see
    :func:`_is_local_dev_host`) so NetBox bootstrap works against
    ``http://localhost:8000`` and ``http://netbox:8080``.

    Rejects userinfo (``user:pass@host`` / ``legit@evil``) so credentials
    cannot be redirected to an attacker-controlled host via URL confusion.
    Rejects query strings and fragments. Path is preserved (NetBox may be
    mounted under a subpath) and trailing slashes are stripped.

    Raises ``ValueError`` for empty, hostless, userinfo-bearing, or
    non-local ``http://`` values so tokens are never sent to an
    unencrypted remote endpoint.
    """
    cleaned = (url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if not parsed.netloc:
        raise ValueError(f"{what} must be an https:// URL with a host")
    # urlparse puts userinfo in .username/.password; also reject raw "@"
    # in netloc so "https://legit@evil.com" cannot slip through.
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"{what} must not include userinfo (user:pass@host)")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{what} must not include a query string or fragment")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"{what} must be an https:// URL with a host")

    if parsed.scheme == "https" or (parsed.scheme == "http" and _is_local_dev_host(hostname)):
        return cleaned
    raise ValueError(f"{what} must be an https:// URL with a host")
