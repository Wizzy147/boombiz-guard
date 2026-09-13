"""Zone engine (Phase 2 §12–15, §23, §37).

For every live track it answers "which zones is this person standing in?"
using the FOOT point (§12), and emits ZONE_ENTRY / ZONE_EXIT transitions.
It keeps no history beyond the current membership per track — the
correlator owns anything longer-lived.

IGNORE and PRIVACY zones are special:
  · a detection whose foot point lies in one is dropped before tracking
    (a person on the TV screen, in the mirror, on the pavement outside);
  · a PRIVACY zone is also blacked out of the frame before any analysis
    touches it (§37), so nothing downstream ever "sees" that area.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from .geometry import BBox, Point, bbox_polygon_overlap, contains


class ZoneType(StrEnum):
    SHELF = "SHELF"
    EXIT = "EXIT"
    RESTRICTED = "RESTRICTED"
    CASHIER = "CASHIER"
    STOCKROOM = "STOCKROOM"
    FIRE_RISK = "FIRE_RISK"
    IGNORE = "IGNORE"
    PRIVACY = "PRIVACY"


SUPPRESSING = {ZoneType.IGNORE, ZoneType.PRIVACY}
# Zones a person can be "in" (membership is tracked for these).
OCCUPIABLE = {ZoneType.SHELF, ZoneType.EXIT, ZoneType.RESTRICTED, ZoneType.CASHIER, ZoneType.STOCKROOM}


@dataclass
class ZoneDef:
    id: str
    name: str
    zone_type: ZoneType
    polygon: list[Point]
    sensitivity: str = "MEDIUM"
    enabled: bool = True


@dataclass
class ZoneTransition:
    kind: str  # "ENTRY" | "EXIT"
    track_id: str
    zone: ZoneDef


@dataclass
class ZoneEngine:
    zones: list[ZoneDef] = field(default_factory=list)
    _membership: dict[str, set[str]] = field(default_factory=dict)

    def set_zones(self, zones: list[ZoneDef]) -> None:
        self.zones = [z for z in zones if z.enabled]
        valid = {z.id for z in self.zones}
        for tid in self._membership:
            self._membership[tid] &= valid

    def by_id(self, zone_id: str) -> ZoneDef | None:
        return next((z for z in self.zones if z.id == zone_id), None)

    def of_type(self, *types: ZoneType) -> list[ZoneDef]:
        return [z for z in self.zones if z.zone_type in types]

    # ── pre-tracking filters ────────────────────────────────────────
    def apply_privacy_mask(self, frame: np.ndarray) -> np.ndarray:
        privacy = self.of_type(ZoneType.PRIVACY)
        if not privacy:
            return frame
        import cv2

        h, w = frame.shape[:2]
        out = frame.copy()
        for z in privacy:
            pts = np.array([[int(x * w), int(y * h)] for x, y in z.polygon], dtype=np.int32)
            cv2.fillPoly(out, [pts], (0, 0, 0))
        return out

    def suppressed(self, box: BBox) -> bool:
        """True when a detection stands inside an IGNORE/PRIVACY zone."""
        foot = box.foot_point()
        return any(contains(z.polygon, foot) for z in self.of_type(*SUPPRESSING))

    # ── membership ──────────────────────────────────────────────────
    def zones_at(self, box: BBox) -> set[str]:
        foot = box.foot_point()
        return {z.id for z in self.zones if z.zone_type in OCCUPIABLE and contains(z.polygon, foot)}

    def evaluate(self, tracks: dict[str, BBox], ended: list[str]) -> list[ZoneTransition]:
        out: list[ZoneTransition] = []
        for tid, box in tracks.items():
            now_in = self.zones_at(box)
            before = self._membership.get(tid, set())
            for zid in now_in - before:
                out.append(ZoneTransition("ENTRY", tid, self.by_id(zid)))  # type: ignore[arg-type]
            for zid in before - now_in:
                z = self.by_id(zid)
                if z:
                    out.append(ZoneTransition("EXIT", tid, z))
            self._membership[tid] = now_in
        for tid in ended:
            for zid in self._membership.pop(tid, set()):
                z = self.by_id(zid)
                if z:
                    out.append(ZoneTransition("EXIT", tid, z))
        return out

    def current(self, track_id: str) -> set[str]:
        return set(self._membership.get(track_id, set()))

    @staticmethod
    def reach_overlap(box: BBox, zone: ZoneDef) -> float:
        """How much of a person's upper body / arm reach overlaps a shelf (§18)."""
        return bbox_polygon_overlap(box.upper_body(), zone.polygon)
