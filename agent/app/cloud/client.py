"""Link to the Boombiz cloud for phone alerts (a first slice of Phase 4).

Outbound only (PRD §40): the agent calls the cloud; the cloud never reaches
into the shop network.

    POST {cloud}/api/guard/v1/pair/start         → device_id, device_token, pairing_code
    GET  {cloud}/api/guard/v1/device             → {paired, business_name}   (Bearer)
    POST {cloud}/api/guard/v1/alerts             → {accepted, pushed}       (Bearer)

The device token is shown to nobody: it's DPAPI-encrypted in the settings
table and only ever sent as a Bearer header over HTTPS. The pairing code is
what the owner types on their phone after signing in to Boombiz.

What goes to the cloud: incident ref, type, severity, camera NAME, time and
title. No video, no snapshot, no CCTV address or password.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from ..database.db import Database
from ..database.models import CloudOutbox, Setting
from ..security.vault import default_cipher

log = logging.getLogger(__name__)

DEFAULT_CLOUD = os.environ.get("GUARD_CLOUD_URL", "https://guard.getboombiz.com").rstrip("/")
BACKOFF = (5, 15, 30, 60, 120, 300)
# PRD §39 — after a long outage: HIGH alerts older than this go to the
# dashboard only (no phone buzz); CRITICAL always buzzes.
STALE_PUSH_MINUTES = 60


class CloudError(Exception):
    pass


class CloudLink:
    def __init__(self, db: Database, cipher=None, base_url: str | None = None) -> None:  # noqa: ANN001
        self.db = db
        self.cipher = cipher or default_cipher()
        self.base = (base_url or DEFAULT_CLOUD).rstrip("/")
        self.state: dict = {"paired": False, "business_name": None, "online": None, "last_error": None,
                            "pairing_code": None, "pairing_expires_at": None}

    # ── token storage (encrypted) ────────────────────────────────────
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
        blob = self._get("cloud_device_token")
        if not blob:
            return None
        return self.cipher.unprotect(base64.b64decode(blob)).decode()

    def device_id(self) -> str | None:
        return self._get("cloud_device_id")

    # ── pairing ──────────────────────────────────────────────────────
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
        self._put("cloud_device_id", d["device_id"])
        self._put("cloud_device_token", base64.b64encode(self.cipher.protect(d["device_token"].encode())).decode())
        self.state.update(pairing_code=d["pairing_code"], pairing_expires_at=d["expires_at"], paired=False)
        self.db.audit("cloud_pairing_started", d["device_id"])
        return {"pairing_code": d["pairing_code"], "expires_at": d["expires_at"]}

    def unpair(self) -> None:
        self._put("cloud_device_token", None)
        self._put("cloud_device_id", None)
        self.state.update(paired=False, business_name=None, pairing_code=None, pairing_expires_at=None)
        self.db.audit("cloud_unpaired", None)

    async def refresh(self) -> dict:
        tok = self.token()
        if not tok:
            self.state.update(paired=False, online=None)
            return self.state
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.base}/api/guard/v1/device", headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 401:
                self.state.update(paired=False, online=True, last_error="This PC is no longer linked. Pair it again.")
            elif r.status_code == 200:
                d = r.json()
                self.state.update(paired=bool(d.get("paired")), business_name=d.get("business_name"), online=True,
                                  last_error=None)
                if d.get("paired"):
                    self.state.update(pairing_code=None, pairing_expires_at=None)
            else:
                self.state.update(online=True, last_error="Boombiz returned an error.")
        except httpx.HTTPError:
            self.state.update(online=False)
        return self.state

    # ── outbox ───────────────────────────────────────────────────────
    def enqueue_alert(self, item: dict) -> None:
        """Called for every pop-up-worthy item. Idempotent per (incident, severity)."""
        with self.db.session() as s:
            if s.scalar(select(CloudOutbox.id).where(CloudOutbox.dedup_key == item["key"])):
                return
            s.add(CloudOutbox(kind="ALERT", dedup_key=item["key"], payload_json=json.dumps(item)))

    async def flush(self, now: datetime | None = None) -> dict:
        """Send due alerts. No token/not paired → keep them queued."""
        now = now or datetime.now(timezone.utc)
        tok = self.token()
        sent = failed = 0
        if not tok:
            return {"sent": 0, "failed": 0, "queued": self.queued()}
        with self.db.session() as s:
            due = [(o.id, o.payload_json, o.attempts) for o in s.scalars(
                select(CloudOutbox).where(CloudOutbox.status == "PENDING").order_by(CloudOutbox.created_at).limit(20))
                if (o.next_attempt_at if o.next_attempt_at.tzinfo else o.next_attempt_at.replace(tzinfo=timezone.utc)) <= now]
        async with httpx.AsyncClient(timeout=15) as c:
            for oid, payload, attempts in due:
                item = json.loads(payload)
                occurred = datetime.fromisoformat(item["occurred_at"])
                stale = now - occurred > timedelta(minutes=STALE_PUSH_MINUTES)
                item["push"] = item["severity"] == "CRITICAL" or not stale
                try:
                    r = await c.post(f"{self.base}/api/guard/v1/alerts", json=item,
                                     headers={"Authorization": f"Bearer {tok}"})
                    ok = r.status_code in (200, 409)  # 409 = already had it
                    err = None if ok else ("Not linked to a business yet." if r.status_code in (401, 403)
                                           else f"HTTP {r.status_code}")
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
                if err == "offline":
                    self.state["online"] = False
                    break  # no point hammering a dead connection
                self.state["online"] = True
        return {"sent": sent, "failed": failed, "queued": self.queued()}

    def queued(self) -> int:
        with self.db.session() as s:
            return len(list(s.scalars(select(CloudOutbox.id).where(CloudOutbox.status == "PENDING"))))
