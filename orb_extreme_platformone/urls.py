"""URL validation helpers for outbound API clients."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def _is_local_dev_host(hostname: str | None) -> bool:
    """True for loopback and typical local-dev hostnames.

    Allows plaintext ``http://`` only when tokens cannot leave the machine
    (or a ``*.local`` mDNS name / single-label Docker Compose DNS name such
    as ``netbox``). Public/remote hosts still require HTTPS.
    """
    if not hostname:
        return False
    host = hostname.strip().lower().rstrip(".")
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return True
    # Compose service DNS is a single label (e.g. http://netbox:8080).
    if "." not in host and host.isidentifier():
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_https_url(url: str, *, what: str) -> str:
    """Return a stripped base URL safe for sending API tokens.

    Requires ``https://`` with a host for remote endpoints. Plaintext
    ``http://`` is allowed only for local/dev hosts (localhost, loopback,
    ``*.local``) so NetBox bootstrap works against ``http://localhost:8000``.

    Raises ``ValueError`` for empty, hostless, or non-local ``http://`` values
    so tokens are never sent to an unencrypted remote endpoint.
    """
    cleaned = (url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if not parsed.netloc:
        raise ValueError(f"{what} must be an https:// URL with a host")
    if parsed.scheme == "https":
        return cleaned
    if parsed.scheme == "http" and _is_local_dev_host(parsed.hostname):
        return cleaned
    raise ValueError(f"{what} must be an https:// URL with a host")
