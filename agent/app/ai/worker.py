"""One AI worker per Guard camera (Phase 2 §38–41, §55–56).

    stream worker JPEG ─► bounded queue (2) ─► scheduler gate (target FPS)
        ─► decode ─► privacy mask ─► person detector (SHARED model)
        ─► ignore-zone filter ─► tracker ─► zones ─► shelf ─► correlator
        ─► events            (+ fire/smoke at ~2 FPS, separately gated)

The queue keeps only the newest frames: if AI falls behind, old frames are
DROPPED, never backlogged (§40). Each stage past the detector is isolated:
if the shelf or fire module throws, it is switched off for this camera and
person/exit/restricted protection carries on (§55). A frame that fails to
decode is skipped, never analysed (§56).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np

from ..events.correlator import Correlator
from ..events.service import EventService
from ..fire.detector import HeuristicFireModel
from ..fire.validator import FireValidator
from ..interactions.concealment import ConcealmentEngine
from ..interactions.shelf import ShelfInteractionEngine
from .pose import PoseEstimator
from ..performance.monitor import ResourceMonitor
from ..zones.engine import OCCUPIABLE, ZoneDef, ZoneEngine, ZoneType
from ..zones.schedule import DayHours, is_open
from .detector import PersonDetector
from .tracker import ByteTracker, TrackState

log = logging.getLogger(__name__)

FIRE_INTERVAL_S = 0.5  # §34: 1–3 FPS


@dataclass
class WorkerStats:
    frames_in: int = 0
    frames_dropped: int = 0
    frames_corrupt: int = 0
    inferences: int = 0
    infer_ms: float = 0.0
    ai_fps: float = 0.0
    lag_ms: float = 0.0
    last_inference_at: float | None = None
    _times: deque = field(default_factory=lambda: deque(maxlen=20))


class CameraAIWorker:
    def __init__(self, camera_id: str, name: str, *, detector: PersonDetector, events: EventService,
                 monitor: ResourceMonitor, versions: dict[str, str], features: dict[str, bool], priority: str,
                 zones: list[ZoneDef], schedule: list[DayHours]) -> None:
        self.camera_id = camera_id
        self.name = name
        self.detector = detector
        self.events = events
        self.monitor = monitor
        self.versions = versions
        self.features_cfg = features
        self.priority = priority
        self.schedule = schedule
        self.tracker = ByteTracker()
        self.zones = ZoneEngine()
        self.shelf = ShelfInteractionEngine()
        self.correlator = Correlator(camera_id)
        self.concealment = ConcealmentEngine()
        self.pose: PoseEstimator | None = None  # set by the service when a pose model is loaded
        self.fire_model = HeuristicFireModel()
        self.fire_validator = FireValidator()
        self.failed_modules: set[str] = set()
        self.set_zones(zones)
        self.queue: deque[tuple[bytes, float]] = deque(maxlen=2)
        self._wake = asyncio.Event()
        self.state = "WAITING_FOR_STREAM"
        self.stats = WorkerStats()
        self._task: asyncio.Task | None = None
        self._last_fire = 0.0
        self.snapshot: dict = {"tracks": [], "at": None}
        self.stream_ok = lambda: True  # replaced by the service

    # ── wiring ──────────────────────────────────────────────────────
    def set_zones(self, zones: list[ZoneDef]) -> None:
        self.zones.set_zones(zones)
        self.shelf.set_zones(self.zones.of_type(ZoneType.SHELF))

    def on_frame(self, jpeg: bytes, ts: float) -> None:
        """Called from the stream worker for every decoded frame (event loop thread)."""
        self.stats.frames_in += 1
        if len(self.queue) == self.queue.maxlen:
            self.stats.frames_dropped += 1
        self.queue.append((jpeg, ts))
        self._wake.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"ai:{self.camera_id}")
        log.info("camera_worker_started camera=%s", self.camera_id)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.state = "STOPPED"
        log.info("camera_worker_stopped camera=%s", self.camera_id)

    def features(self) -> set[str]:
        on = {k for k, v in self.features_cfg.items() if v}
        return on - set(self.monitor.plan.disabled_features) - self.failed_modules

    def target_fps(self) -> float:
        return self.monitor.plan.fps_for(self.priority)

    # ── loop ────────────────────────────────────────────────────────
    async def _run(self) -> None:
        last = 0.0
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not self.stream_ok() or not self.queue:
                self.state = "WAITING_FOR_STREAM"
                continue
            self.state = "RUNNING"
            now = time.monotonic()
            interval = 1.0 / max(0.5, self.target_fps())
            if now - last < interval:
                await asyncio.sleep(interval - (now - last))
            jpeg, ts = self.queue.pop()        # newest frame
            self.stats.frames_dropped += len(self.queue)
            self.queue.clear()                 # everything older is stale (§40)
            last = time.monotonic()
            try:
                drafts = await asyncio.to_thread(self._process, jpeg, ts)
            except Exception:
                log.exception("model_error camera=%s", self.camera_id)
                await asyncio.sleep(1)
                continue
            for d in drafts:
                self.events.emit(d, self.versions)

    # ── one frame (runs in a worker thread) ─────────────────────────
    def _process(self, jpeg: bytes, ts: float) -> list:
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            self.stats.frames_corrupt += 1
            return []
        now = ts
        frame = self.zones.apply_privacy_mask(frame)
        feats = self.features()

        t0 = time.perf_counter()
        dets = [d for d in self.detector.detect(frame) if not self.zones.suppressed(d.bbox)]
        self.stats.infer_ms = (time.perf_counter() - t0) * 1000
        live, _created, ended = self.tracker.update(dets, now)
        visible = {t.track_id: t.bbox for t in live if t.last_seen == now}
        transitions = self.zones.evaluate(visible, [t.track_id for t in ended])

        shelf_events = []
        if "shelf" in feats:
            try:
                speeds = {t.track_id: (t.vx * t.vx + t.vy * t.vy) ** 0.5 for t in live if t.last_seen == now}
                shelf_events = self.shelf.update(frame, visible, now, speeds)
            except Exception:
                self._disable("shelf")

        concealments: list[tuple[str, str | None, str]] = []
        if "concealment" in feats and self.pose is not None:
            try:
                track_speeds = {t.track_id: (t.vx * t.vx + t.vy * t.vy) ** 0.5 for t in live if t.last_seen == now}
                concealments = self._concealment(frame, visible, shelf_events, now, track_speeds)
            except Exception:
                self._disable("concealment")

        has_occupiable = any(z.zone_type in OCCUPIABLE for z in self.zones.zones)
        active = {t.track_id: t.first_seen for t in live if t.state == TrackState.ACTIVE
                  and (not has_occupiable or self.zones.current(t.track_id))}
        drafts = self.correlator.process(
            now=now, active_tracks=active,
            promoted=[t.track_id for t in live if t.state == TrackState.ACTIVE],
            ended=[t.track_id for t in ended], transitions=transitions, shelf_events=shelf_events,
            store_open=is_open(self.schedule, datetime.now()), features=feats,
            zone_of_track={tid: self.zones.current(tid) for tid in visible},
            concealments=concealments,
        )
        for t in ended:
            self.shelf.forget_track(t.track_id, 0, now)
            self.concealment.forget(t.track_id)

        if "fire" in feats and now - self._last_fire >= FIRE_INTERVAL_S:
            self._last_fire = now
            try:
                drafts += self._fire(frame, now)
            except Exception:
                self._disable("fire")

        # Phase 3: every event carries the capture time of the frame that
        # produced it (same monotonic clock as the rolling buffer), so an
        # incident's "5 s before" is anchored to what the camera saw, not to
        # when the AI got round to it.
        for d in drafts:
            d.metadata.setdefault("frame_ts", ts)

        s = self.stats
        s.inferences += 1
        s._times.append(now)
        if len(s._times) > 1:
            s.ai_fps = round((len(s._times) - 1) / max(1e-6, s._times[-1] - s._times[0]), 1)
        s.lag_ms = round((time.monotonic() - ts) * 1000, 1)
        s.last_inference_at = time.time()
        self.snapshot = {
            "at": time.time(),
            "tracks": [{**t.to_dict(self.camera_id), "zones": sorted(self.zones.current(t.track_id))} for t in live],
        }
        return drafts

    def _concealment(self, frame: np.ndarray, visible: dict, shelf_events: list, now: float,
                     speeds: dict[str, float] | None = None) -> list:
        """Pose only on people who just touched a shelf (bounded cost)."""
        speeds = speeds or {}
        for ev in shelf_events:
            if ev.kind == "SHELF_INTERACTION":
                self.concealment.on_shelf_interaction(ev.interaction.track_id, ev.interaction.shelf_zone_id, now)
        out = []
        for tid, box in visible.items():
            if not self.concealment.wants_pose(tid, now):
                continue
            st = self.concealment.tracks[tid]
            pose = self.pose.estimate(frame, box)
            region = self.concealment.update(tid, box, pose, self.zones.by_id(st.shelf_zone_id or ""), now,
                                             speeds.get(tid, 0.0))
            if region:
                out.append((tid, st.shelf_zone_id, region))
        return out

    def _fire(self, frame: np.ndarray, now: float) -> list:
        from ..events.service import EventDraft

        risk = self.zones.of_type(ZoneType.FIRE_RISK)
        mask = None
        if risk:
            h, w = frame.shape[:2]
            mask = np.zeros((h, w), np.uint8)
            for z in risk:
                cv2.fillPoly(mask, [np.array([[int(x * w), int(y * h)] for x, y in z.polygon], np.int32)], 255)
        obs = self.fire_model.observe(frame, mask)
        out = []
        for kind in self.fire_validator.feed(obs, now, risk_zone=bool(risk)):
            out.append(EventDraft(
                self.camera_id, kind, None, risk[0].id if risk else None,
                "CRITICAL" if kind == "POSSIBLE_FIRE" else "HIGH", "LOW",
                metadata={"flame_score": obs.flame_score, "smoke_score": obs.smoke_score,
                          "notice": "Visual AI warning only. Not a replacement for certified fire detection systems."},
                dedup_ttl=120.0,
            ))
        return out

    def _disable(self, module: str) -> None:
        self.failed_modules.add(module)
        log.exception("model_error module=%s camera=%s — module disabled, person/exit monitoring continues",
                      module, self.camera_id)

    def status(self) -> dict:
        s = self.stats
        return {
            "camera_id": self.camera_id, "name": self.name, "state": self.state, "priority": self.priority,
            "target_fps": self.target_fps(), "ai_fps": s.ai_fps, "infer_ms": round(s.infer_ms, 1), "lag_ms": s.lag_ms,
            "frames_in": s.frames_in, "frames_dropped": s.frames_dropped, "frames_corrupt": s.frames_corrupt,
            "inferences": s.inferences, "active_tracks": len(self.snapshot["tracks"]),
            "features": sorted(self.features()), "failed_modules": sorted(self.failed_modules),
        }
