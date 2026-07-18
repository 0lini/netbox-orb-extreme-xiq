"""URL validation helpers for outbound API clients."""

from __future__ import annotations

from urllib.parse import urlparse


def require_https_url(url: str, *, what: str) -> str:
    """Return a stripped base URL, requiring an ``https://`` scheme and host.

    Raises ``ValueError`` for empty, non-https, or hostless values so tokens
    are never sent to an unencrypted or malformed endpoint.
    """
    cleaned = (url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{what} must be an https:// URL with a host")
    return cleaned
