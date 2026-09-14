"""Shelf interaction V1 — an MVP HEURISTIC (Phase 2 §17–22, §27).

It never claims an item was taken. It answers two narrower questions:

1. SHELF_INTERACTION — did a tracked person physically reach into a shelf
   area?  Needed together, for ≥ 500 ms:
     · the person's upper-body/arm region overlaps the shelf polygon, and
     · there is localised motion in that overlap (hands moving, not just a
       body standing next to the shelf).

2. UNRESOLVED_SHELF_INTERACTION — after they step away, does the shelf look
   the way it did before?  A clean "before" picture of the shelf is kept
   whenever nobody is in front of it. When the person leaves, Guard waits
   for the view to settle (1 s, and until nobody else blocks it), then
   compares the stable "after" picture with "before". A real change beyond
   noise → unresolved. Back to how it was → resolved.
   If the shelf stays blocked by other people for 5 s, the interaction is
   left UNRESOLVED but UNVERIFIED — Guard couldn't check, says so, and any
   unpaid-exit it feeds is LOW confidence.
   If the camera moved or the lighting changed, the comparison can't be
   trusted and the interaction closes without an alert.

3. WHAT changed (product swap, V1 heuristic): the changed patch is compared
   with the ring of shelf right around it, before and after.
     · object before, looks like its surroundings after   → TAKEN
     · looks like its surroundings before, object after   → ADDED (put back /
       restocked — no alert)
     · an object before AND after, but a different one    → REPLACED:
       "Possible product swap" — someone took a product and left something
       else, or a fake, in its place. Also counts as unresolved, so walking
       out afterwards is still a possible unpaid exit.
   Grayscale histograms only: it sees "different object", never which
   product. Tuned from pilot footage like the rest of this file.

Each interaction is its own record (§27): pick-up-and-return then pick-up
again is one RESOLVED + one UNRESOLVED.

Tunables are deliberately exposed; they will be set from pilot footage.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum

import cv2
import numpy as np

from ..zones.engine import ZoneDef, ZoneEngine
from ..zones.geometry import BBox, polygon_bounds


class InteractionState(StrEnum):
    APPROACHING = "APPROACHING"
    INTERACTING = "INTERACTING"
    PENDING_CHECK = "PENDING_CHECK"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


@dataclass
class ShelfConfig:
    reach_overlap: float = 0.18     # share of the upper-body box inside the shelf
    motion_frac: float = 0.02       # share of the overlap region that moved
    min_interaction_s: float = 0.5  # §18: > 500 ms
    leave_grace_s: float = 0.7      # overlap must be gone this long to count as "left"
    settle_s: float = 1.0           # §22: wait 0.5–1.5 s before comparing
    blocked_timeout_s: float = 5.0
    change_frac: float = 0.015      # shelf pixels changed beyond noise → unresolved
    strong_change_frac: float = 0.05
    pixel_delta: int = 28
    # Walking past is not reaching in: the person's feet must be nearly still
    # (normalised frame-widths per second) while their arms are in the shelf.
    max_dwell_speed: float = 0.06
    # The shelf comparison is only trusted when the CAMERA held still and the
    # LIGHTING held steady between "before" and "after". People walking around
    # the rest of the shop are expected and must not count — so these are
    # measured directly (phase-correlation shift, global brightness), not as
    # "how much of the picture changed". Pixels are at the 96-wide control size.
    max_camera_shift_px: float = 1.5
    # Only a shift the correlation is sure of counts as "the camera moved";
    # a near-featureless picture gives a random, low-confidence shift.
    min_shift_confidence: float = 0.08
    # Lighting is judged on the SHELF's own pixels: removing one item leaves
    # most of them alone (median change ≈ 0), a light going off moves them all.
    max_brightness_delta: float = 12.0
    # Scene cut: when most of the WHOLE picture changed between "before" and
    # "after", the camera was knocked or turned (or, in the lab, a looping clip
    # restarted). Phase correlation can't catch that — two unrelated pictures
    # give a low-confidence shift — so it's checked directly. People moving
    # change ~20 % of a busy scene; a different view changes most of it.
    max_scene_change: float = 0.5
    scene_pixel_delta: int = 40
    # Swap verdict: histogram (Bhattacharyya) distance below this = "looks like
    # the shelf around it"; above swap_difference = "a different object".
    bg_similarity: float = 0.35
    swap_difference: float = 0.5
    min_ring_px: int = 20


_ids = itertools.count(1)


@dataclass
class Interaction:
    id: str
    track_id: str
    shelf_zone_id: str
    started_at: float
    state: InteractionState = InteractionState.APPROACHING
    interacting_since: float | None = None
    last_overlap_at: float = 0.0
    ended_at: float | None = None
    check_after: float | None = None
    announced: bool = False
    change_frac: float | None = None
    strength: str | None = None  # STRONG | WEAK
    control_change: float | None = None     # camera shift, px at 96-wide
    brightness_change: float | None = None
    scene_change: float | None = None       # share of the whole frame that changed
    # False when the camera moved or the lighting changed too much to trust the
    # shelf comparison; such an interaction is closed without an alert.
    verifiable: bool | None = None
    verdict: str | None = None  # TAKEN | REPLACED | ADDED (what changed on the shelf)
    before: np.ndarray | None = field(default=None, repr=False)
    control_before: np.ndarray | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"interaction_id": self.id, "track_id": self.track_id, "shelf_zone_id": self.shelf_zone_id,
                "interaction_started_at": self.started_at, "interaction_ended_at": self.ended_at,
                "state": self.state.value, "resolved": self.state == InteractionState.RESOLVED,
                "change_frac": self.change_frac, "strength": self.strength,
                "camera_shift_px": self.control_change, "brightness_change": self.brightness_change,
                "scene_change": self.scene_change,
                "verifiable": self.verifiable, "verdict": self.verdict}


@dataclass
class ShelfEvent:
    kind: str  # SHELF_INTERACTION | UNRESOLVED_SHELF_INTERACTION | RESOLVED
    interaction: Interaction


class _ShelfView:
    """Grayscale, blurred crop of one shelf polygon + its mask."""

    def __init__(self, zone: ZoneDef) -> None:
        self.zone = zone
        self.bounds = polygon_bounds(zone.polygon)
        self.baseline: np.ndarray | None = None
        self.mask: np.ndarray | None = None
        self.prev: np.ndarray | None = None

    def crop(self, gray: np.ndarray) -> np.ndarray:
        h, w = gray.shape
        b = self.bounds
        x1, y1 = int(b.x1 * w), int(b.y1 * h)
        x2, y2 = max(x1 + 2, int(b.x2 * w)), max(y1 + 2, int(b.y2 * h))
        roi = gray[y1:y2, x1:x2]
        if self.mask is None or self.mask.shape != roi.shape:
            pts = np.array([[int(x * w) - x1, int(y * h) - y1] for x, y in self.zone.polygon], np.int32)
            self.mask = np.zeros(roi.shape, np.uint8)
            cv2.fillPoly(self.mask, [pts], 255)
        return cv2.GaussianBlur(roi, (5, 5), 0)

    def changed_frac(self, a: np.ndarray, b: np.ndarray, delta: int) -> float:
        if a.shape != b.shape:
            return 0.0
        diff = cv2.absdiff(a, b)
        m = self.mask > 0
        return float(((diff > delta) & m).sum() / max(1, m.sum()))

    def region_motion(self, now_crop: np.ndarray, box: BBox, gray_shape, delta: int) -> float:
        """Motion inside (upper body ∩ shelf) between this and the previous frame."""
        if self.prev is None or self.prev.shape != now_crop.shape:
            return 0.0
        h, w = gray_shape
        b = self.bounds
        ub = box.upper_body()
        ix1, iy1 = max(ub.x1, b.x1), max(ub.y1, b.y1)
        ix2, iy2 = min(ub.x2, b.x2), min(ub.y2, b.y2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        ox, oy = int(b.x1 * w), int(b.y1 * h)
        xs, ys = int(ix1 * w) - ox, int(iy1 * h) - oy
        xe, ye = max(xs + 1, int(ix2 * w) - ox), max(ys + 1, int(iy2 * h) - oy)
        d = cv2.absdiff(now_crop[ys:ye, xs:xe], self.prev[ys:ye, xs:xe])
        m = self.mask[ys:ye, xs:xe] > 0
        return float(((d > delta) & m).sum() / max(1, m.sum()))


class ShelfInteractionEngine:
    def __init__(self, config: ShelfConfig | None = None) -> None:
        self.cfg = config or ShelfConfig()
        self.views: dict[str, _ShelfView] = {}
        self.active: dict[tuple[str, str], Interaction] = {}  # (track, shelf) → open interaction
        self.history: dict[str, list[Interaction]] = {}        # track → all its interactions

    def set_zones(self, zones: list[ZoneDef]) -> None:
        ids = {z.id for z in zones}
        self.views = {z.id: self.views.get(z.id) or _ShelfView(z) for z in zones}
        for v in self.views.values():
            v.zone = next(z for z in zones if z.id == v.zone.id)
        self.active = {k: v for k, v in self.active.items() if k[1] in ids}

    def _occupied(self, view: _ShelfView, boxes: list[BBox]) -> bool:
        return any(ZoneEngine.reach_overlap(b, view.zone) > 0.05 for b in boxes)

    @staticmethod
    def _control(gray: np.ndarray, boxes: list[BBox] | None = None) -> np.ndarray:
        """Small picture of the whole frame, used only to detect a moved camera
        or a lighting change between 'before' and 'after'."""
        return cv2.GaussianBlur(cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA), (3, 3), 0).astype(np.float32)

    @staticmethod
    def _camera_shift(a: np.ndarray | None, b: np.ndarray) -> tuple[float, float]:
        """→ (camera shift in px at 96-wide, confidence of that estimate)."""
        if a is None or a.shape != b.shape:
            return 0.0, 0.0
        win = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
        (dx, dy), response = cv2.phaseCorrelate(a, b, win)
        return float((dx * dx + dy * dy) ** 0.5), float(response)

    @staticmethod
    def _shelf_brightness_shift(view: "_ShelfView", before: np.ndarray, after: np.ndarray) -> float:
        if before.shape != after.shape or view.mask is None:
            return 0.0
        m = view.mask > 0
        return float(abs(np.median(after[m].astype(np.float32) - before[m].astype(np.float32))))

    @staticmethod
    def _hist_distance(a: np.ndarray, b: np.ndarray) -> float:
        """Bhattacharyya distance of two pixel sets' 16-bin histograms: 0 same, 1 unrelated."""
        if a.size == 0 or b.size == 0:
            return 0.0
        ha = np.histogram(a, bins=16, range=(0, 256))[0].astype(np.float64)
        hb = np.histogram(b, bins=16, range=(0, 256))[0].astype(np.float64)
        ha /= ha.sum() or 1.0
        hb /= hb.sum() or 1.0
        return float(np.sqrt(max(0.0, 1.0 - float(np.sqrt(ha * hb).sum()))))

    def _verdict(self, view: "_ShelfView", before: np.ndarray, after: np.ndarray) -> str | None:
        """TAKEN / REPLACED / ADDED for the biggest changed patch, or None if it can't tell."""
        if before.shape != after.shape or view.mask is None:
            return None
        m = view.mask > 0
        changed = ((cv2.absdiff(before, after) > self.cfg.pixel_delta) & m).astype(np.uint8)
        changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(changed, connectivity=8)
        if n <= 1:
            return None
        k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        comp = labels == k
        r = max(3, int(round(0.25 * np.sqrt(stats[k, cv2.CC_STAT_AREA]))))
        grown = cv2.dilate(comp.astype(np.uint8), np.ones((2 * r + 1, 2 * r + 1), np.uint8)) > 0
        ring = grown & ~(changed > 0) & m  # shelf that didn't change, just around the patch
        if int(ring.sum()) < self.cfg.min_ring_px:
            return None
        surroundings = after[ring]
        before_is_bg = self._hist_distance(before[comp], surroundings) < self.cfg.bg_similarity
        after_is_bg = self._hist_distance(after[comp], surroundings) < self.cfg.bg_similarity
        if before_is_bg and not after_is_bg:
            return "ADDED"
        if not before_is_bg and after_is_bg:
            return "TAKEN"
        if not before_is_bg and not after_is_bg and \
                self._hist_distance(before[comp], after[comp]) >= self.cfg.swap_difference:
            return "REPLACED"
        return "TAKEN"  # can't tell what: still a real change → unresolved, as before

    def update(self, frame_bgr: np.ndarray, tracks: dict[str, BBox], now: float,
               speeds: dict[str, float] | None = None) -> list[ShelfEvent]:
        if not self.views:
            return []
        speeds = speeds or {}
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        boxes = list(tracks.values())
        control = self._control(gray, boxes)
        events: list[ShelfEvent] = []
        for sid, view in self.views.items():
            crop = view.crop(gray)
            occupied = self._occupied(view, boxes)
            if not occupied:
                # Slow EMA toward the current empty shelf: absorbs lighting drift.
                if view.baseline is None or view.baseline.shape != crop.shape:
                    view.baseline = crop.astype(np.float32)
                elif not any(i.state == InteractionState.PENDING_CHECK for (t, s), i in self.active.items() if s == sid):
                    cv2.accumulateWeighted(crop.astype(np.float32), view.baseline, 0.05)

            for tid, box in tracks.items():
                overlap = ZoneEngine.reach_overlap(box, view.zone)
                key = (tid, sid)
                it = self.active.get(key)
                if overlap >= self.cfg.reach_overlap:
                    if it is None:
                        it = Interaction(f"interaction_{next(_ids)}", tid, sid, now)
                        it.before = view.baseline.copy() if view.baseline is not None else crop.astype(np.float32)
                        it.control_before = control
                        self.active[key] = it
                        self.history.setdefault(tid, []).append(it)
                    if it.state == InteractionState.PENDING_CHECK:
                        it.state, it.check_after = InteractionState.INTERACTING, None  # came back to the shelf
                    it.last_overlap_at = now
                    motion = view.region_motion(crop, box, gray.shape, self.cfg.pixel_delta)
                    dwelling = speeds.get(tid, 0.0) <= self.cfg.max_dwell_speed
                    if not dwelling and not it.announced:
                        it.interacting_since = None  # passing by: the 500 ms clock restarts
                    elif motion >= self.cfg.motion_frac:
                        it.interacting_since = it.interacting_since or now
                        if not it.announced and now - it.interacting_since >= self.cfg.min_interaction_s:
                            it.state = InteractionState.INTERACTING
                            it.announced = True
                            events.append(ShelfEvent("SHELF_INTERACTION", it))
            view.prev = crop

            # Close interactions whose person stepped away (or vanished).
            for (tid, s), it in list(self.active.items()):
                if s != sid:
                    continue
                gone = tid not in tracks or now - it.last_overlap_at >= self.cfg.leave_grace_s
                if it.state in (InteractionState.APPROACHING,) and gone:
                    self.active.pop((tid, s))  # never really interacted
                    continue
                if it.state == InteractionState.INTERACTING and gone:
                    it.state = InteractionState.PENDING_CHECK
                    it.ended_at = it.last_overlap_at
                    it.check_after = now + self.cfg.settle_s
                if it.state == InteractionState.PENDING_CHECK and now >= (it.check_after or now):
                    if not occupied:
                        frac = view.changed_frac(it.before.astype(np.uint8), crop, self.cfg.pixel_delta) if it.before is not None else 0.0
                        shift, conf = self._camera_shift(it.control_before, control)
                        bright = self._shelf_brightness_shift(view, it.before, crop) if it.before is not None else 0.0
                        it.change_frac = round(frac, 4)
                        it.control_change = round(shift, 2)
                        it.brightness_change = round(bright, 1)
                        # A small shift only counts when the correlation is sure of
                        # it (noise). A LARGE shift counts regardless: on a real,
                        # crowded, moving stream the confidence reads low while
                        # the camera genuinely moved 7–9 px — the lab caught
                        # false HIGH alerts slipping through exactly that gap.
                        camera_moved = shift > self.cfg.max_camera_shift_px and (
                            conf >= self.cfg.min_shift_confidence or shift > 3 * self.cfg.max_camera_shift_px)
                        scene = (float((np.abs(it.control_before - control) > self.cfg.scene_pixel_delta).mean())
                                 if it.control_before is not None and it.control_before.shape == control.shape else 0.0)
                        it.scene_change = round(scene, 3)
                        verifiable = (not camera_moved and bright <= self.cfg.max_brightness_delta
                                      and scene <= self.cfg.max_scene_change)
                        it.verifiable = verifiable
                        if frac >= self.cfg.change_frac and verifiable:
                            it.verdict = self._verdict(view, it.before.astype(np.uint8), crop) \
                                if it.before is not None else None
                        if frac >= self.cfg.change_frac and verifiable and it.verdict != "ADDED":
                            it.state = InteractionState.UNRESOLVED
                            it.strength = "STRONG" if frac >= self.cfg.strong_change_frac else "WEAK"
                            events.append(ShelfEvent("UNRESOLVED_SHELF_INTERACTION", it))
                        else:
                            it.state = InteractionState.RESOLVED
                            events.append(ShelfEvent("RESOLVED", it))
                            view.baseline = crop.astype(np.float32)
                        self.active.pop((tid, s))
                    elif now - (it.ended_at or now) > self.cfg.blocked_timeout_s:
                        it.state, it.strength = InteractionState.UNRESOLVED, "UNVERIFIED"
                        events.append(ShelfEvent("UNRESOLVED_SHELF_INTERACTION", it))
                        self.active.pop((tid, s))
        return events

    def forget_track(self, track_id: str, keep_s: float, now: float) -> None:
        """Drop history older than the correlation window (§26)."""
        hist = self.history.get(track_id)
        if hist is not None:
            self.history[track_id] = [i for i in hist if now - (i.ended_at or i.started_at) <= keep_s]
            if not self.history[track_id]:
                self.history.pop(track_id, None)
