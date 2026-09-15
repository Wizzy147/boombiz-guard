"""Link to the Boombiz cloud. Outbound only (PRD §40): the agent calls the
cloud; the cloud never reaches into the shop network.

Two ways to connect this PC (both end with a device secret here):

    Installer activation (Phase 4A)
    POST {cloud}/api/guard/v1/devices/activate   → device_id, device_secret, location
    Phone pairing (owner types the code this PC shows)
    POST {cloud}/api/guard/v1/pair/start         → device_id, device_token, pairing_code

Then (cloud/auth.py) the secret is traded for 30-minute access tokens:

    POST {cloud}/api/guard/v1/device/auth        → access_token     (secret)
    GET  {cloud}/api/guard/v1/device             → {paired, business_name}
    POST {cloud}/api/guard/v1/alerts             → {accepted, pushed}
    POST {cloud}/api/guard/v1/devices/{id}/heartbeat   (cloud/heartbeat.py)

The secret is shown to nobody: it's DPAPI-encrypted in the settings table.

What goes to the cloud: incident ref, type, severity, camera NAME, time and
title, plus health numbers. No video, no snapshot, no CCTV address or password.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from ..database.db import Database
from ..database.models import CloudOutbox, Setting
from ..security.vault import default_cipher
from .auth import AuthRejected, DeviceAuth

log = logging.getLogger(__name__)

DEFAULT_CLOUD = os.environ.get("GUARD_CLOUD_URL", "https://guard.getboombiz.com").rstrip("/")
BACKOFF = (5, 15, 30, 60, 120, 300)
# PRD §39 — after a long outage: HIGH alerts older than this go to the
# dashboard only (no phone buzz); CRITICAL always buzzes.
STALE_PUSH_MINUTES = 60
# A linked PC re-asks "am I still linked?" at most this often; heartbeats
# (every 2 min) answer it in between. Revocation still shows at once: the
# next heartbeat or token refresh fails sign-in.
LINKED_CHECK_SECONDS = 15 * 60


class CloudError(Exception):
    pass


def _error_from(r: httpx.Response, fallback: str) -> str:
    try:
        return r.json().get("error") or fallback
    except ValueError:
        return fallback


class CloudLink:
    def __init__(self, db: Database, cipher=None, base_url: str | None = None) -> None:  # noqa: ANN001
        self.db = db
        self.cipher = cipher or default_cipher()
        self.base = (base_url or DEFAULT_CLOUD).rstrip("/")
        self.auth = DeviceAuth(self)
        self._last_link_check = float("-inf")
        # app/licence.py — set by main; every cloud answer that carries a
        # `licence` is handed to it.
        self.licence = None
        self.state: dict = {"paired": False, "business_name": None, "location_name": None, "online": None,
                            "last_error": None, "pairing_code": None, "pairing_expires_at": None,
                            "link_code": None, "link_url": None, "link_expires_at": None,
                            "health_status": None, "health_reasons": [], "last_heartbeat_at": None}

    def apply_licence(self, d: dict) -> None:
        if self.licence is not None and isinstance(d, dict) and "licence" in d:
            self.licence.update(d["licence"])

    # ── secret storage (encrypted) ───────────────────────────────────
    def _get(self, key: str) -> str | None:
        with self.db.session() as s:
            r = s.get(Setting, key)
            return r.value if r else None

    def _put(self, key: str, value: str | None) -> None:
        with self.db.session() as s:
            r = s.get(Setting, key)
            if value is None:
                if r:
                    s.delete(r)
            elif r:
                r.value = value
            else:
                s.add(Setting(key=key, value=value))

    def token(self) -> str | None:
        """The device SECRET. Only cloud/auth.py sends it anywhere."""
        blob = self._get("cloud_device_token")
        if not blob:
            return None
        return self.cipher.unprotect(base64.b64decode(blob)).decode()

    def device_id(self) -> str | None:
        return self._get("cloud_device_id")

    def _store_secret(self, device_id: str, secret: str) -> None:
        self._put("cloud_device_id", device_id)
        self._put("cloud_device_token", base64.b64encode(self.cipher.protect(secret.encode())).decode())
        self.auth.invalidate()
        self.auth.legacy = False

    # ── installer activation (Phase 4A) ──────────────────────────────
    async def activate(self, code: str, name: str, version: str) -> dict:
        body = {
            "activation_code": code.strip()[:20],
            "installation_id": self.auth.installation_id(),
            "device_name": name[:60],
            "agent_version": version,
            "os_version": f"{platform.system()} {platform.release()} ({platform.version()})"[:100],
            "fingerprint_hash": self.auth.fingerprint(),
        }
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{self.base}/api/guard/v1/devices/activate", json=body)
        except httpx.HTTPError:
            raise CloudError("Can't reach Boombiz. Check this computer's internet connection and try again.") from None
        if r.status_code == 404:
            raise CloudError("Boombiz isn't ready for activation codes yet. Use phone linking instead, or try again later.")
        if r.status_code != 200:
            raise CloudError(_error_from(r, "Boombiz couldn't activate this computer. Try again in a minute."))
        d = r.json()
        self._store_secret(d["device_id"], d["device_secret"])
        self.state.update(paired=True, business_name=d.get("business_name"), location_name=d.get("location_name"),
                          pairing_code=None, pairing_expires_at=None, online=True, last_error=None)
        self.db.audit("cloud_activated", d["device_id"], location=d.get("location_name"))
        return {"business_name": d.get("business_name"), "location_name": d.get("location_name")}

    # ── phone pairing ────────────────────────────────────────────────
    async def start_pairing(self, name: str, version: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{self.base}/api/guard/v1/pair/start",
                                 json={"name": name[:60], "agent_version": version})
        except httpx.HTTPError:
            raise CloudError("Can't reach Boombiz. Check this computer's internet connection and try again.") from None
        if r.status_code != 200:
            raise CloudError("Boombiz couldn't start pairing. Try again in a minute.")
        d = r.json()
        self._store_secret(d["device_id"], d["device_token"])
        self.state.update(pairing_code=d["pairing_code"], pairing_expires_at=d["expires_at"], paired=False,
                          location_name=None)
        self.db.audit("cloud_pairing_started", d["device_id"])
        return {"pairing_code": d["pairing_code"], "expires_at": d["expires_at"]}

    # ── browser sign-in (plug-and-play, lib/guard/link.ts in the cloud) ──
    async def start_link(self, name: str, version: str, summary: dict | None) -> dict:
        """Ask for a guard.getboombiz.com/link URL. The PC gets its secret now,
        linked to nobody until the owner signs in and confirms in a browser —
        the merchant's password never reaches this computer."""
        body = {
            "installation_id": self.auth.installation_id(), "name": name[:60], "agent_version": version,
            "os_version": f"{platform.system()} {platform.release()} ({platform.version()})"[:100],
            "fingerprint_hash": self.auth.fingerprint(),
            **({"summary": summary} if summary else {}),
        }
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{self.base}/api/guard/v1/link/start", json=body)
        except httpx.HTTPError:
            raise CloudError("Can't reach Boombiz. Check this computer's internet connection and try again.") from None
        if r.status_code == 404:
            raise CloudError("Boombiz isn't ready for browser sign-in yet. Use an activation code instead.")
        if r.status_code != 200:
            raise CloudError(_error_from(r, "Boombiz couldn't start sign-in. Try again in a minute."))
        d = r.json()
        self._store_secret(d["device_id"], d["device_secret"])
        self.state.update(paired=False, business_name=None, location_name=None, link_code=d["link_code"],
                          link_url=d["link_url"], link_expires_at=d.get("expires_at"), online=True, last_error=None)
        self.db.audit("cloud_link_started", d["device_id"])
        return {"link_url": d["link_url"], "link_code": d["link_code"], "expires_at": d.get("expires_at")}

    async def send_test_alert(self) -> dict:
        """Guard Test's last step: the owner's phone gets "Your business protection is active"."""
        async with httpx.AsyncClient(timeout=20) as c:
            headers = await self.auth.headers(c)
            if headers is None:
                raise CloudError("This computer isn't linked to your Boombiz account.")
            r = await c.post(f"{self.base}/api/guard/v1/setup/test-alert", json={}, headers=headers)
        if r.status_code == 404:
            raise CloudError("Boombiz can't send test alerts yet.")
        if r.status_code != 200:
            raise CloudError(_error_from(r, "The test alert couldn't be sent."))
        return r.json()

    def unpair(self) -> None:
        self._put("cloud_device_token", None)
        self._put("cloud_device_id", None)
        self.auth.invalidate()
        self.state.update(paired=False, business_name=None, location_name=None, pairing_code=None,
                          pairing_expires_at=None, link_code=None, link_url=None, link_expires_at=None,
                          health_status=None, health_reasons=[], last_heartbeat_at=None)
        self.db.audit("cloud_unpaired", None)

    async def refresh(self) -> dict:
        if not self.token():
            self.state.update(paired=False, online=None)
            return self.state
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                headers = await self.auth.headers(c)
                r = await c.get(f"{self.base}/api/guard/v1/device", headers=headers)
                if r.status_code == 401 and not self.auth.legacy:
                    self.auth.invalidate()
                    r = await c.get(f"{self.base}/api/guard/v1/device", headers=await self.auth.headers(c))
            if r.status_code == 401:
                self.state.update(paired=False, online=True, last_error="This PC is no longer linked. Pair it again.")
            elif r.status_code == 200:
                d = r.json()
                self.state.update(paired=bool(d.get("paired")), business_name=d.get("business_name"), online=True,
                                  last_error=None)
                if "location_name" in d:
                    self.state["location_name"] = d.get("location_name")
                self.mark_link_checked()
                self.apply_licence(d)
                if d.get("paired"):
                    self.state.update(pairing_code=None, pairing_expires_at=None, link_code=None, link_url=None,
                                      link_expires_at=None)
            else:
                self.state.update(online=True, last_error="Boombiz returned an error.")
        except AuthRejected as e:
            self.state.update(paired=False, online=True, last_error=str(e))
        except httpx.HTTPError:
            self.state.update(online=False)
        return self.state

    def mark_link_checked(self) -> None:
        self._last_link_check = time.monotonic()

    async def refresh_if_due(self) -> dict:
        """The 60 s loop calls this. Not linked yet (or pairing code on screen):
        ask every time, so a phone claim shows up fast. Linked: heartbeats
        already carry the link state, so only ask when nothing has confirmed
        it for 15 min — saves most of this PC's cloud requests."""
        linked = self.state.get("paired") and not self.state.get("pairing_code") and not self.state.get("link_code")
        if linked and time.monotonic() - self._last_link_check < LINKED_CHECK_SECONDS:
            return self.state
        return await self.refresh()

    # ── outbox ───────────────────────────────────────────────────────
    def enqueue_alert(self, item: dict) -> None:
        """Called for every pop-up-worthy item. Idempotent per (incident, severity)."""
        with self.db.session() as s:
            if s.scalar(select(CloudOutbox.id).where(CloudOutbox.dedup_key == item["key"])):
                return
            s.add(CloudOutbox(kind="ALERT", dedup_key=item["key"], payload_json=json.dumps(item)))

    async def flush(self, now: datetime | None = None) -> dict:
        """Send due alerts. No secret/not paired → keep them queued."""
        now = now or datetime.now(timezone.utc)
        sent = failed = 0
        if not self.token():
            return {"sent": 0, "failed": 0, "queued": self.queued()}
        with self.db.session() as s:
            due = [(o.id, o.payload_json, o.attempts) for o in s.scalars(
                select(CloudOutbox).where(CloudOutbox.status == "PENDING").order_by(CloudOutbox.created_at).limit(20))
                if (o.next_attempt_at if o.next_attempt_at.tzinfo else o.next_attempt_at.replace(tzinfo=timezone.utc)) <= now]
        if not due:
            return {"sent": 0, "failed": 0, "queued": self.queued()}
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                headers = await self.auth.headers(c)
            except AuthRejected as e:
                self.state.update(paired=False, online=True, last_error=str(e))
                return {"sent": 0, "failed": 0, "queued": self.queued()}
            except httpx.HTTPError:
                self.state["online"] = False
                return {"sent": 0, "failed": 0, "queued": self.queued()}
            for oid, payload, attempts in due:
                item = json.loads(payload)
                occurred = datetime.fromisoformat(item["occurred_at"])
                stale = now - occurred > timedelta(minutes=STALE_PUSH_MINUTES)
                item["push"] = item["severity"] == "CRITICAL" or not stale
                try:
                    r = await c.post(f"{self.base}/api/guard/v1/alerts", json=item, headers=headers)
                    if r.status_code == 401 and not self.auth.legacy:
                        self.auth.invalidate()  # token rotated mid-batch: one fresh try
                        headers = await self.auth.headers(c)
                        r = await c.post(f"{self.base}/api/guard/v1/alerts", json=item, headers=headers)
                    ok = r.status_code in (200, 409)  # 409 = already had it
                    err = None if ok else ("Not linked to a business yet." if r.status_code in (401, 403)
                                           else f"HTTP {r.status_code}")
                except AuthRejected as e:
                    self.state.update(paired=False, last_error=str(e))
                    ok, err = False, "rejected"
                except httpx.HTTPError:
                    ok, err = False, "offline"
                with self.db.session() as s:
                    o = s.get(CloudOutbox, oid)
                    if ok:
                        o.status, o.last_error = "SENT", None
                        sent += 1
                    else:
                        o.attempts = attempts + 1
                        o.last_error = err
                        o.next_attempt_at = now + timedelta(seconds=BACKOFF[min(attempts, len(BACKOFF) - 1)])
                        failed += 1
                if err in ("offline", "rejected"):
                    if err == "offline":
                        self.state["online"] = False
                    break  # no point hammering a dead connection or a revoked credential
                self.state["online"] = True
        return {"sent": sent, "failed": failed, "queued": self.queued()}

    def queued(self) -> int:
        with self.db.session() as s:
            return len(list(s.scalars(select(CloudOutbox.id).where(CloudOutbox.status == "PENDING"))))
