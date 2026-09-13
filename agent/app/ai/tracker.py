"""Multi-object tracking — a compact ByteTrack (Phase 2 §8–9).

ByteTrack's idea, kept whole: match high-confidence detections first, then
give still-unmatched tracks a second chance against LOW-confidence
detections. That second pass is what carries a person through a partial
occlusion instead of minting a new ID. Motion is a constant-velocity
prediction on the box centre (instead of a full Kalman filter) — plenty at
5–10 FPS in a shop, and one less dependency to package.

Matching is greedy IoU. With the handful of people a shop camera sees,
greedy and Hungarian agree in practice and greedy needs no scipy.

A track ID means only "probably the same moving person in this camera
session". There is no appearance model and no face — nothing that could
recognise someone across sessions.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import StrEnum

from ..zones.geometry import BBox
from .detector import Detection


class TrackState(StrEnum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    TEMPORARILY_LOST = "TEMPORARILY_LOST"
    ENDED = "ENDED"


def iou(a: BBox, b: BBox) -> float:
    ix = max(0.0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Track:
    track_id: str
    bbox: BBox
    confidence: float
    first_seen: float
    last_seen: float
    state: TrackState = TrackState.NEW
    hits: int = 1
    vx: float = 0.0  # normalised units / second, box centre
    vy: float = 0.0
    history: list[tuple[float, float, float]] = field(default_factory=list)  # (t, foot_x, foot_y)

    def predict(self, now: float) -> BBox:
        dt = min(now - self.last_seen, 1.0)
        dx, dy = self.vx * dt, self.vy * dt
        b = self.bbox
        return BBox(b.x1 + dx, b.y1 + dy, b.x2 + dx, b.y2 + dy)

    def update(self, det: Detection, now: float) -> None:
        dt = now - self.last_seen
        if dt > 0:
            ocx, ocy = (self.bbox.x1 + self.bbox.x2) / 2, (self.bbox.y1 + self.bbox.y2) / 2
            ncx, ncy = (det.bbox.x1 + det.bbox.x2) / 2, (det.bbox.y1 + det.bbox.y2) / 2
            # Smoothed velocity — one jittery box must not fling the prediction.
            self.vx = 0.6 * self.vx + 0.4 * (ncx - ocx) / dt
            self.vy = 0.6 * self.vy + 0.4 * (ncy - ocy) / dt
        self.bbox = det.bbox
        self.confidence = det.confidence
        self.last_seen = now
        self.hits += 1
        fx, fy = det.bbox.foot_point()
        self.history.append((now, fx, fy))
        if len(self.history) > 120:
            del self.history[:40]

    def to_dict(self, camera_id: str) -> dict:
        return {
            "track_id": self.track_id, "camera_id": camera_id, "state": self.state.value,
            "confidence": round(self.confidence, 2),
            "first_seen_at": self.first_seen, "last_seen_at": self.last_seen,
            "bbox": {"x1": round(self.bbox.x1, 4), "y1": round(self.bbox.y1, 4),
                     "x2": round(self.bbox.x2, 4), "y2": round(self.bbox.y2, 4)},
        }


@dataclass
class TrackerConfig:
    high_thresh: float = 0.5   # first-pass detections
    low_thresh: float = 0.25   # second-pass (occlusion rescue)
    # Tuned on the lab clips: 0.15 / 0.65 cut ID churn by ~25 % on a crowded
    # mall scene versus 0.25 / 0.55, with no merged identities seen.
    new_track_thresh: float = 0.65
    match_iou: float = 0.15
    confirm_hits: int = 2      # NEW → ACTIVE after this many matches
    lost_after_s: float = 2.0  # §9: no match for <2 s → TEMPORARILY_LOST
    end_after_s: float = 5.0   # §9: no match for >5 s → ENDED


_ids = itertools.count(1)


class ByteTracker:
    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.cfg = config or TrackerConfig()
        self.tracks: list[Track] = []

    @staticmethod
    def _match(tracks: list[Track], dets: list[Detection], now: float, min_iou: float):
        pairs = sorted(
            ((iou(t.predict(now), d.bbox), ti, di) for ti, t in enumerate(tracks) for di, d in enumerate(dets)),
            reverse=True,
        )
        used_t, used_d, matches = set(), set(), []
        for score, ti, di in pairs:
            if score < min_iou:
                break
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            matches.append((ti, di))
        return matches, [i for i in range(len(tracks)) if i not in used_t], [i for i in range(len(dets)) if i not in used_d]

    def update(self, dets: list[Detection], now: float | None = None) -> tuple[list[Track], list[Track], list[Track]]:
        """→ (live tracks, tracks created this frame, tracks ended this frame)."""
        now = time.monotonic() if now is None else now
        live = [t for t in self.tracks if t.state != TrackState.ENDED]
        high = [d for d in dets if d.confidence >= self.cfg.high_thresh]
        low = [d for d in dets if self.cfg.low_thresh <= d.confidence < self.cfg.high_thresh]

        m1, un_t, un_d = self._match(live, high, now, self.cfg.match_iou)
        for ti, di in m1:
            live[ti].update(high[di], now)
        rest = [live[i] for i in un_t]
        m2, un_t2, _ = self._match(rest, low, now, self.cfg.match_iou)
        for ti, di in m2:
            rest[ti].update(low[di], now)

        created: list[Track] = []
        for di in un_d:
            d = high[di]
            if d.confidence >= self.cfg.new_track_thresh:
                t = Track(f"track_{next(_ids):04d}", d.bbox, d.confidence, now, now)
                fx, fy = d.bbox.foot_point()
                t.history.append((now, fx, fy))
                self.tracks.append(t)
                created.append(t)

        ended: list[Track] = []
        for t in self.tracks:
            gap = now - t.last_seen
            if gap > self.cfg.end_after_s:
                t.state = TrackState.ENDED
                ended.append(t)
            elif gap >= self.cfg.lost_after_s:
                t.state = TrackState.TEMPORARILY_LOST
            elif t.last_seen == now:  # matched on this frame
                t.state = TrackState.ACTIVE if t.hits >= self.cfg.confirm_hits else TrackState.NEW
            # else: a brief miss (< lost_after_s) keeps its current state
        self.tracks = [t for t in self.tracks if t.state != TrackState.ENDED]
        return [t for t in self.tracks], created, ended
