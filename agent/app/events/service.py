"""Event service — dedup, persist to ai_events, keep a live feed (§30–32, §52).

Only metadata is stored: never a frame, never an image (§52, §62). Every
row carries the model versions that produced it (§53).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..database.db import Database
from ..database.models import AiEvent
from .dedup import Deduper

log = logging.getLogger(__name__)

EVENT_TYPES = {
    "PERSON_DETECTED", "ZONE_ENTRY", "ZONE_EXIT", "SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION",
    "EXIT_APPROACH", "POSSIBLE_UNPAID_EXIT", "RESTRICTED_ZONE_ENTRY", "AFTER_HOURS_PERSON",
    "POSSIBLE_CONCEALMENT", "POSSIBLE_SMOKE", "POSSIBLE_FIRE", "TRACK_LOST",
}
# The ones a person should look at; the rest are context for correlation/debug.
SECURITY_TYPES = {"POSSIBLE_UNPAID_EXIT", "RESTRICTED_ZONE_ENTRY", "AFTER_HOURS_PERSON",
                  "POSSIBLE_FIRE", "POSSIBLE_SMOKE", "POSSIBLE_CONCEALMENT", "UNRESOLVED_SHELF_INTERACTION"}


@dataclass
class EventDraft:
    camera_id: str
    event_type: str
    track_id: str | None = None
    zone_id: str | None = None
    severity: str = "INFO"
    confidence: str | None = None
    metadata: dict = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    dedup_ttl: float = 10.0


class EventService:
    def __init__(self, db: Database, *, persist_types: set[str] | None = None) -> None:
        self.db = db
        self.dedup = Deduper()
        self.feed: deque[dict] = deque(maxlen=500)
        # Everything is persisted by default; PERSON_DETECTED etc. are already
        # once-per-track, so the table grows with people, not with frames.
        self.persist_types = persist_types or EVENT_TYPES

    def emit(self, d: EventDraft, versions: dict[str, str] | None = None) -> dict | None:
        if d.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {d.event_type}")
        if not self.dedup.allow(d.camera_id, d.track_id, d.event_type, d.zone_id, ttl=d.dedup_ttl):
            return None
        meta = {**d.metadata, **({"models": versions} if versions else {})}
        row = AiEvent(camera_id=d.camera_id, track_id=d.track_id, event_type=d.event_type,
                      severity=d.severity, confidence=d.confidence, zone_id=d.zone_id,
                      metadata_json=json.dumps(meta, default=str), occurred_at=d.occurred_at)
        if d.event_type in self.persist_types:
            with self.db.session() as s:
                s.add(row)
                s.flush()
                event_id = row.id
        else:
            event_id = None
        out = self._to_dict(row, event_id)
        self.feed.append(out)
        log.info("event_generated type=%s camera=%s track=%s zone=%s confidence=%s",
                 d.event_type, d.camera_id, d.track_id, d.zone_id, d.confidence)
        return out

    @staticmethod
    def _to_dict(r: AiEvent, event_id: str | None = None) -> dict:
        return {
            "id": event_id or r.id, "camera_id": r.camera_id, "track_id": r.track_id, "event_type": r.event_type,
            "severity": r.severity, "confidence": r.confidence, "zone_id": r.zone_id,
            "metadata": json.loads(r.metadata_json or "{}"),
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "feedback": r.feedback, "feedback_note": r.feedback_note,
        }

    def list(self, *, camera_id: str | None = None, event_type: str | None = None, security_only: bool = False,
             limit: int = 200) -> list[dict]:
        with self.db.session() as s:
            q = select(AiEvent).order_by(AiEvent.occurred_at.desc()).limit(min(max(limit, 1), 1000))
            if camera_id:
                q = q.where(AiEvent.camera_id == camera_id)
            if event_type:
                q = q.where(AiEvent.event_type == event_type)
            if security_only:
                q = q.where(AiEvent.event_type.in_(SECURITY_TYPES))
            return [self._to_dict(r) for r in s.scalars(q)]

    def get(self, event_id: str) -> dict | None:
        with self.db.session() as s:
            r = s.get(AiEvent, event_id)
            return self._to_dict(r) if r else None

    def set_feedback(self, event_id: str, feedback: str, note: str | None) -> dict | None:
        with self.db.session() as s:
            r = s.get(AiEvent, event_id)
            if not r:
                return None
            r.feedback = feedback
            r.feedback_note = (note or "")[:500] or None
        self.db.audit("event_feedback", event_id, feedback=feedback)
        return self.get(event_id)
