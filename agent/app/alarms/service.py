"""Local alarm engine (Phase 3 §26–31, §64–65).

For each new incident: matching rules → outputs → activate for the rule's
duration, unless that rule is inside its cooldown. During cooldown the
incident is still recorded; only the siren stays quiet (§30).

Fire (§31) is special: a rule with `repeat_until_ack` keeps re-sounding
every cooldown period while the incident is still UNREVIEWED, up to a safety
cap, instead of going quiet after one burst.

An output that fails is marked ERROR and an ALARM_OUTPUT_FAILURE is audited
— the incident itself is never lost because a siren didn't answer (§79 T8).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from ..adapters.base import Credentials
from ..database.db import Database
from ..database.models import AlarmOutput, AlarmRule, Device, Incident, iso_utc
from ..incidents.classifier import at_least
from ..security.vault import CredentialVault
from .adapters import (
    AlarmAdapter,
    AlarmError,
    DahuaIOAdapter,
    HikvisionIOAdapter,
    NetworkRelayAdapter,
    PCSoundAdapter,
    USBRelayAdapter,
)

log = logging.getLogger(__name__)

# Phase 3 §29 defaults. enabled=False means "configurable, off until chosen".
DEFAULT_RULES = [
    ("POSSIBLE_UNPAID_EXIT", False, 3, 30, False),
    ("POSSIBLE_CONCEALMENT", False, 3, 30, False),
    ("RESTRICTED_AREA_INCIDENT", False, 5, 30, False),
    ("AFTER_HOURS_INTRUSION", True, 10, 60, False),
    ("POSSIBLE_SMOKE", True, 10, 30, False),
    ("POSSIBLE_FIRE", True, 10, 30, True),
    ("CAMERA_OFFLINE", False, 3, 300, False),
    ("GUARD_PROTECTION_DEGRADED", False, 3, 300, False),
]
MAX_FIRE_REPEATS = 20


class AlarmService:
    def __init__(self, db: Database, vault: CredentialVault) -> None:
        self.db = db
        self.vault = vault
        self._last_fired: dict[str, float] = {}  # rule id → monotonic
        self._repeat_tasks: dict[str, asyncio.Task] = {}
        self.seed_defaults()

    def seed_defaults(self) -> None:
        with self.db.session() as s:
            have = {r.incident_type for r in s.scalars(select(AlarmRule))}
            for t, enabled, dur, cool, repeat in DEFAULT_RULES:
                if t not in have:
                    s.add(AlarmRule(incident_type=t, enabled=enabled, duration_seconds=dur,
                                    cooldown_seconds=cool, repeat_until_ack=repeat))
            if not s.scalar(select(AlarmOutput).where(AlarmOutput.kind == "PC_SOUND")):
                s.add(AlarmOutput(name="This computer's speaker", kind="PC_SOUND"))

    # ── adapters ─────────────────────────────────────────────────────
    def _adapter(self, o: AlarmOutput) -> AlarmAdapter:
        cfg = json.loads(o.config_json or "{}")
        if o.kind == "PC_SOUND":
            return PCSoundAdapter()
        if o.kind in ("HIKVISION_IO", "DAHUA_IO"):
            with self.db.session() as s:
                dev = s.get(Device, o.device_id or "")
                if not dev:
                    raise AlarmError("The recorder for this alarm output is no longer set up.")
                host, port = dev.ip_address, cfg.get("http_port") or dev.port
            c = self.vault.get_credentials(o.device_id)
            creds = Credentials(*c) if c else None
            cls = HikvisionIOAdapter if o.kind == "HIKVISION_IO" else DahuaIOAdapter
            return cls(host, port, int(cfg.get("output", 1)), creds)
        if o.kind == "NETWORK_RELAY":
            return NetworkRelayAdapter(cfg["on_url"], cfg["off_url"], cfg.get("status_url"))
        if o.kind == "USB_RELAY":
            return USBRelayAdapter(cfg["port"], int(cfg.get("baud", 9600)))
        raise AlarmError("Unknown alarm type.")

    def outputs(self) -> list[dict]:
        with self.db.session() as s:
            return [{"id": o.id, "name": o.name, "kind": o.kind, "device_id": o.device_id, "enabled": o.enabled,
                     "health": o.health, "last_error": o.last_error,
                     "config": json.loads(o.config_json or "{}"),
                     "last_tested_at": iso_utc(o.last_tested_at)}
                    for o in s.scalars(select(AlarmOutput))]

    def rules(self) -> list[dict]:
        with self.db.session() as s:
            return [{"id": r.id, "incident_type": r.incident_type, "min_severity": r.min_severity,
                     "enabled": r.enabled, "alarm_output_id": r.alarm_output_id,
                     "duration_seconds": r.duration_seconds, "cooldown_seconds": r.cooldown_seconds,
                     "repeat_until_ack": r.repeat_until_ack} for r in s.scalars(select(AlarmRule))]

    def _set_health(self, output_id: str, health: str, error: str | None) -> None:
        with self.db.session() as s:
            o = s.get(AlarmOutput, output_id)
            if o:
                o.health, o.last_error = health, error

    async def _fire_outputs(self, output_ids: list[str], seconds: int, incident_ref: str | None) -> bool:
        ok_any = False
        with self.db.session() as s:
            outs = [o for o in s.scalars(select(AlarmOutput).where(AlarmOutput.enabled.is_(True)))
                    if not output_ids or o.id in output_ids]
            snapshot = [(o.id, o.name, o.kind, o) for o in outs]
        for oid, name, kind, o in snapshot:
            try:
                await self._adapter(o).activate(seconds)
                self._set_health(oid, "AVAILABLE", None)
                self.db.audit("alarm_triggered", oid, output=name, seconds=seconds, incident=incident_ref)
                ok_any = True
            except (AlarmError, KeyError) as e:
                msg = str(e) if isinstance(e, AlarmError) else "The alarm output is missing settings."
                self._set_health(oid, "ERROR", msg)
                self.db.audit("ALARM_OUTPUT_FAILURE", oid, output=name, error=msg, incident=incident_ref)
                log.warning("alarm output %s failed: %s", kind, msg)
        return ok_any

    # ── incidents ────────────────────────────────────────────────────
    async def on_incident(self, incident_id: str) -> str:
        """→ alarm_state: TRIGGERED | COOLDOWN | FAILED | NONE"""
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if not inc:
                return "NONE"
            itype, sev, ref = inc.incident_type, inc.severity, inc.ref
            rules = [r for r in s.scalars(select(AlarmRule).where(AlarmRule.incident_type == itype,
                                                                   AlarmRule.enabled.is_(True)))
                     if at_least(sev, r.min_severity)]
            rules = [(r.id, r.alarm_output_id, r.duration_seconds, r.cooldown_seconds, r.repeat_until_ack) for r in rules]
        state = "NONE"
        for rid, out_id, dur, cool, repeat in rules:
            now = time.monotonic()
            if now - self._last_fired.get(rid, -1e9) < cool and not repeat:
                state = "COOLDOWN" if state == "NONE" else state
                continue
            self._last_fired[rid] = now
            ok = await self._fire_outputs([out_id] if out_id else [], dur, ref)
            state = "TRIGGERED" if ok else "FAILED"
            if repeat and incident_id not in self._repeat_tasks:
                self._repeat_tasks[incident_id] = asyncio.create_task(
                    self._repeat_until_ack(incident_id, out_id, dur, cool, ref))
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if inc:
                inc.alarm_state = state
        return state

    async def _repeat_until_ack(self, incident_id: str, out_id: str | None, dur: int, cool: int, ref: str) -> None:
        try:
            for _ in range(MAX_FIRE_REPEATS):
                await asyncio.sleep(max(5, cool))
                with self.db.session() as s:
                    inc = s.get(Incident, incident_id)
                    if not inc or inc.status != "UNREVIEWED":
                        return
                await self._fire_outputs([out_id] if out_id else [], dur, ref)
        finally:
            self._repeat_tasks.pop(incident_id, None)

    def stop_repeat(self, incident_id: str) -> None:
        t = self._repeat_tasks.pop(incident_id, None)
        if t:
            t.cancel()

    async def test(self, output_id: str, seconds: int = 2) -> dict:
        seconds = max(1, min(3, seconds))  # §64: 1–3 s
        with self.db.session() as s:
            o = s.get(AlarmOutput, output_id)
            if not o:
                raise AlarmError("That alarm output no longer exists.")
            o.last_tested_at = datetime.now(timezone.utc)
        ok = await self._fire_outputs([output_id], seconds, None)
        self.db.audit("alarm_tested", output_id, ok=ok)
        return {"ok": ok, "output": next(o for o in self.outputs() if o["id"] == output_id)}

    async def check_health(self) -> None:
        with self.db.session() as s:
            outs = list(s.scalars(select(AlarmOutput).where(AlarmOutput.enabled.is_(True))))
        for o in outs:
            try:
                ok = await self._adapter(o).available()
            except (AlarmError, KeyError):
                ok = False
            before = o.health
            self._set_health(o.id, "AVAILABLE" if ok else "UNAVAILABLE", None if ok else o.last_error)
            if before == "AVAILABLE" and not ok:
                self.db.audit("ALARM_OUTPUT_FAILURE", o.id, output=o.name, error="Output stopped responding")

    def add_output(self, name: str, kind: str, device_id: str | None, config: dict) -> dict:
        # Validate before saving: a relay URL that points off the LAN is refused here.
        probe = AlarmOutput(name=name, kind=kind, device_id=device_id, config_json=json.dumps(config))
        self._adapter(probe)
        with self.db.session() as s:
            s.add(probe)
            s.flush()
            oid = probe.id
        self.db.audit("alarm_output_added", oid, kind=kind)
        return next(o for o in self.outputs() if o["id"] == oid)

    def update_rule(self, rule_id: str, **fields) -> dict:  # noqa: ANN003
        with self.db.session() as s:
            r = s.get(AlarmRule, rule_id)
            if not r:
                raise AlarmError("That alarm rule no longer exists.")
            for k, v in fields.items():
                if v is not None and hasattr(r, k):
                    setattr(r, k, v)
        self.db.audit("configuration_changed", rule_id, field="alarm_rule")
        return next(x for x in self.rules() if x["id"] == rule_id)
