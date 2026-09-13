"""The ONE place credentials are put into an RTSP URL — just before FFmpeg
opens it. Stored URIs are always credential-free."""

from __future__ import annotations

from urllib.parse import quote, urlparse, urlunparse

from ..adapters.base import Credentials


def with_credentials(uri: str, creds: Credentials | None) -> str:
    if not creds:
        return uri
    p = urlparse(uri)
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    netloc = f"{quote(creds.username, safe='')}:{quote(creds.password, safe='')}@{host}"
    return urlunparse(p._replace(netloc=netloc))


def split_credentials(uri: str) -> tuple[str, Credentials | None]:
    """An installer may paste rtsp://user:pass@ip/... — keep the URI, vault the rest."""
    from urllib.parse import unquote

    p = urlparse(uri)
    if not p.username:
        return uri, None
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    clean = urlunparse(p._replace(netloc=host))
    return clean, Credentials(unquote(p.username), unquote(p.password or ""))
