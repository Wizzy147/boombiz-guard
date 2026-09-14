"""What pops up (user decision 2026-09-13): HIGH + CRITICAL incidents and
health problems — never LOW, never plain AI events.

    POSSIBLE_UNPAID_EXIT, POSSIBLE_PRODUCT_REPLACEMENT, RESTRICTED_AREA_INCIDENT, AFTER_HOURS_INTRUSION,
    POSSIBLE_FIRE, POSSIBLE_SMOKE, MANUAL (HIGH/CRITICAL)
    CAMERA_OFFLINE once it's HIGH (offline > 5 min)
    GUARD_PROTECTION_DEGRADED (all cameras down)
    ALARM_OUTPUT_FAILURE (a siren/relay didn't answer)

One feed, two readers: the Windows tray app polls it locally; the cloud
outbox sends the same items to phones. Items are keyed (id, severity) so a
camera that goes from LOW to HIGH pops once, when it becomes HIGH.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from ..database.db import Database
from ..database.models import AuditLog, Camera, Incident, iso_utc

POP_SEVERITIES = {"HIGH", "CRITICAL"}
FIRE_TYPES = {"POSSIBLE_FIRE", "POSSIBLE_SMOKE"}

# Mute groups a person can switch off (tray menu / phone settings).
GROUPS = {
    "POSSIBLE_UNPAID_EXIT": "unpaid_exit",
    "POSSIBLE_PRODUCT_REPLACEMENT": "unpaid_exit",
    "POSSIBLE_CONCEALMENT": "unpaid_exit",
    "RESTRICTED_AREA_INCIDENT": "restricted",
    "AFTER_HOURS_INTRUSION": "after_hours",
    "POSSIBLE_FIRE": "fire",
    "POSSIBLE_SMOKE": "fire",
    "CAMERA_OFFLINE": "health",
    "GUARD_PROTECTION_DEGRADED": "health",
    "ALARM_OUTPUT_FAILURE": "health",
    "MANUAL_SECURITY_INCIDENT": "manual",
}


def _parse(after: str | None) -> datetime | None:
    if not after:
        return None
    d = datetime.fromisoformat(after)
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def incident_item(i: Incident, camera: str) -> dict:
    return {
        "key": f"{i.id}:{i.severity}", "kind": "incident", "incident_id": i.id, "ref": i.ref,
        "incident_type": i.incident_type, "group": GROUPS.get(i.incident_type, "other"),
        # A staff report's own words ("forced entry at the back door") say more
        # than "Reported by staff".
        "title": ((i.description or "")[:90] if i.incident_type == "MANUAL_SECURITY_INCIDENT" and i.description
                  else i.title or i.incident_type.replace("_", " ").capitalize()),
        "severity": i.severity,
        "camera": camera, "occurred_at": iso_utc(i.occurred_at), "at": iso_utc(i.updated_at or i.created_at),
        "fire": i.incident_type in FIRE_TYPES,
    }


def pops(i: Incident) -> bool:
    return i.severity in POP_SEVERITIES and i.deleted_at is None


def feed(db: Database, after: str | None, limit: int = 50) -> dict:
    since = _parse(after)
    with db.session() as s:
        names = {c.id: c.name or c.id for c in s.scalars(select(Camera))}
        q = select(Incident).where(Incident.severity.in_(POP_SEVERITIES), Incident.deleted_at.is_(None))
        if since:
            q = q.where(Incident.updated_at > since)
        items = [incident_item(i, names.get(i.camera_id, i.camera_id))
                 for i in s.scalars(q.order_by(Incident.updated_at).limit(limit))]
        aq = select(AuditLog).where(AuditLog.action == "ALARM_OUTPUT_FAILURE")
        if since:
            aq = aq.where(AuditLog.at > since)
        for a in s.scalars(aq.order_by(AuditLog.at).limit(limit)):
            d = json.loads(a.detail or "{}")
            items.append({
                "key": f"alarm:{a.id}", "kind": "alarm_failure", "incident_id": None, "ref": d.get("incident"),
                "incident_type": "ALARM_OUTPUT_FAILURE", "group": "health",
                "title": f"Alarm not responding: {d.get('output', 'alarm output')}", "severity": "HIGH",
                "camera": None, "occurred_at": iso_utc(a.at), "at": iso_utc(a.at), "fire": False,
            })
    items.sort(key=lambda x: x["at"] or "")
    return {"items": items[:limit], "cursor": items[-1]["at"] if items else after,
            "now": datetime.now(timezone.utc).isoformat()}
