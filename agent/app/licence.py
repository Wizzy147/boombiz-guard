"""What this PC's Guard plan allows (monthly plan, 2026-09-15).

The Boombiz cloud decides; this keeps its last answer so protection survives
the internet going down. The answer arrives with every sign-in, link check
and heartbeat (`licence` in the response), carrying a signed token:

    base64url(payload JSON) "." base64url(Ed25519 signature)
    payload = {v, d: device id, i: installation id, s: status, c: cameras,
               u: valid until (unix s) | null, t: issued (unix s)}

The token is checked against the public key built into this agent, and must
name THIS device and installation — so editing guard.db or copying another
PC's token doesn't extend or move a plan.

    ACTIVE / LEGACY   Guard AI on up to `c` cameras until `u` (end of the paid
                      or free month + 7 grace days)
    EXPIRED           the plan lapsed: demo mode until a renewal arrives
    DEMO / REVOKED    nothing paid (or refunded): demo mode

Rules that keep a paying shop protected:
  · Only an explicit cloud answer changes the plan; offline, errors or an old
    cloud change nothing. A signed plan keeps working offline until `u`.
  · A licence sent WITHOUT a token (the cloud's signing key isn't set) is
    trusted for 72 hours of this process only, never across a restart.
  · An install that was already protecting cameras before licences existed
    keeps 2 cameras until the cloud first speaks.
  · GUARD_MAX_CAMERAS, when set, overrides everything (lab and support only).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select

from .database.db import Database
from .database.models import Camera, Setting

log = logging.getLogger(__name__)

KEY = "licence_json"
STATUSES = {"ACTIVE", "LEGACY", "DEMO", "EXPIRED", "REVOKED"}
PROTECTING = {"ACTIVE", "LEGACY", "GRANDFATHERED"}
LEGACY_CAMERAS = 2
UNSIGNED_TRUST_S = 72 * 3600
DEMO = {"status": "DEMO", "tier": None, "name": None, "ai_cameras": 0, "valid_until": None, "token": None}

# Public half of BOOMBIZ_GUARD_LICENCE_KEY (web app). Rotating the key means
# shipping an agent with the new public key first.
PUBLIC_KEY_B64 = "Emc52ZioPjtX29B0imZTPYz2SYpixkGWGzZntpZRrJg="


def _override() -> int | None:
    raw = os.environ.get("GUARD_MAX_CAMERAS")
    return int(raw) if raw and raw.strip().isdigit() else None


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(token: str, device_id: str | None, installation_id: str | None) -> dict | None:
    """The token's payload if it's genuine and made for this PC, else None."""
    try:
        body, sig = token.split(".")
        payload = _b64(body)
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(os.environ.get("GUARD_LICENCE_PUBKEY") or PUBLIC_KEY_B64))
        pub.verify(_b64(sig), payload)
        p = json.loads(payload)
    except (ValueError, InvalidSignature, TypeError):
        return None
    if not isinstance(p, dict) or p.get("v") != 1 or p.get("s") not in STATUSES:
        return None
    if device_id and p.get("d") != device_id:
        return None
    if installation_id and p.get("i") and p.get("i") != installation_id:
        return None
    return p


class Licence:
    def __init__(self, db: Database, identity: Callable[[], tuple[str | None, str | None]] | None = None) -> None:
        self.db = db
        # (device id, installation id) — set by main once the cloud link exists.
        self.identity = identity or (lambda: (None, None))
        # Called with the new limit when it goes DOWN, so the extra cameras stop.
        self.on_lower: Callable[[int], object] | None = None
        self._unsigned_at: float | None = None  # monotonic time an unsigned answer arrived
        self._state = self._load()
        self._last_limit = self.limit()

    # ── storage ──────────────────────────────────────────────────────
    def _load(self) -> dict:
        with self.db.session() as s:
            row = s.get(Setting, KEY)
            raw = row.value if row else None
            protecting = s.scalar(select(Camera.id).where(Camera.guard_enabled.is_(True)).limit(1)) is not None
        if raw:
            try:
                d = json.loads(raw)
            except ValueError:
                d = None
            if isinstance(d, dict):
                if d.get("status") == "GRANDFATHERED":
                    return d
                if d.get("token"):
                    p = verify_token(d["token"], *self.identity())
                    if p:
                        return self._from_payload(p, d["token"], d)
                # Unsigned or tampered: never trusted across a restart.
                return dict(DEMO)
        state = ({"status": "GRANDFATHERED", "tier": "BASIC", "name": "Guard Basic", "ai_cameras": LEGACY_CAMERAS,
                  "valid_until": None, "token": None} if protecting else dict(DEMO))
        self._save(state)
        return state

    def _save(self, state: dict) -> None:
        with self.db.session() as s:
            row = s.get(Setting, KEY)
            if row:
                row.value = json.dumps(state)
            else:
                s.add(Setting(key=KEY, value=json.dumps(state)))

    @staticmethod
    def _from_payload(p: dict, token: str, wire: dict) -> dict:
        return {"status": p["s"], "tier": wire.get("tier"), "name": wire.get("name"),
                "ai_cameras": max(0, min(64, int(p.get("c") or 0))), "valid_until": p.get("u"), "token": token}

    # ── read ─────────────────────────────────────────────────────────
    def _covered(self, now: float | None = None) -> bool:
        st = self._state
        if st.get("status") not in PROTECTING:
            return False
        if st.get("status") == "GRANDFATHERED":
            return True
        u = st.get("valid_until")
        if u is not None and (now or time.time()) >= float(u):
            return False
        if not st.get("token"):
            return self._unsigned_at is not None and time.monotonic() - self._unsigned_at < UNSIGNED_TRUST_S
        return True

    def limit(self) -> int:
        o = _override()
        if o is not None:
            return o
        return max(0, int(self._state.get("ai_cameras") or 0)) if self._covered() else 0

    def protects(self) -> bool:
        return self.limit() > 0

    def is_demo(self) -> bool:
        return not self.protects()

    def current(self) -> dict:
        st = self._state
        status = st.get("status")
        if status in PROTECTING and not self._covered():
            status = "EXPIRED"
        u = st.get("valid_until")
        return {"status": status, "tier": st.get("tier"), "name": st.get("name"), "ai_cameras": st.get("ai_cameras", 0),
                "valid_until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(u))) if u else None,
                "days_left": max(0, int((float(u) - time.time()) // 86400)) if u else None,
                "signed": bool(st.get("token")), "limit": self.limit(), "protects": self.protects()}

    # ── write (cloud answers only) ───────────────────────────────────
    def update(self, wire: object) -> bool:
        """Apply a `licence` object from the cloud. Returns True if it changed."""
        if not isinstance(wire, dict) or wire.get("status") not in STATUSES:
            return False  # absent or malformed: an old cloud, never a reason to stop protecting
        token = wire.get("token")
        if token:
            p = verify_token(token, *self.identity())
            if not p:
                log.warning("licence token refused (bad signature or another PC's)")
                return False
            new = self._from_payload(p, token, wire)
        else:
            try:
                cams = max(0, min(64, int(wire.get("ai_cameras") or 0)))
            except (TypeError, ValueError):
                return False
            vu = wire.get("valid_until")
            try:
                u = time.mktime(time.strptime(vu[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if vu else None
            except (TypeError, ValueError):
                u = None
            new = {"status": wire["status"], "tier": wire.get("tier"), "name": wire.get("name"), "ai_cameras": cams,
                   "valid_until": u, "token": None}
            self._unsigned_at = time.monotonic()
        keys = ("status", "tier", "ai_cameras", "valid_until")
        changed = any(new.get(k) != self._state.get(k) for k in keys)
        self._state = new
        self._save(new)  # also refreshes the stored token's issue time
        if changed:
            self.db.audit("licence_changed", None, status=new["status"], ai_cameras=new["ai_cameras"],
                          valid_until=new["valid_until"], signed=bool(token))
            log.info("licence %s ai_cameras=%s valid_until=%s signed=%s", new["status"], new["ai_cameras"],
                     new["valid_until"], bool(token))
        self.check()
        return changed

    def check(self) -> int:
        """Run every minute and after every update: a plan that just ran out
        (or went down) stops the cameras over the new limit."""
        now = self.limit()
        if now < self._last_limit and self.on_lower:
            try:
                self.on_lower(now)
            except Exception:
                log.exception("applying a lower camera limit failed")
        self._last_limit = now
        return now
