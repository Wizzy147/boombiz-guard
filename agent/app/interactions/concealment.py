"""POSSIBLE_CONCEALMENT — experimental (PRD §33). ALWAYS LOW confidence.

Cue (all needed):
  1. The person had a SHELF_INTERACTION in the last `window_s` seconds.
  2. During that interaction a wrist was inside/next to the shelf (a hand
     that actually went to the product — from pose, not the body box).
  3. After they step back from the shelf, that same hand goes to the
     waist / pocket band on THEIR OWN body (around the hip keypoints) and
     stays ≥ `hold_s`.
     Chest height is deliberately NOT a concealment region: that is how
     people carry a product they are about to pay for.
  4. It happens within `after_s` seconds of leaving the shelf.

Putting a hand in a pocket is also what people do for a phone or money, so
this event never stands alone as an alert. It can only raise the confidence
of a POSSIBLE_UNPAID_EXIT by one step (LOW→MEDIUM, MEDIUM→HIGH is NOT
allowed — it tops out at MEDIUM unless the shelf evidence was already
strong). No face, no identity: only body points of one tracked person.

Pose runs only on people with a recent shelf interaction, at `pose_fps`, so
the cost is bounded by how many people are handling products, not by crowd
size. It is the first feature paused under CPU load (§44).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..ai.pose import L_ELBOW, L_HIP, L_SHOULDER, L_WRIST, R_ELBOW, R_HIP, R_SHOULDER, R_WRIST, Pose
from ..zones.geometry import BBox, contains
from ..zones.engine import ZoneDef


@dataclass
class ConcealmentConfig:
    window_s: float = 20.0       # shelf interaction must be this recent
    after_s: float = 8.0         # hand-to-body must happen this soon after leaving the shelf
    hold_s: float = 0.6          # …and stay there this long
    pose_fps: float = 5.0
    min_kp_score: float = 0.35
    shelf_margin: float = 0.04   # wrist within this distance of the shelf polygon counts as "at the shelf"
    waist_band: float = 0.35     # ± fraction of torso height around the hip line
    # Walking people's hands swing at hip height — that is not a pocket. The
    # hold only counts while the person is nearly still (same limit as the
    # shelf dwell). Someone pocketing mid-stride is missed: accepted for an
    # always-LOW signal.
    max_hold_speed: float = 0.06
    # Far-away people give unreliable keypoints; don't judge them.
    min_body_height: float = 0.2


@dataclass
class _HandState:
    touched_shelf: bool = False
    in_region_since: float | None = None


@dataclass
class _TrackState:
    shelf_zone_id: str | None = None
    interaction_at: float = 0.0
    left_shelf_at: float | None = None
    hands: dict[str, _HandState] = field(default_factory=lambda: {"L": _HandState(), "R": _HandState()})
    last_pose_at: float = 0.0
    fired: bool = False
    region: str | None = None


def _near_polygon(poly: list[tuple[float, float]], p: tuple[float, float], margin: float) -> bool:
    if contains(poly, p):
        return True
    x, y = p
    return any(contains(poly, (x + dx, y + dy)) for dx in (-margin, 0, margin) for dy in (-margin, 0, margin))


def concealment_region(pose: Pose, box: BBox, wrist: tuple[float, float], cfg: ConcealmentConfig) -> str | None:
    """'waist' | None — is the hand at the person's own waist/pocket band?"""
    s = cfg.min_kp_score
    ls, rs, lh, rh = pose.pt(L_SHOULDER, s), pose.pt(R_SHOULDER, s), pose.pt(L_HIP, s), pose.pt(R_HIP, s)
    if not (lh or rh):
        return None
    hips = [p for p in (lh, rh) if p]
    hip_y = float(np.mean([p[1] for p in hips]))
    shoulders = [p for p in (ls, rs) if p]
    sh_y = float(np.mean([p[1] for p in shoulders])) if shoulders else box.y1 + box.h * 0.2
    torso = max(1e-3, hip_y - sh_y)
    xs = [p[0] for p in hips + shoulders]
    x_lo, x_hi = min(xs) - box.w * 0.15, max(xs) + box.w * 0.15
    wx, wy = wrist
    if not (x_lo <= wx <= x_hi):
        return None
    if abs(wy - hip_y) <= torso * cfg.waist_band:
        return "waist"
    return None


class ConcealmentEngine:
    def __init__(self, config: ConcealmentConfig | None = None) -> None:
        self.cfg = config or ConcealmentConfig()
        self.tracks: dict[str, _TrackState] = {}

    def on_shelf_interaction(self, track_id: str, shelf_zone_id: str, now: float) -> None:
        st = self.tracks.setdefault(track_id, _TrackState())
        st.shelf_zone_id, st.interaction_at, st.left_shelf_at, st.fired = shelf_zone_id, now, None, False
        st.hands = {"L": _HandState(), "R": _HandState()}

    def wants_pose(self, track_id: str, now: float) -> bool:
        st = self.tracks.get(track_id)
        if not st or st.fired or now - st.interaction_at > self.cfg.window_s:
            return False
        return now - st.last_pose_at >= 1.0 / self.cfg.pose_fps

    def update(self, track_id: str, box: BBox, pose: Pose, shelf: ZoneDef | None, now: float,
               speed: float = 0.0) -> str | None:
        """Feed one pose; → 'waist' when concealment is confirmed."""
        st = self.tracks.get(track_id)
        if not st or st.fired:
            return None
        st.last_pose_at = now
        s = self.cfg.min_kp_score
        judgeable = box.h >= self.cfg.min_body_height
        still = speed <= self.cfg.max_hold_speed
        at_shelf_any = False
        for side, wi, ei in (("L", L_WRIST, L_ELBOW), ("R", R_WRIST, R_ELBOW)):
            wrist = pose.pt(wi, s)
            hand = st.hands[side]
            if wrist is None:
                hand.in_region_since = None
                continue
            if shelf and _near_polygon(shelf.polygon, wrist, self.cfg.shelf_margin):
                hand.touched_shelf = True
                hand.in_region_since = None
                at_shelf_any = True
                continue
            if not hand.touched_shelf or st.left_shelf_at is None:
                continue
            if now - st.left_shelf_at > self.cfg.after_s:
                continue
            region = concealment_region(pose, box, wrist, self.cfg) if (judgeable and still) else None
            if region is None:
                hand.in_region_since = None
                continue
            hand.in_region_since = hand.in_region_since or now
            if now - hand.in_region_since >= self.cfg.hold_s:
                st.fired, st.region = True, region
                return region
        if not at_shelf_any and st.left_shelf_at is None and any(h.touched_shelf for h in st.hands.values()):
            st.left_shelf_at = now
        return None

    def forget(self, track_id: str) -> None:
        self.tracks.pop(track_id, None)
