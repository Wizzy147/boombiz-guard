"""Incident sync to the Boombiz cloud (Phase 4 §15–22, §50, §97).

    incident created/changed locally
          │  scan() every 5 s (cursor on incidents.updated_at)
          ▼
    sync_queue ── INCIDENT_UPSERT  (fire 10, after-hours 20, unpaid exit 30,
             │                      restricted 35, health 40, other 70)
             ├── SNAPSHOT_UPLOAD  (50; fire 15)   waits for the cloud incident id
             └── CLIP_UPLOAD      (60; fire 25)   HIGH / CRITICAL only
          │  process() every 5 s, lowest priority number first
          ▼
    POST /api/guard/v1/incidents                    (idempotent per incident)
    POST /api/guard/v1/incidents/{id}/uploads       → presigned PUT
    PUT  <bucket>                                    (decrypted in memory, SHA-256 signed)
    POST /api/guard/v1/incidents/{id}/uploads/complete

So after an outage: fire first, then after-hours, unpaid exits, health;
metadata before snapshots, snapshots before clips — the owner gets something
useful before the video finishes (§97).

Never on the detection path: if the cloud is down, jobs wait here. Only
incidents after this PC was connected are synced — never history.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import func, select

from ..database.models import Camera, Incident, SyncJob, iso_utc
from ..media.compress import shrink_clip, shrink_snapshot
from ..media.encryption import MediaCryptoError
from .auth import AuthRejected
from .bandwidth import Bandwidth

if TYPE_CHECKING:
    from ..database.db import Database
    from ..media.worker import MediaWorker
    from .client import CloudLink

log = logging.getLogger(__name__)

UPSERT, SNAPSHOT, CLIP = "INCIDENT_UPSERT", "SNAPSHOT_UPLOAD", "CLIP_UPLOAD"
BACKOFF = (5, 15, 30, 60, 120, 300)
MEDIA_MAX_ATTEMPTS = 8
NOT_READY_RETRY_S = 300  # cloud without incident sync yet (404)

# §17 order ×10, leaving room for the types the spec doesn't list: restricted
# area goes after unpaid exits and before health (§50's example order).
TYPE_PRIORITY = {
    "POSSIBLE_FIRE": 10, "POSSIBLE_SMOKE": 10,
    "AFTER_HOURS_INTRUSION": 20,
    "POSSIBLE_UNPAID_EXIT": 30, "POSSIBLE_CONCEALMENT": 30,
    "RESTRICTED_AREA_INCIDENT": 35,
    "CAMERA_OFFLINE": 40, "GUARD_PROTECTION_DEGRADED": 40, "ALARM_OUTPUT_FAILURE": 40,
}
FIRE = {"POSSIBLE_FIRE", "POSSIBLE_SMOKE"}
CLIP_SEVERITIES = {"HIGH", "CRITICAL"}
MEDIA_TYPES = {SNAPSHOT: ("SNAPSHOT", "image/jpeg"), CLIP: ("CLIP", "video/mp4")}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def meta_priority(incident_type: str, severity: str) -> int:
    p = TYPE_PRIORITY.get(incident_type, 70)
    if incident_type == "MANUAL_SECURITY_INCIDENT":
        p = 20 if severity == "CRITICAL" else 30 if severity == "HIGH" else 70
    return p


def media_priority(op: str, incident_type: str) -> int:
    # Fire media jumps the queue: the owner should see the smoke before anything else.
    if incident_type in FIRE:
        return 15 if op == SNAPSHOT else 25
    return 50 if op == SNAPSHOT else 60


class SyncQueue:
    def __init__(self, db: "Database", link: "CloudLink", media: "MediaWorker") -> None:
        self.db = db
        self.link = link
        self.media = media
        self.bandwidth = Bandwidth(link)
        self.warning: str | None = None
        with db.session() as s:  # a crash mid-send: send it again
            for j in s.scalars(select(SyncJob).where(SyncJob.status == "PROCESSING")):
                j.status = "PENDING"

    # ── queueing ─────────────────────────────────────────────────────
    def scan(self) -> int:
        """Queue incidents created or changed since the cursor. Returns jobs (re)queued."""
        if not self.link.token():
            return 0
        cursor = self.link._get("sync_cursor")
        if cursor is None:  # first run after connecting: start now — never push history
            self.link._put("sync_cursor", _now().isoformat())
            return 0
        since = datetime.fromisoformat(cursor)
        since = since.astimezone(timezone.utc).replace(tzinfo=None) if since.tzinfo else since
        queued = 0
        last = None
        with self.db.session() as s:
            rows = list(s.scalars(select(Incident).where(Incident.updated_at > since)
                                  .order_by(Incident.updated_at).limit(200)))
            for i in rows:
                last = i.updated_at
                if i.deleted_at is not None or i.severity == "INFO":
                    continue
                queued += self._want(s, UPSERT, i.id, meta_priority(i.incident_type, i.severity), reset=True)
                if i.snapshot_path:
                    queued += self._want(s, SNAPSHOT, i.id, media_priority(SNAPSHOT, i.incident_type))
                if i.clip_path and i.severity in CLIP_SEVERITIES:
                    queued += self._want(s, CLIP, i.id, media_priority(CLIP, i.incident_type))
        if last is not None:
            self.link._put("sync_cursor", iso_utc(last))
        self.enforce_cap()
        return queued

    # ── §99 backlog cap ──────────────────────────────────────────────
    def _backlog(self) -> list[dict]:
        with self.db.session() as s:
            rows = s.execute(
                select(SyncJob.id, SyncJob.operation_type, Incident.severity, Incident.incident_type,
                       Incident.keep_evidence, Incident.snapshot_path, Incident.clip_path, Incident.occurred_at)
                .join(Incident, Incident.id == SyncJob.resource_id)
                .where(SyncJob.operation_type.in_((SNAPSHOT, CLIP)), SyncJob.status.in_(("PENDING", "RETRY")))
            ).all()
        out = []
        for jid, op, sev, itype, keep, snap, clip, occurred in rows:
            rel = snap if op == SNAPSHOT else clip
            p = self.media.dir / rel if rel else None
            size = p.stat().st_size if p is not None and p.exists() else 0
            out.append({"id": jid, "op": op, "size": size, "occurred_at": occurred,
                        "protected": sev == "CRITICAL" or itype in FIRE or bool(keep)})
        return out

    def enforce_cap(self) -> int:
        """Keep waiting media under the cap. Critical, fire and kept evidence are
        never skipped; of the rest, clips go before snapshots and oldest before
        newest. Skipped files stay on this PC — they just don't go to the cloud."""
        cap = self.bandwidth.cap_bytes()
        items = self._backlog()
        total = sum(i["size"] for i in items)
        if total <= cap:
            self.warning = None
            return 0
        droppable = sorted((i for i in items if not i["protected"]),
                           key=lambda i: (0 if i["op"] == CLIP else 1, i["occurred_at"]))
        skipped = []
        for it in droppable:
            if total <= cap:
                break
            skipped.append(it["id"])
            total -= it["size"]
        if skipped:
            with self.db.session() as s:
                for jid in skipped:
                    j = s.get(SyncJob, jid)
                    j.status, j.last_error = "SKIPPED", "Not uploaded: too much was waiting while Boombiz was unreachable."
            self.db.audit("cloud_backlog_trimmed", None, skipped=len(skipped), cap_bytes=cap)
            log.warning("cloud backlog over %s bytes: skipped %s media uploads", cap, len(skipped))
        gb = round(cap / 1024**3, 1)
        self.warning = (
            f"Too much video was waiting to upload (limit {gb} GB), so {len(skipped)} older "
            f"snapshot/clip upload{'s were' if len(skipped) != 1 else ' was'} skipped. Incident details, critical "
            "incidents and kept evidence still upload. The files are still on this computer."
            if total <= cap else
            f"More than {gb} GB of critical or kept evidence is waiting to upload. It will all be sent — check this "
            "computer's internet connection."
        )
        return len(skipped)

    @staticmethod
    def _want(s, op: str, rid: str, priority: int, reset: bool = False) -> int:  # noqa: ANN001
        job = s.scalar(select(SyncJob).where(SyncJob.operation_type == op, SyncJob.resource_id == rid))
        if job is None:
            s.add(SyncJob(operation_type=op, resource_id=rid, priority=priority))
            return 1
        if reset and job.status in ("COMPLETED", "FAILED", "PROCESSING"):
            # PROCESSING too: the send in flight carries the old state, so it must go again.
            job.status, job.attempts, job.next_attempt_at, job.priority = "PENDING", 0, _now(), priority
            return 1
        return 0

    def pending(self) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count(SyncJob.id)).where(
                SyncJob.status.in_(("PENDING", "RETRY", "PROCESSING")))) or 0

    def status(self) -> dict:
        with self.db.session() as s:
            counts = dict(s.execute(select(SyncJob.status, func.count(SyncJob.id)).group_by(SyncJob.status)).all())
        return {"pending": sum(counts.get(k, 0) for k in ("PENDING", "RETRY", "PROCESSING")),
                "failed": counts.get("FAILED", 0), "completed": counts.get("COMPLETED", 0),
                "skipped": counts.get("SKIPPED", 0), "warning": self.warning,
                "bandwidth": self.bandwidth.get()}

    # ── sending ──────────────────────────────────────────────────────
    async def process(self, now: datetime | None = None, limit: int = 10) -> dict:
        now = now or _now()
        done = retried = 0
        if not self.link.token() or not self.link.device_id():
            return {"done": 0, "retried": 0, "pending": self.pending()}
        with self.db.session() as s:
            candidates = [(j.id, j.operation_type, j.resource_id) for j in s.scalars(
                select(SyncJob).where(SyncJob.status.in_(("PENDING", "RETRY")))
                .order_by(SyncJob.priority, SyncJob.created_at).limit(100))
                if _aware(j.next_attempt_at) <= now]
        if not candidates:
            return {"done": 0, "retried": 0, "pending": self.pending()}

        async with httpx.AsyncClient(timeout=30) as c:
            try:
                headers = await self.link.auth.headers(c)
            except AuthRejected as e:
                self.link.state.update(paired=False, online=True, last_error=str(e))
                return {"done": 0, "retried": 0, "pending": self.pending()}
            except httpx.HTTPError:
                self.link.state["online"] = False
                return {"done": 0, "retried": 0, "pending": self.pending()}
            if headers is None:
                return {"done": 0, "retried": 0, "pending": self.pending()}

            ran = 0
            metadata_only = self.bandwidth.mode() == "METADATA_ONLY"
            for jid, op, rid in candidates:
                if ran >= limit:
                    break
                cloud_id = None
                if op != UPSERT:
                    if metadata_only:
                        continue  # media waits until the mode is switched back (or expires)
                    cloud_id = self._cloud_id(rid)
                    if cloud_id is None:
                        continue  # metadata hasn't reached the cloud yet
                if not self._claim(jid):
                    continue
                ran += 1
                try:
                    outcome = await (self._upsert(c, headers, jid, rid) if op == UPSERT
                                     else self._upload(c, headers, op, rid, cloud_id))
                except httpx.HTTPError:
                    self._retry(jid, "offline", now)
                    self.link.state["online"] = False
                    retried += 1
                    break  # no point hammering a dead connection
                self.link.state["online"] = True
                kind = outcome[0]
                if kind == "done":
                    done += self._complete(jid, outcome[1] if len(outcome) > 1 else None)
                elif kind == "auth":
                    self.link.auth.invalidate()
                    self._retry(jid, "sign-in expired", now, delay=5)
                    retried += 1
                    break
                elif kind == "retry":
                    self._retry(jid, outcome[1], now, delay=outcome[2] if len(outcome) > 2 else None, media=op != UPSERT)
                    retried += 1
                else:
                    self._fail(jid, outcome[1])
        return {"done": done, "retried": retried, "pending": self.pending()}

    # ── one job ──────────────────────────────────────────────────────
    async def _upsert(self, c: httpx.AsyncClient, headers: dict, jid: str, rid: str) -> tuple:
        payload = self._payload(rid)
        if payload is None:
            return ("failed", "The incident was deleted on this computer.")
        key = f"{self.link.device_id()}:{payload['ref']}:upsert"
        r = await c.post(f"{self.link.base}/api/guard/v1/incidents", json=payload,
                         headers={**headers, "Idempotency-Key": key})
        res = _classify(r)
        if res[0] == "done":
            return ("done", r.json()["incident_id"])
        return res

    async def _upload(self, c: httpx.AsyncClient, headers: dict, op: str, rid: str, cloud_id: str) -> tuple:
        media_type, content_type = MEDIA_TYPES[op]
        with self.db.session() as s:
            inc = s.get(Incident, rid)
            if inc is None or inc.deleted_at is not None:
                return ("failed", "The incident was deleted on this computer.")
            rel, ref = (inc.snapshot_path if op == SNAPSHOT else inc.clip_path), inc.ref
        try:
            data = self.media.read(rel, ref)  # decrypted in memory; plaintext never touches disk
        except MediaCryptoError as e:
            return ("failed", str(e))
        if data is None:
            return ("failed", "The file is no longer on this computer.")
        if self.bandwidth.mode() == "LOW":  # the cloud copy only; local evidence stays full quality
            data = shrink_snapshot(data) if op == SNAPSHOT else shrink_clip(data)
        digest = hashlib.sha256(data).hexdigest()

        base = f"{self.link.base}/api/guard/v1/incidents/{cloud_id}/uploads"
        r = await c.post(base, json={"media_type": media_type, "content_type": content_type,
                                     "size_bytes": len(data), "sha256": digest}, headers=headers)
        res = _classify(r)
        if res[0] != "done":
            return res
        d = r.json()
        if d.get("already_uploaded"):
            return ("done",)
        put = await c.put(d["upload_url"], content=data, headers=d.get("headers") or {"Content-Type": content_type},
                          timeout=180)
        if put.status_code >= 400:
            return ("retry", f"Upload refused (HTTP {put.status_code}).")
        r = await c.post(f"{base}/complete", json={"media_type": media_type}, headers=headers)
        return _classify(r) if r.status_code != 422 else ("retry", _err(r, "Upload check failed."))

    def _payload(self, rid: str) -> dict | None:
        with self.db.session() as s:
            i = s.get(Incident, rid)
            if i is None or i.deleted_at is not None:
                return None
            cam = s.get(Camera, i.camera_id)
            meta = json.loads(i.event_metadata_json or "{}")
            timeline = [{"event": str(e.get("event"))[:60], "at": (str(e["at"])[:40] if e.get("at") else None)}
                        for e in meta.get("timeline", []) if e.get("event")][-50:]
            return {
                "local_incident_id": i.id, "ref": i.ref, "incident_type": i.incident_type, "severity": i.severity,
                "confidence": i.confidence, "camera_id": i.camera_id, "camera_name": (cam.name if cam else None),
                "title": i.title,
                # Staff reports' own words; AI incidents have no free text worth sending.
                "description": (i.description or "")[:1000] or None,
                "occurred_at": iso_utc(i.occurred_at), "ended_at": iso_utc(i.ended_at), "status": i.status,
                "acknowledged_by": i.acknowledged_by, "acknowledged_at": iso_utc(i.acknowledged_at),
                "reviewed_by": i.reviewed_by, "reviewed_at": iso_utc(i.reviewed_at),
                "false_alert_reason": i.false_alert_reason, "keep_evidence": bool(i.keep_evidence),
                "timeline": timeline, "clip_duration_seconds": i.clip_duration_seconds,
                "snapshot_expected": bool(i.snapshot_path) or i.media_status == "PENDING",
                "clip_expected": i.severity in CLIP_SEVERITIES and (bool(i.clip_path) or i.media_status in ("PENDING", "PARTIAL")),
            }

    # ── bookkeeping ──────────────────────────────────────────────────
    def _cloud_id(self, rid: str) -> str | None:
        with self.db.session() as s:
            return s.scalar(select(SyncJob.cloud_ref).where(SyncJob.operation_type == UPSERT, SyncJob.resource_id == rid))

    def _claim(self, jid: str) -> bool:
        with self.db.session() as s:
            j = s.get(SyncJob, jid)
            if j is None or j.status not in ("PENDING", "RETRY"):
                return False
            j.status, j.attempts = "PROCESSING", j.attempts + 1
            return True

    def _complete(self, jid: str, cloud_ref: str | None) -> int:
        with self.db.session() as s:
            j = s.get(SyncJob, jid)
            if cloud_ref:
                j.cloud_ref = cloud_ref
            if j.status != "PROCESSING":
                return 0  # re-queued while in flight (incident changed): it goes again
            j.status, j.last_error = "COMPLETED", None
            return 1

    def _retry(self, jid: str, err: str, now: datetime, delay: float | None = None, media: bool = False) -> None:
        with self.db.session() as s:
            j = s.get(SyncJob, jid)
            if j.status != "PROCESSING":
                return
            if media and j.attempts >= MEDIA_MAX_ATTEMPTS:
                j.status, j.last_error = "FAILED", err
                return
            j.status, j.last_error = "RETRY", err
            j.next_attempt_at = now + timedelta(seconds=delay if delay is not None
                                                else BACKOFF[min(j.attempts - 1, len(BACKOFF) - 1)])

    def _fail(self, jid: str, err: str) -> None:
        with self.db.session() as s:
            j = s.get(SyncJob, jid)
            j.status, j.last_error = "FAILED", err
        log.warning("sync job %s failed: %s", jid, err)


def _err(r: httpx.Response, fallback: str) -> str:
    try:
        return r.json().get("error") or fallback
    except ValueError:
        return fallback


def _classify(r: httpx.Response) -> tuple:
    if r.status_code in (200, 201):
        return ("done",)
    if r.status_code == 401:
        return ("auth",)
    if r.status_code == 404:
        return ("retry", "Boombiz isn't ready for incident sync yet.", NOT_READY_RETRY_S)
    if r.status_code in (408, 409, 425, 429) or r.status_code >= 500:
        return ("retry", _err(r, f"HTTP {r.status_code}"))
    return ("failed", _err(r, f"HTTP {r.status_code}"))


def sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()
