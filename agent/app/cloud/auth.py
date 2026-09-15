"""Guard Agent ↔ Boombiz cloud authentication (Phase 4 §11–13).

    device secret (gd_…, DPAPI-encrypted in settings, sent ONLY to device/auth)
        → POST /api/guard/v1/device/auth
        → access token (ga_…, 30 min, kept in memory only)
        → every other cloud request

Machine binding (§12): the agent generates an installation UUID once and
keeps it in its own database. The cloud refuses the secret from any other
installation. The fingerprint is sha256(installation id + Windows
MachineGuid) — a stable, non-sensitive id, never a hardware serial.

A cloud that hasn't been updated yet has no device/auth route (404). Then we
fall back to sending the secret directly, as agents before Phase 4A did, so
updating the agent before the cloud never breaks phone alerts.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .client import CloudLink

log = logging.getLogger(__name__)

# Refresh a little before expiry so a request never goes out with a dying token.
REFRESH_MARGIN_S = 120


class AuthRejected(Exception):
    """The cloud refused the device secret: revoked, or bound to another PC."""


def machine_guid() -> str:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:  # noqa: BLE001 — not Windows, or no access: the installation id alone still binds
        return ""


class DeviceAuth:
    def __init__(self, link: "CloudLink") -> None:
        self.link = link
        self._token: str | None = None
        self._expires_at = 0.0
        self.legacy = False  # cloud has no device/auth yet → send the secret itself

    # ── identity ─────────────────────────────────────────────────────
    def installation_id(self) -> str:
        iid = self.link._get("installation_id")
        if not iid:
            iid = f"inst_{uuid.uuid4().hex}"
            self.link._put("installation_id", iid)
        return iid

    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.installation_id()}|{machine_guid()}".encode()).hexdigest()

    # ── tokens ───────────────────────────────────────────────────────
    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def cached_token(self) -> str | None:
        return self._token if self._token and time.time() < self._expires_at - REFRESH_MARGIN_S else None

    async def headers(self, client: httpx.AsyncClient) -> dict | None:
        """Authorization header for a cloud call, or None when not activated.
        Raises AuthRejected when the cloud refuses the secret, httpx.HTTPError when offline."""
        secret = self.link.token()
        if not secret:
            return None
        if self.legacy:
            return {"Authorization": f"Bearer {secret}"}
        tok = self.cached_token()
        if not tok:
            tok = await self._exchange(client, secret)
            if tok is None:  # legacy cloud
                return {"Authorization": f"Bearer {secret}"}
        return {"Authorization": f"Bearer {tok}"}

    async def _exchange(self, client: httpx.AsyncClient, secret: str) -> str | None:
        r = await client.post(f"{self.link.base}/api/guard/v1/device/auth",
                              json={"installation_id": self.installation_id()},
                              headers={"Authorization": f"Bearer {secret}"})
        if r.status_code == 404:
            log.info("cloud has no device/auth yet — using the device secret directly")
            self.legacy = True
            return None
        if r.status_code == 401:
            self.invalidate()
            try:
                msg = r.json().get("error")
            except ValueError:
                msg = None
            raise AuthRejected(msg or "This computer is no longer connected to Boombiz. Activate it again.")
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._expires_at = time.time() + int(d.get("expires_in", 1800))
        self.link.state.update(paired=bool(d.get("paired")), business_name=d.get("business_name"),
                               location_name=d.get("location_name"), online=True, last_error=None)
        self.link.apply_licence(d)
        return self._token
