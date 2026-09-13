"""AI service — owns the ONE shared person model (§39), the resource
monitor, and a CameraAIWorker per Guard camera.

A sync loop every few seconds attaches workers to Guard cameras and detaches
them from cameras that were turned off — and re-subscribes when a stream
worker was replaced (credentials changed). The AI layer never sees a camera
URL or password: it only receives decoded frames from the stream workers
(§62 "no camera credentials passed to AI module").
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from ..config import Settings
from ..database.db import Database
from ..database.models import Camera, CameraAiConfig, Schedule, Zone
from ..events.service import EventService
from ..performance.adaptive_policy import AdaptivePolicy
from ..performance.monitor import ResourceMonitor
from ..services.streams import StreamManager
from ..zones.engine import ZoneDef, ZoneType
from ..zones.schedule import DayHours
from .backends.onnx import ONNXCPUBackend
from .detector import PersonDetector
from .pose import PoseEstimator
from .model_manager import ModelIntegrityError, ModelManager
from .worker import CameraAIWorker

log = logging.getLogger(__name__)

FEATURE_KEYS = ("person", "shelf", "exit", "restricted", "after_hours", "fire", "concealment")


class AIService:
    def __init__(self, db: Database, streams: StreamManager, events: EventService, settings: Settings) -> None:
        self.db = db
        self.streams = streams
        self.events = events
        self.settings = settings
        self.models = ModelManager(settings.models_dir)
        self.monitor = ResourceMonitor(AdaptivePolicy(max_fps=settings.ai_max_fps))
        self.backend = ONNXCPUBackend()
        self.detector: PersonDetector | None = None
        # Pose (concealment only). Its own small session: 1–2 threads, because
        # it only runs on people who just touched a shelf.
        self.pose: PoseEstimator | None = None
        self.pose_error: str | None = None
        self.versions: dict[str, str] = {}
        self.workers: dict[str, CameraAIWorker] = {}
        self._subscribed: dict[str, object] = {}
        self.running = False
        self.error: str | None = None
        self._sync_task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────
    def _load_model(self) -> bool:
        if self.detector:
            return True
        try:
            path, spec = self.models.verified_path(self.settings.person_model)
            self.backend.load_model(str(path))
            self.detector = PersonDetector(self.backend, threshold=self.settings.person_threshold)
            self.versions = self.models.versions(self.settings.person_model)
            log.info("inference_started device=%s model=%s", self.backend.get_device_name(), spec.version)
            self.error = None
            self._load_pose()
            return True
        except (ModelIntegrityError, Exception) as e:  # never crash the agent over AI
            self.error = str(e) if isinstance(e, ModelIntegrityError) else "The AI model could not be loaded."
            log.error("model_error %s", e)
            return False

    def _load_pose(self) -> None:
        """Optional: without it, concealment is 'unavailable' and nothing else changes."""
        try:
            path, spec = self.models.verified_path(self.settings.pose_model)
            backend = ONNXCPUBackend(threads=2)
            backend.load_model(str(path))
            self.pose = PoseEstimator(backend)
            self.versions["pose_model"] = spec.version
            self.pose_error = None
        except (ModelIntegrityError, Exception) as e:
            self.pose = None
            self.pose_error = str(e) if isinstance(e, ModelIntegrityError) else "The pose model could not be loaded."
            log.warning("model_error pose: %s — concealment unavailable, other protection unaffected", e)

    async def start(self) -> dict:
        if self.running:
            return self.status()
        ok = await asyncio.to_thread(self._load_model)
        if not ok:
            return self.status()
        self.monitor.start()
        self.running = True
        await self.sync()
        self._sync_task = asyncio.create_task(self._sync_loop(), name="ai-sync")
        self.db.audit("ai_started", None)
        return self.status()

    async def stop(self) -> dict:
        self.running = False
        if self._sync_task:
            self._sync_task.cancel()
        await self.monitor.stop()
        for cid in list(self.workers):
            await self._detach(cid)
        self.db.audit("ai_stopped", None)
        return self.status()

    async def _sync_loop(self) -> None:
        while self.running:
            await asyncio.sleep(3)
            try:
                await self.sync()
            except Exception:
                log.exception("ai sync failed")

    # ── camera ↔ worker ─────────────────────────────────────────────
    async def sync(self) -> None:
        with self.db.session() as s:
            cams = {c.id: c.name or c.id for c in s.scalars(select(Camera).where(Camera.guard_enabled.is_(True)))}
        for cid in list(self.workers):
            if cid not in cams:
                await self._detach(cid)
        for cid, name in cams.items():
            stream = self.streams.guard.get(cid)
            if stream is None:
                continue
            if cid not in self.workers:
                w = CameraAIWorker(cid, name, detector=self.detector, events=self.events, monitor=self.monitor,
                                   versions=self.versions, features=self._features(cid), priority=self._priority(cid),
                                   zones=self._zones(cid), schedule=self._schedule())
                w.pose = self.pose
                self.workers[cid] = w
                w.start()
            w = self.workers[cid]
            if self._subscribed.get(cid) is not stream:
                old = self._subscribed.get(cid)
                if old is not None and w.on_frame in getattr(old, "subscribers", []):
                    old.subscribers.remove(w.on_frame)
                stream.subscribers.append(w.on_frame)
                self._subscribed[cid] = stream
            w.stream_ok = lambda st=stream: st.health.status.value in ("ONLINE", "DEGRADED")

    async def _detach(self, cid: str) -> None:
        w = self.workers.pop(cid, None)
        stream = self._subscribed.pop(cid, None)
        if w and stream is not None and w.on_frame in stream.subscribers:
            stream.subscribers.remove(w.on_frame)
        if w:
            await w.stop()

    # ── config readers ──────────────────────────────────────────────
    def _cfg_row(self, s, cid: str) -> CameraAiConfig:  # noqa: ANN001
        row = s.get(CameraAiConfig, cid)
        if row is None:
            row = CameraAiConfig(camera_id=cid)
            s.add(row)
            s.flush()
        return row

    def _features(self, cid: str) -> dict[str, bool]:
        with self.db.session() as s:
            row = self._cfg_row(s, cid)
            return {k: bool(getattr(row, k)) for k in FEATURE_KEYS}

    def _priority(self, cid: str) -> str:
        with self.db.session() as s:
            return self._cfg_row(s, cid).priority

    def _zones(self, cid: str) -> list[ZoneDef]:
        with self.db.session() as s:
            rows = s.scalars(select(Zone).where(Zone.camera_id == cid, Zone.enabled.is_(True))).all()
            return [ZoneDef(z.id, z.name, ZoneType(z.zone_type), [(p[0], p[1]) for p in json.loads(z.polygon_json)],
                            z.sensitivity, z.enabled) for z in rows]

    def _schedule(self) -> list[DayHours]:
        with self.db.session() as s:
            return [DayHours(r.day_of_week, r.opens_at, r.closes_at, r.closed) for r in s.scalars(select(Schedule))]

    def reload_camera(self, cid: str) -> None:
        w = self.workers.get(cid)
        if w:
            w.set_zones(self._zones(cid))
            w.features_cfg = self._features(cid)
            w.priority = self._priority(cid)

    def reload_schedule(self) -> None:
        sch = self._schedule()
        for w in self.workers.values():
            w.schedule = sch

    # ── read models ─────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "running": self.running, "error": self.error,
            "concealment_available": self.pose is not None, "concealment_error": self.pose_error,
            "device": self.backend.get_device_name() if self.detector else None,
            "models": self.versions, "performance": self.monitor.plan.to_dict(),
            "cameras": [w.status() for w in self.workers.values()],
        }

    def active_tracks(self) -> list[dict]:
        return [t for w in self.workers.values() for t in w.snapshot["tracks"] if t["state"] != "ENDED"]

    def debug(self, cid: str) -> dict | None:
        w = self.workers.get(cid)
        if not w:
            return None
        return {
            "camera_id": cid, "tracks": w.snapshot["tracks"], "at": w.snapshot["at"],
            "zones": [{"id": z.id, "name": z.name, "zone_type": z.zone_type.value,
                       "polygon": [{"x": x, "y": y} for x, y in z.polygon]} for z in w.zones.zones],
            "status": w.status(),
        }
