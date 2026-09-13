"""Zone geometry on NORMALISED coordinates (Phase 2 §11–12).

Polygons are stored as fractions of the frame (0–1), so a zone drawn on a
640×360 substream still fits the 1920×1080 main stream. Everything here is
pure Python — it runs per track per frame, so no numpy allocations.
"""

from __future__ import annotations

from dataclasses import dataclass

Point = tuple[float, float]

MAX_POINTS = 32
MIN_AREA = 0.0005  # 0.05 % of the frame — smaller is almost certainly a mis-click


class PolygonError(ValueError):
    pass


def validate_polygon(raw: object) -> list[Point]:
    """Input validation for zone polygons (Phase 2 §62). Raises PolygonError."""
    if not isinstance(raw, list) or not 3 <= len(raw) <= MAX_POINTS:
        raise PolygonError(f"A zone needs between 3 and {MAX_POINTS} points.")
    pts: list[Point] = []
    for p in raw:
        try:
            x, y = (float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
        except (KeyError, TypeError, ValueError, IndexError):
            raise PolygonError("Each zone point needs an x and a y.") from None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise PolygonError("Zone points must lie inside the camera picture.")
        pts.append((x, y))
    if abs(area(pts)) < MIN_AREA:
        raise PolygonError("That zone is too small. Draw it around the whole area.")
    return pts


def area(poly: list[Point]) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(poly):
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def contains(poly: list[Point], pt: Point) -> bool:
    """Ray casting; points on the edge count as inside."""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x <= x_cross:
                inside = not inside
        j = i
    return inside


@dataclass(frozen=True)
class BBox:
    """Normalised box, x1<x2, y1<y2."""

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    def foot_point(self, frac: float = 0.9) -> Point:
        """Phase 2 §12: where the person stands, not where their head is."""
        return ((self.x1 + self.x2) / 2.0, self.y1 + self.h * frac)

    def upper_body(self) -> "BBox":
        """Top ~55 % of the body, widened — where arms reach a shelf from."""
        pad = self.w * 0.25
        return BBox(max(0.0, self.x1 - pad), self.y1, min(1.0, self.x2 + pad), self.y1 + self.h * 0.55)


def bbox_polygon_overlap(box: BBox, poly: list[Point], samples: int = 6) -> float:
    """Fraction of a box's sample grid that falls inside a polygon (cheap overlap)."""
    hit = 0
    total = samples * samples
    for i in range(samples):
        for j in range(samples):
            px = box.x1 + box.w * (i + 0.5) / samples
            py = box.y1 + box.h * (j + 0.5) / samples
            if contains(poly, (px, py)):
                hit += 1
    return hit / total


def polygon_bounds(poly: list[Point]) -> BBox:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return BBox(min(xs), min(ys), max(xs), max(ys))
