"""Heartbeat to the Boombiz cloud (Phase 4 §44), every ~2 minutes.

What it sends: agent version, camera NAMES and online/offline, CPU/RAM/disk,
AI frames per second, whether an alarm output is failing, how many items are
waiting to upload, and when the last incident happened.

What it never sends (§79): video, snapshots, camera IP addresses, stream
URLs, usernames or passwords.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

import httpx
from sqlalchemy import func, select

from ..database.models import AlarmOutput, Camera, Incident, iso_utc
from ..health.system import system_snapshot
from .auth import AuthRejected

if TYPE_CHECKING:
    from ..ai.service import AIService
    from ..database.db import Database
    from ..services.streams import StreamManager
    from .client import CloudLink

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 120
CAMERA_UP = {"ONLINE", "DEGRADED"}  # DEGRADED still delivers frames


def collect_health(db: "Database", streams: "StreamManager", ai: "AIService", link: "CloudLink",
                   version: str, sync=None) -> dict:  # noqa: ANN001 — SyncQueue, optional
    sysh = system_snapshot()
    health = streams.health()
    with db.session() as s:
        cams = [{"id": c.id, "name": (c.name or c.id)[:80],
                 "online": (health.get(c.id) or {}).get("status") in CAMERA_UP}
                for c in s.scalars(select(Camera).where(Camera.guard_enabled.is_(True)).order_by(Camera.name))]
        outputs = list(s.scalars(select(AlarmOutput).where(AlarmOutput.enabled.is_(True))))
        last_incident = s.scalar(select(func.max(Incident.occurred_at)).where(Incident.deleted_at.is_(None)))
    ai_status = ai.status()
    fps = [c.get("ai_fps") or 0.0 for c in ai_status.get("cameras", [])]
    return {
        "agent_version": version,
        "device_status": "ACTIVE",
        "camera_online": sum(1 for c in cams if c["online"]),
        "camera_total": len(cams),
        "cameras": cams,
        "cpu_percent": sysh["cpu_percent"],
        "ram_percent": sysh["ram_percent"],
        "disk_free_gb": sysh["disk_free_gb"],
        "ai_fps": round(sum(fps) / len(fps), 1) if fps else 0.0,
        "ai_running": bool(ai_status.get("running")),
        # None when no alarm output is configured: nothing to be broken.
        "alarm_available": (all(o.health not in ("UNAVAILABLE", "ERROR") for o in outputs) if outputs else None),
        "queue_size": link.queued() + (sync.pending() if sync else 0),
        "bandwidth_mode": sync.bandwidth.mode() if sync else None,
        "last_incident_at": iso_utc(last_incident),
    }


class Heartbeat:
    def __init__(self, link: "CloudLink", collect: Callable[[], dict]) -> None:
        self.link = link
        self.collect = collect

    async def beat(self) -> dict | None:
        device_id = self.link.device_id()
        if not self.link.token() or not device_id:
            return None
        payload = self.collect()
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                for attempt in (1, 2):
                    headers = await self.link.auth.headers(c)
                    if headers is None:
                        return None
                    r = await c.post(f"{self.link.base}/api/guard/v1/devices/{device_id}/heartbeat",
                                     json=payload, headers=headers)
                    if r.status_code == 401 and attempt == 1 and not self.link.auth.legacy:
                        self.link.auth.invalidate()  # token revoked/rotated: re-auth once
                        continue
                    break
        except AuthRejected as e:
            self.link.state.update(paired=False, online=True, last_error=str(e))
            return None
        except httpx.HTTPError:
            self.link.state["online"] = False
            return None
        if r.status_code == 404:
            return None  # cloud without heartbeats yet (or legacy secret on a new route)
        self.link.state["online"] = True
        if r.status_code != 200:
            log.info("heartbeat refused: HTTP %s", r.status_code)
            return None
        d = r.json()
        self.link.state.update(health_status=d.get("status"), health_reasons=d.get("reasons", []),
                               last_heartbeat_at=datetime.now(timezone.utc).isoformat())
        if "paired" in d:  # the heartbeat doubles as the link check
            self.link.state.update(paired=bool(d["paired"]), business_name=d.get("business_name"),
                                   location_name=d.get("location_name"), last_error=None)
            if d["paired"]:
                self.link.state.update(pairing_code=None, pairing_expires_at=None)
            self.link.mark_link_checked()
        return d
