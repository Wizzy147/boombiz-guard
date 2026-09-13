"""Retention, Keep Evidence and disk protection (Phase 3 §40–45, §72, §81).

Policy match, most specific first: (type+severity) → type → severity →
default. Defaults: 30 days, LOW 14 days, INFO 7 days. Keep Evidence is never
deleted by retention — only by an owner, deliberately.

Expiry is a SOFT delete (§72): media files are removed, the row keeps its
metadata with deleted_at/ARCHIVED so the audit trail and review data stay.

Storage ceiling: min(10 GB, 5 % of the drive) for Guard media. Disk tiers
(free space on the Guard drive):

    > 10 GB   NORMAL
    5–10 GB   WARNING
    2–5 GB    ACCELERATED — expired media cleaned now, then oldest
              unkept non-critical media until back under the ceiling
    < 2 GB    CRITICAL — stop saving media for non-critical incidents
              (metadata still saved), show CRITICAL STORAGE WARNING

The POS drive is never filled by Guard.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ..database.db import Database
from ..database.models import Incident, RetentionPolicy

log = logging.getLogger(__name__)
GB = 1024 ** 3

DEFAULT_POLICIES = [(None, None, 30, False), (None, "LOW", 14, False), (None, "INFO", 7, False)]


class RetentionService:
    def __init__(self, db: Database, incidents_dir: Path, *, ceiling_bytes: int | None = None,
                 disk_probe=None) -> None:  # noqa: ANN001
        self.db = db
        self.dir = incidents_dir
        self._disk_probe = disk_probe or (lambda: shutil.disk_usage(self.dir if self.dir.exists() else self.dir.anchor))
        total = self._disk_probe().total
        self.ceiling = ceiling_bytes or min(10 * GB, int(total * 0.05))
        self.seed_defaults()

    def seed_defaults(self) -> None:
        with self.db.session() as s:
            if not s.scalar(select(RetentionPolicy)):
                for t, sev, days, keep in DEFAULT_POLICIES:
                    s.add(RetentionPolicy(incident_type=t, severity=sev, retention_days=days, keep_if_confirmed=keep))

    def policies(self) -> list[dict]:
        with self.db.session() as s:
            return [{"id": p.id, "incident_type": p.incident_type, "severity": p.severity,
                     "retention_days": p.retention_days, "keep_if_confirmed": p.keep_if_confirmed}
                    for p in s.scalars(select(RetentionPolicy))]

    def set_policy(self, incident_type: str | None, severity: str | None, days: int, keep_if_confirmed: bool) -> None:
        if not 1 <= days <= 365:
            raise ValueError("Keep incidents between 1 and 365 days.")
        with self.db.session() as s:
            p = s.scalar(select(RetentionPolicy).where(RetentionPolicy.incident_type.is_(incident_type) if incident_type is None
                                                        else RetentionPolicy.incident_type == incident_type,
                                                        RetentionPolicy.severity.is_(severity) if severity is None
                                                        else RetentionPolicy.severity == severity))
            if p is None:
                p = RetentionPolicy(incident_type=incident_type, severity=severity, retention_days=days)
                s.add(p)
            p.retention_days, p.keep_if_confirmed = days, keep_if_confirmed
        self.db.audit("configuration_changed", None, field="retention", incident_type=incident_type,
                      severity=severity, days=days)

    def _policy_for(self, pols: list[RetentionPolicy], itype: str, sev: str) -> RetentionPolicy | None:
        for match in ((itype, sev), (itype, None), (None, sev), (None, None)):
            p = next((p for p in pols if (p.incident_type, p.severity) == match), None)
            if p:
                return p
        return None

    # ── disk ─────────────────────────────────────────────────────────
    def media_bytes(self) -> int:
        with self.db.session() as s:
            return sum(i.media_bytes or 0 for i in s.scalars(select(Incident).where(Incident.deleted_at.is_(None))))

    def disk_level(self) -> str:
        free = self._disk_probe().free
        if free < 2 * GB:
            return "CRITICAL"
        if free < 5 * GB:
            return "ACCELERATED"
        if free < 10 * GB:
            return "WARNING"
        return "NORMAL"

    def media_allowed(self, severity: str) -> bool:
        """§45 <2 GB: only CRITICAL incidents keep saving media."""
        if self.disk_level() == "CRITICAL" and severity != "CRITICAL":
            return False
        return True

    def status(self) -> dict:
        du = self._disk_probe()
        used = self.media_bytes()
        level = self.disk_level()
        with self.db.session() as s:
            kept = sum(1 for _ in s.scalars(select(Incident.id).where(Incident.keep_evidence.is_(True),
                                                                      Incident.deleted_at.is_(None))))
        return {
            "guard_media_bytes": used, "ceiling_bytes": self.ceiling,
            "disk_free_bytes": du.free, "disk_total_bytes": du.total, "level": level,
            "kept_evidence": kept,
            "warning": {"WARNING": "Disk space is getting low.",
                        "ACCELERATED": "Disk space is low. Guard is removing old incident videos early.",
                        "CRITICAL": "CRITICAL STORAGE WARNING: Guard has stopped saving video for non-critical "
                                    "incidents. Free up space on this computer."}.get(level),
        }

    # ── cleanup ──────────────────────────────────────────────────────
    def _delete_media(self, inc: Incident) -> int:
        freed = 0
        folder = None
        for rel in (inc.snapshot_path, inc.clip_path):
            if not rel:
                continue
            p = (self.dir / rel).resolve()
            if self.dir.resolve() in p.parents and p.exists():
                freed += p.stat().st_size
                folder = p.parent
                p.unlink()
        if folder:
            for extra in folder.glob("*"):
                if extra.is_file():
                    freed += extra.stat().st_size
                    extra.unlink()
            try:
                folder.rmdir()
            except OSError:
                pass
        inc.snapshot_path = inc.clip_path = None
        inc.media_status, inc.media_bytes = "DELETED", 0
        return freed

    def soft_delete(self, inc: Incident, reason: str) -> int:
        freed = self._delete_media(inc)
        inc.deleted_at = datetime.now(timezone.utc)
        inc.status = "ARCHIVED"
        self.db.audit("retention_deleted" if reason != "manual" else "incident_deleted", inc.ref, reason=reason)
        return freed

    def cleanup(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        expired = freed = 0
        with self.db.session() as s:
            pols = list(s.scalars(select(RetentionPolicy)))
            live = list(s.scalars(select(Incident).where(Incident.deleted_at.is_(None))))
            for inc in live:
                if inc.keep_evidence:
                    continue
                p = self._policy_for(pols, inc.incident_type, inc.severity)
                if p is None:
                    continue
                if p.keep_if_confirmed and inc.status == "CONFIRMED":
                    continue
                occurred = inc.occurred_at if inc.occurred_at.tzinfo else inc.occurred_at.replace(tzinfo=timezone.utc)
                if now - occurred > timedelta(days=p.retention_days):
                    freed += self.soft_delete(inc, "expired")
                    expired += 1
            # Over the ceiling or low disk: oldest unkept, non-critical media first.
            level = self.disk_level()
            used = sum(i.media_bytes or 0 for i in live if i.deleted_at is None)
            if used > self.ceiling or level in ("ACCELERATED", "CRITICAL"):
                candidates = sorted((i for i in live if i.deleted_at is None and not i.keep_evidence
                                     and i.media_bytes and i.severity != "CRITICAL"), key=lambda i: i.occurred_at)
                for inc in candidates:
                    if used <= self.ceiling * 0.8 and self.disk_level() not in ("ACCELERATED", "CRITICAL"):
                        break
                    b = inc.media_bytes or 0
                    freed += self._delete_media(inc)
                    used -= b
                    self.db.audit("retention_deleted", inc.ref, reason="storage_pressure")
        log.info("retention cleanup expired=%s freed=%s", expired, freed)
        return {"expired": expired, "freed_bytes": freed, "level": self.disk_level()}
