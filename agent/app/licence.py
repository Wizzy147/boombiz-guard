"""What this PC's Guard licence allows (plug-and-play, 2026-09-15).

The Boombiz cloud decides; this keeps its last answer so protection survives
the internet going down. The answer arrives with every sign-in, link check
and heartbeat (`licence` in the response):

    ACTIVE   a package: Guard AI on up to `ai_cameras` cameras
    LEGACY   set up by a BDO before packages existed: Guard Basic, 2 cameras
    DEMO     Compatibility & Demo mode: scan, connect, test, recommend —
             no camera is protected continuously
    REVOKED  licence withdrawn: behaves like DEMO

Rules that keep a paying shop protected:
  · Only an explicit cloud answer lowers the limit. Being offline, a cloud
    error, or an old cloud that sends no licence changes nothing.
  · An install that was already protecting cameras before this version (no
    stored answer yet) keeps Guard Basic's 2 cameras until the cloud speaks.
  · GUARD_MAX_CAMERAS, when set, overrides everything (lab and support only).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from sqlalchemy import select

from .database.db import Database
from .database.models import Camera, Setting

log = logging.getLogger(__name__)

KEY = "licence_json"
STATUSES = {"ACTIVE", "LEGACY", "DEMO", "REVOKED"}
LEGACY_CAMERAS = 2
DEMO = {"status": "DEMO", "tier": None, "name": None, "ai_cameras": 0}


def _override() -> int | None:
    raw = os.environ.get("GUARD_MAX_CAMERAS")
    return int(raw) if raw and raw.strip().isdigit() else None


class Licence:
    def __init__(self, db: Database) -> None:
        self.db = db
        # Called with the new limit when it goes DOWN, so the extra cameras stop.
        self.on_lower: Callable[[int], object] | None = None
        self._state = self._load()

    def _load(self) -> dict:
        with self.db.session() as s:
            row = s.get(Setting, KEY)
            if row:
                try:
                    d = json.loads(row.value)
                    if d.get("status") in STATUSES | {"GRANDFATHERED"}:
                        return d
                except ValueError:
                    pass
            # First start of a version with licences: an install already
            # protecting cameras keeps doing so.
            protecting = s.scalar(select(Camera.id).where(Camera.guard_enabled.is_(True)).limit(1)) is not None
        state = ({"status": "GRANDFATHERED", "tier": "BASIC", "name": "Guard Basic", "ai_cameras": LEGACY_CAMERAS}
                 if protecting else dict(DEMO))
        self._save(state)
        return state

    def _save(self, state: dict) -> None:
        with self.db.session() as s:
            row = s.get(Setting, KEY)
            if row:
                row.value = json.dumps(state)
            else:
                s.add(Setting(key=KEY, value=json.dumps(state)))

    # ── read ─────────────────────────────────────────────────────────
    def current(self) -> dict:
        return {**self._state, "limit": self.limit(), "protects": self.protects()}

    def limit(self) -> int:
        o = _override()
        if o is not None:
            return o
        if self._state.get("status") in ("ACTIVE", "LEGACY", "GRANDFATHERED"):
            return max(0, int(self._state.get("ai_cameras") or 0))
        return 0

    def protects(self) -> bool:
        return self.limit() > 0

    def is_demo(self) -> bool:
        return not self.protects()

    # ── write (cloud answers only) ───────────────────────────────────
    def update(self, wire: object) -> bool:
        """Apply a `licence` object from the cloud. Returns True if it changed."""
        if not isinstance(wire, dict) or wire.get("status") not in STATUSES:
            return False  # absent or malformed: an old cloud, never a reason to stop protecting
        try:
            cams = max(0, min(64, int(wire.get("ai_cameras") or 0)))
        except (TypeError, ValueError):
            return False
        new = {"status": wire["status"], "tier": wire.get("tier"), "name": wire.get("name"), "ai_cameras": cams}
        if new == {k: self._state.get(k) for k in new}:
            return False
        before = self.limit()
        self._state = new
        self._save(new)
        after = self.limit()
        self.db.audit("licence_changed", None, status=new["status"], tier=new["tier"], ai_cameras=cams)
        log.info("licence %s %s ai_cameras=%s (limit %s → %s)", new["status"], new["tier"], cams, before, after)
        if after < before and self.on_lower:
            try:
                self.on_lower(after)
            except Exception:
                log.exception("applying a lower camera limit failed")
        return True
