"""The incident engine (Phase 3 §5–14, §25, §32–40, §47, §55–56).

    AI event ──► classifier ──► correlation key (camera : person : family)
         │            │               │
         │            │        open incident inside its window? ── yes ─► merge into it
         │            │               │ no
         │            │               ▼
         │            │        create incident  (ref BG-LOC-YYYYMMDD-000001)
         │            │               ├─ freeze 5 s pre-event + collect 10 s post (RAM)
         │            │               ├─ queue SNAPSHOT (+1.5 s) and CLIP (+10 s)
         │            │               ├─ encrypted metadata.json
         │            │               └─ alarm rules
         └─ timeline events (PERSON_DETECTED, SHELF_INTERACTION, …) are remembered
            per person for 5 minutes and become the incident's "why" (§55).

The AI never decides theft: incidents start UNREVIEWED and only a signed-in
person moves them on (§37).

Camera health (§55–56 of the PRD's Phase 1/2 and §7 here) is watched on a
loop: a Guard camera with no video for 30 s (the stream worker calls it
offline after 15 s, this loop confirms for 15 s more) opens ONE HIGH
CAMERA_OFFLINE incident — it may have been smashed, cut or unplugged — that
ends when it reconnects; all Guard cameras offline opens ONE
GUARD_PROTECTION_DEGRADED (CRITICAL).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
import time
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from ..alarms.service import AlarmService
from ..buffer.segment_manager import BufferManager
from ..database.db import Database
from ..database.models import Camera, Incident, Setting, iso_utc
from ..media.worker import MediaWorker
from ..retention.service import RetentionService
from ..review.auth import Session
from ..review.permissions import Forbidden, require
from . import lifecycle
from .classifier import MANUAL_TYPES, RULES, SEVERITY_RANK, TIMELINE_EVENTS, IncidentRule, rule_for

log = logging.getLogger(__name__)

CONF_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
# The stream worker marks a camera OFFLINE after 15 s without video; 15 s more
# here makes 30 s from the last picture (owner decision 2026-09-14) — long
# enough that a Wi-Fi or recorder blip doesn't raise a damaged-camera alarm.
OFFLINE_OPEN_S = 15.0
HEALTH_CHECK_S = 5.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IncidentError(Exception):
    pass


@dataclass
class _Open:
    incident_id: str
    last_mono: float
    window: float


class IncidentService:
    def __init__(self, db: Database, buffers: BufferManager, media: MediaWorker, alarms: AlarmService,
                 retention: RetentionService, *, streams=None, pre_s: float = 5.0, post_s: float = 10.0) -> None:  # noqa: ANN001
        self.db = db
        self.buffers = buffers
        self.media = media
        self.alarms = alarms
        self.retention = retention
        self.streams = streams
        self.pre_s, self.post_s = pre_s, post_s
        self._open: dict[str, _Open] = {}
        self._recent: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=60))
        self._lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._offline_since: dict[str, float] = {}
        self._offline_incident: dict[str, str] = {}
        self._degraded_incident: str | None = None
        self._task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._health_loop(), name="incident-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ── ids ──────────────────────────────────────────────────────────
    def location_code(self) -> str:
        with self.db.session() as s:
            row = s.get(Setting, "location_code")
            return row.value if row else "LOC"

    def set_location_code(self, code: str) -> str:
        code = (code or "").strip().upper()
        if not (len(code) == 3 and code.isalpha()):
            raise IncidentError("The location code is 3 letters, e.g. LAG, ABJ or PHC.")
        with self.db.session() as s:
            row = s.get(Setting, "location_code")
            if row:
                row.value = code
            else:
                s.add(Setting(key="location_code", value=code))
        self.db.audit("configuration_changed", None, field="location_code", value=code)
        return code

    def _next_ref(self, s, when: datetime) -> str:  # noqa: ANN001
        day = when.astimezone().strftime("%Y%m%d")
        key = f"incident_seq:{day}"
        row = s.get(Setting, key)
        seq = int(row.value) + 1 if row else 1
        if row:
            row.value = str(seq)
        else:
            s.add(Setting(key=key, value=str(seq)))
        loc = s.get(Setting, "location_code")
        return f"BG-{loc.value if loc else 'LOC'}-{day}-{seq:06d}"

    # ── events in ────────────────────────────────────────────────────
    def on_ai_event(self, evt: dict) -> None:
        """Called by EventService.emit — possibly from the AI worker thread."""
        try:
            self._handle(evt)
        except Exception:
            log.exception("incident engine failed on %s", evt.get("event_type"))

    def _handle(self, evt: dict) -> None:
        etype, cam, track = evt["event_type"], evt["camera_id"], evt.get("track_id")
        entry = {"event": etype, "at": evt.get("occurred_at"), "zone_id": evt.get("zone_id"),
                 "confidence": evt.get("confidence")}
        if etype in TIMELINE_EVENTS and track:
            self._recent[(cam, track)].append(entry)
        rule = rule_for(etype)
        with self._lock:
            # Context events join an open incident for the same person.
            if not rule or not rule.creates_incident:
                if track:
                    for key, op in self._open.items():
                        if key.startswith(f"{cam}:{track}:") and time.monotonic() - op.last_mono <= op.window:
                            self._append_timeline(op.incident_id, entry, bump_conf=etype == "POSSIBLE_CONCEALMENT")
                return
            key = f"{cam}:{track if (rule.per_track and track) else '*'}:{rule.family}"
            op = self._open.get(key)
            now_m = time.monotonic()
            if op and now_m - op.last_mono <= op.window:
                op.last_mono = now_m
                self._merge(op.incident_id, rule, evt, entry)
                return
            iid = self._create(rule, evt, key)
            self._open[key] = _Open(iid, now_m, rule.window_s)
        self._after_create(iid, rule.severity)

    def _timeline_for(self, cam: str, track: str | None) -> list[dict]:
        if not track:
            return []
        cutoff = _now() - timedelta(minutes=5)
        out = []
        for e in self._recent.get((cam, track), []):
            try:
                if datetime.fromisoformat(e["at"]) >= cutoff:
                    out.append(e)
            except (TypeError, ValueError):
                out.append(e)
        return out

    def _create(self, rule: IncidentRule, evt: dict, key: str, *, manual: dict | None = None) -> str:
        occurred = datetime.fromisoformat(evt["occurred_at"]) if evt.get("occurred_at") else _now()
        cam, track = evt["camera_id"], evt.get("track_id")
        trigger = {"event": evt["event_type"], "at": evt.get("occurred_at"), "zone_id": evt.get("zone_id"),
                   "confidence": evt.get("confidence")}
        timeline = self._timeline_for(cam, track) + [trigger]
        with self.db.session() as s:
            ref = self._next_ref(s, occurred)
            cam_row = s.get(Camera, cam)
            inc = Incident(
                ref=ref, camera_id=cam, track_id=track, incident_type=rule.incident_type,
                severity=rule.severity, confidence=evt.get("confidence"), status=lifecycle.UNREVIEWED,
                zone_id=evt.get("zone_id"), title=rule.title, correlation_key=key,
                description=(manual or {}).get("description") or (evt.get("metadata") or {}).get("note"),
                occurred_at=occurred, started_at=occurred, last_event_at=occurred,
                location_id=self.location_code(),
                model_metadata_json=json.dumps((evt.get("metadata") or {}).get("models") or {}),
                event_metadata_json=json.dumps({"trigger": evt, "timeline": timeline, "manual": manual}, default=str),
                media_status="PENDING",
            )
            s.add(inc)
            s.flush()
            iid, cam_name = inc.id, (cam_row.name if cam_row else cam)
        self.db.audit("incident_created", ref, type=rule.incident_type, severity=rule.severity, camera=cam)
        log.info("incident_created %s %s camera=%s track=%s", ref, rule.incident_type, cam, track)
        # Encrypted metadata.json beside the media (§22).
        try:
            self.media.write_metadata(ref, occurred, {
                "incident_id": ref, "incident_type": rule.incident_type, "camera": cam_name, "track_id": track,
                "severity": rule.severity, "confidence": evt.get("confidence"), "trigger_event": evt["event_type"],
                "timeline": [{"event": e["event"], "timestamp": e["at"]} for e in timeline]})
        except Exception:
            log.exception("metadata write failed for %s", ref)
        # Media: freeze pre-event, collect post-event, queue jobs. Anchored to
        # the capture time of the triggering frame when the AI supplied it.
        frame_ts = (evt.get("metadata") or {}).get("frame_ts")
        self._start_media(iid, cam, rule.severity, rule.family == "fire",
                          trigger_ts=float(frame_ts) if frame_ts is not None else None,
                          pre=(manual or {}).get("pre_s", self.pre_s), post=(manual or {}).get("post_s", self.post_s),
                          wanted=(manual or {}).get("capture", True))
        return iid

    def _start_media(self, iid: str, cam: str, severity: str, fire: bool, pre: float, post: float, wanted: bool,
                     trigger_ts: float | None = None) -> None:
        buf = self.buffers.get(cam)
        if not wanted or buf is None or not self.retention.media_allowed(severity):
            with self.db.session() as s:
                inc = s.get(Incident, iid)
                inc.media_status = "SKIPPED" if wanted and buf is not None else "UNAVAILABLE"
            if wanted and buf is not None:
                self.db.audit("media_skipped_low_disk", iid)
            return
        cap = buf.start_capture(iid, trigger_ts if trigger_ts is not None else time.monotonic(), pre, post)
        self.media.captures[iid] = cap
        self.media.enqueue(iid, "SNAPSHOT", 2 if fire else 4, 1.5)
        self.media.enqueue(iid, "CLIP", 1 if fire else 5, cap.post_s + 1.0)

    def _after_create(self, iid: str, severity: str) -> None:
        if self.loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        coro = self.alarms.on_incident(iid)
        if running is self.loop:
            asyncio.ensure_future(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _merge(self, iid: str, rule: IncidentRule, evt: dict, entry: dict) -> None:
        with self.db.session() as s:
            inc = s.get(Incident, iid)
            if not inc:
                return
            meta = json.loads(inc.event_metadata_json or "{}")
            meta.setdefault("timeline", []).append(entry)
            meta["merged"] = meta.get("merged", 0) + 1
            inc.event_metadata_json = json.dumps(meta, default=str)
            inc.last_event_at = datetime.fromisoformat(evt["occurred_at"]) if evt.get("occurred_at") else _now()
            if SEVERITY_RANK[rule.severity] > SEVERITY_RANK.get(inc.severity, 0):
                inc.severity = rule.severity
            c = evt.get("confidence")
            if c and CONF_RANK.get(c, 0) > CONF_RANK.get(inc.confidence or "LOW", 0):
                inc.confidence = c

    def _append_timeline(self, iid: str, entry: dict, bump_conf: bool = False) -> None:
        with self.db.session() as s:
            inc = s.get(Incident, iid)
            if not inc:
                return
            meta = json.loads(inc.event_metadata_json or "{}")
            meta.setdefault("timeline", []).append(entry)
            inc.event_metadata_json = json.dumps(meta, default=str)
            if bump_conf and inc.confidence == "LOW":
                inc.confidence = "MEDIUM"  # concealment: one step, never to HIGH (§33)

    # ── camera health → incidents ───────────────────────────────────
    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_CHECK_S)
            try:
                self.check_camera_health()
            except Exception:
                log.exception("camera health check failed")

    def check_camera_health(self, now_m: float | None = None, statuses: dict[str, str] | None = None) -> None:
        now_m = now_m if now_m is not None else time.monotonic()
        if statuses is None:
            if self.streams is None:
                return
            statuses = {cid: w.health.status.value for cid, w in self.streams.guard.items()}
        if not statuses:
            return
        down = {cid for cid, st in statuses.items() if st not in ("ONLINE", "DEGRADED", "STARTING")}
        for cid in statuses:
            if cid in down:
                since = self._offline_since.setdefault(cid, now_m)
                gone = now_m - since
                if gone >= OFFLINE_OPEN_S and cid not in self._offline_incident:
                    iid = self._create(RULES["CAMERA_OFFLINE"], {"event_type": "CAMERA_OFFLINE", "camera_id": cid,
                                        "occurred_at": _now().isoformat()}, f"{cid}:*:health", manual={"capture": False})
                    with self.db.session() as s:
                        inc = s.get(Incident, iid)
                        if inc:
                            inc.description = ("The camera stopped sending video. It may be damaged, disconnected "
                                               "or without power. Check it now.")
                    self._offline_incident[cid] = iid
                    self._after_create(iid, "HIGH")
            else:
                self._offline_since.pop(cid, None)
                iid = self._offline_incident.pop(cid, None)
                if iid:
                    with self.db.session() as s:
                        inc = s.get(Incident, iid)
                        if inc:
                            inc.ended_at = _now()
                            inc.description = (inc.description or "") + " Camera reconnected."
        all_down = len(down) == len(statuses) and all(now_m - self._offline_since.get(c, now_m) >= OFFLINE_OPEN_S
                                                      for c in down)
        if all_down and self._degraded_incident is None:
            rule = IncidentRule("GUARD_PROTECTION_DEGRADED", "health", "CRITICAL", 1e9,
                                "All Guard cameras are offline", per_track=False)
            first = next(iter(statuses))
            self._degraded_incident = self._create(rule, {"event_type": "GUARD_PROTECTION_DEGRADED",
                                                          "camera_id": first, "occurred_at": _now().isoformat()},
                                                   "*:*:degraded", manual={"capture": False})
            self._after_create(self._degraded_incident, "CRITICAL")
        elif not down and self._degraded_incident:
            with self.db.session() as s:
                inc = s.get(Incident, self._degraded_incident)
                if inc:
                    inc.ended_at = _now()
            self._degraded_incident = None

    # ── manual (§32) ─────────────────────────────────────────────────
    def manual(self, session: Session, camera_id: str, description: str, severity: str,
               capture_snapshot: bool, save_last_15s: bool) -> dict:
        require(session.role, "manual_incident")
        if severity not in ("LOW", "HIGH", "CRITICAL"):
            raise IncidentError("Choose Low, High or Critical.")
        with self.db.session() as s:
            if not s.get(Camera, camera_id):
                raise IncidentError("Choose a camera.")
        rule = IncidentRule("MANUAL_SECURITY_INCIDENT", "manual", severity, 0, "Reported by staff", per_track=False)
        want = capture_snapshot or save_last_15s
        buf = self.buffers.get(camera_id)
        pre = min(10.0, buf.seconds) if buf else 0.0
        iid = self._create(rule, {"event_type": "MANUAL_SECURITY_INCIDENT", "camera_id": camera_id,
                                  "occurred_at": _now().isoformat(),
                                  "metadata": {"reported_by": session.name, "role": session.role}},
                           f"{camera_id}:*:manual:{time.monotonic()}",
                           manual={"description": description.strip()[:1000], "capture": want,
                                   "pre_s": pre if save_last_15s else 1.0, "post_s": (15.0 - pre) if save_last_15s else 1.0,
                                   "reported_by": session.name})
        self.db.audit("incident_created", iid, manual=True, by=session.name)
        self._after_create(iid, severity)
        return self.get(iid)

    # ── review (§33–40) ──────────────────────────────────────────────
    def act(self, incident_id: str, action: str, session: Session, note: str | None = None,
            reason: str | None = None) -> dict:
        with self.db.session() as s:
            inc = self._get_row(s, incident_id)
            require(session.role, action, inc.severity)
            target = lifecycle.check(inc.status, action)
            if action == "false_alert":
                if reason not in lifecycle.FALSE_ALERT_REASONS:
                    raise IncidentError("Choose why this was a false alert.")
                inc.false_alert_reason = reason
            now = _now()
            if action == "acknowledge":
                inc.acknowledged_by, inc.acknowledged_at = inc.acknowledged_by or session.name, inc.acknowledged_at or now
            else:
                inc.reviewed_by, inc.reviewed_at = session.name, now
                if not inc.acknowledged_at:
                    inc.acknowledged_by, inc.acknowledged_at = session.name, now
            if note:
                inc.resolution_note = note.strip()[:1000]
            inc.status = target
            ref = inc.ref
        self.alarms.stop_repeat(incident_id)  # any response ends a repeating fire siren
        self.db.audit(f"incident_{'false_alert' if action == 'false_alert' else action + ('d' if action.endswith('e') else 'ed')}",
                      ref, by=session.name, role=session.role, reason=reason)
        return self.get(incident_id)

    def remote_acknowledge(self, incident_id: str, by: str, at: str | None) -> bool:
        """Someone acknowledged this incident from their phone (Phase 4 §52–55).
        Only moves UNREVIEWED → ACKNOWLEDGED; a review already done here wins.
        Returns True when handled (including "nothing to do"), so the cloud
        stops re-sending the command."""
        when = None
        if at:
            try:
                when = datetime.fromisoformat(at)
            except ValueError:
                when = None
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if inc is None or inc.deleted_at is not None:
                return True
            if inc.status != lifecycle.UNREVIEWED:
                return True
            inc.status = lifecycle.ACKNOWLEDGED
            inc.acknowledged_by = f"{by[:60]} (remote)"
            inc.acknowledged_at = when or _now()
            ref = inc.ref
        self.alarms.stop_repeat(incident_id)  # a response from anywhere ends a repeating fire siren
        self.db.audit("incident_acknowledged", ref, by=by, remote=True)
        return True

    def keep(self, incident_id: str, session: Session, keep: bool) -> dict:
        require(session.role, "keep")
        with self.db.session() as s:
            inc = self._get_row(s, incident_id)
            inc.keep_evidence = keep
            ref = inc.ref
        self.db.audit("incident_kept" if keep else "incident_unkept", ref, by=session.name)
        return self.get(incident_id)

    def delete(self, incident_id: str, session: Session) -> None:
        require(session.role, "delete")
        with self.db.session() as s:
            inc = self._get_row(s, incident_id)
            self.retention.soft_delete(inc, "manual")
            inc.reviewed_by = session.name

    # ── read ─────────────────────────────────────────────────────────
    def _get_row(self, s, incident_id: str) -> Incident:  # noqa: ANN001
        inc = s.get(Incident, incident_id)
        if inc is None or inc.deleted_at is not None:
            raise IncidentError("That incident no longer exists.")
        return inc

    def _dict(self, i: Incident, cam_names: dict[str, str], full: bool = False) -> dict:
        meta = json.loads(i.event_metadata_json or "{}")
        d = {
            "id": i.id, "ref": i.ref, "incident_type": i.incident_type, "title": i.title,
            "severity": i.severity, "confidence": i.confidence, "status": i.status,
            "camera_id": i.camera_id, "camera": cam_names.get(i.camera_id, i.camera_id), "track_id": i.track_id,
            "zone_id": i.zone_id, "occurred_at": iso_utc(i.occurred_at),
            "ended_at": iso_utc(i.ended_at),
            "has_snapshot": bool(i.snapshot_path), "has_clip": bool(i.clip_path),
            "clip_duration_seconds": i.clip_duration_seconds, "media_status": i.media_status,
            "keep_evidence": i.keep_evidence, "alarm_state": i.alarm_state,
            "acknowledged_by": i.acknowledged_by, "reviewed_by": i.reviewed_by,
            "reviewed_at": iso_utc(i.reviewed_at),
            "false_alert_reason": i.false_alert_reason, "resolution_note": i.resolution_note,
            "description": i.description, "merged_events": meta.get("merged", 0),
        }
        if full:
            d["timeline"] = meta.get("timeline", [])
            d["models"] = json.loads(i.model_metadata_json or "{}")
            d["acknowledged_at"] = iso_utc(i.acknowledged_at)
        return d

    def _cam_names(self, s) -> dict[str, str]:  # noqa: ANN001
        return {c.id: c.name or c.id for c in s.scalars(select(Camera))}

    def get(self, incident_id: str) -> dict:
        with self.db.session() as s:
            return self._dict(self._get_row(s, incident_id), self._cam_names(s), full=True)

    def list(self, *, since: datetime | None = None, until: datetime | None = None, severity: str | None = None,
             incident_type: str | None = None, camera_id: str | None = None, status: str | None = None,
             kept: bool | None = None, search: str | None = None, limit: int = 200) -> list[dict]:
        with self.db.session() as s:
            q = select(Incident).where(Incident.deleted_at.is_(None)).order_by(Incident.occurred_at.desc())
            if since:
                q = q.where(Incident.occurred_at >= since)
            if until:
                q = q.where(Incident.occurred_at < until)
            if severity:
                q = q.where(Incident.severity == severity)
            if incident_type:
                q = q.where(Incident.incident_type == incident_type)
            if camera_id:
                q = q.where(Incident.camera_id == camera_id)
            if status:
                q = q.where(Incident.status == status)
            if kept is not None:
                q = q.where(Incident.keep_evidence.is_(kept))
            names = self._cam_names(s)
            if search:
                term = f"%{search.strip()}%"
                cams = [cid for cid, n in names.items() if search.strip().lower() in n.lower()]
                q = q.where(or_(Incident.ref.ilike(term), Incident.incident_type.ilike(term),
                                Incident.status.ilike(term), Incident.reviewed_by.ilike(term),
                                Incident.acknowledged_by.ilike(term), Incident.camera_id.in_(cams)))
            return [self._dict(i, names) for i in s.scalars(q.limit(min(max(limit, 1), 1000)))]

    def overview(self) -> dict:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.list(since=start, limit=1000)
        return {
            "today": {"incidents": len(today),
                      "high": sum(1 for i in today if i["severity"] == "HIGH"),
                      "critical": sum(1 for i in today if i["severity"] == "CRITICAL"),
                      "confirmed": sum(1 for i in today if i["status"] == "CONFIRMED"),
                      "false_alerts": sum(1 for i in today if i["status"] == "FALSE_ALERT"),
                      "unreviewed": sum(1 for i in today if i["status"] == "UNREVIEWED")},
            "latest": today[0] if today else (self.list(limit=1) or [None])[0],
        }

    # ── media out (§23, §71) ─────────────────────────────────────────
    # NB: not called `media` — that name is the MediaWorker attribute.
    def media_bytes(self, incident_id: str, kind: str, session: Session | None, role: str) -> tuple[bytes, str]:
        require(role, "view")
        with self.db.session() as s:
            inc = self._get_row(s, incident_id)
            rel = {"snapshot": inc.snapshot_path, "clip": inc.clip_path,
                   "thumbnail": inc.snapshot_path.replace("snapshot.jpg", "thumb.jpg") if inc.snapshot_path else None}[kind]
            ref = inc.ref
        data = self.media.read(rel, ref)
        if data is None:
            raise IncidentError("That video isn't available for this incident.")
        if kind == "clip":
            self.db.audit("clip_viewed", ref, by=session.name if session else role)
        return data, "video/mp4" if kind == "clip" else "image/jpeg"

    def export(self, incident_id: str, session: Session) -> tuple[bytes, str]:
        require(session.role, "export")
        d = self.get(incident_id)
        with self.db.session() as s:
            inc = self._get_row(s, incident_id)
            snap, clip, ref = inc.snapshot_path, inc.clip_path, inc.ref
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if (b := self.media.read(snap, ref)) is not None:
                z.writestr(f"{ref}/snapshot.jpg", b)
            if (b := self.media.read(clip, ref)) is not None:
                z.writestr(f"{ref}/clip.mp4", b)
            z.writestr(f"{ref}/incident.json", json.dumps(d, indent=2, default=str))
        self.db.audit("clip_exported", ref, by=session.name, role=session.role)
        return buf.getvalue(), f"{ref}.zip"
