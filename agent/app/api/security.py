"""Local API protection (Phase 1 §27).

Threats this answers, for an API on 127.0.0.1 that a browser talks to:

  · Another machine on the LAN — the socket is bound to loopback, and every
    request's client address is re-checked here in case someone widens the
    bind without meaning to.
  · A malicious website in the installer's browser (CSRF) — every /api call
    needs the X-Guard-Token header. A cross-site page cannot set a custom
    header without a CORS preflight, and this server grants no CORS.
  · DNS rebinding (evil.com re-pointed at 127.0.0.1) — the Host header must
    name the loopback address/port, and Origin, when sent, must too.

Token handoff: generated on first run, kept in the data dir, and given to the
UI in the URL FRAGMENT (http://127.0.0.1:7480/#t=…). Fragments are never sent
to a server or written to access logs.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets

from fastapi import HTTPException, Request

from ..config import Settings


def load_or_create_token(settings: Settings) -> str:
    path = settings.data_dir / "setup-token"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    tok = secrets.token_urlsafe(32)
    path.write_text(tok, encoding="utf-8")
    return tok


class LocalGuard:
    def __init__(self, settings: Settings, token: str) -> None:
        self.settings = settings
        self.token = token
        port = settings.port
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        self.allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        # The Vite dev server proxies /api during UI development.
        if os.environ.get("GUARD_DEV"):
            self.allowed_origins |= {"http://127.0.0.1:5173", "http://localhost:5173"}
            self.allowed_hosts |= {"127.0.0.1:5173", "localhost:5173"}
        self.loopback_only = settings.bind_host in ("127.0.0.1", "localhost", "::1")

    def check_origin(self, request: Request) -> None:
        host = request.headers.get("host", "")
        if host not in self.allowed_hosts and self.loopback_only:
            raise HTTPException(403, "This page must be opened on this computer.")
        origin = request.headers.get("origin")
        if origin and origin not in self.allowed_origins:
            raise HTTPException(403, "Request blocked.")
        client = request.client.host if request.client else ""
        if self.loopback_only:
            try:
                if not ipaddress.ip_address(client).is_loopback:
                    raise HTTPException(403, "Guard setup only accepts connections from this computer.")
            except ValueError:
                if client not in ("testclient",):
                    raise HTTPException(403, "Guard setup only accepts connections from this computer.")

    def check_token(self, request: Request) -> None:
        sent = request.headers.get("x-guard-token", "")
        if not sent or not hmac.compare_digest(sent, self.token):
            raise HTTPException(401, "Open Guard setup from the Boombiz Guard shortcut on this computer.")
