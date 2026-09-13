"""Best-snapshot selection (Phase 3 §20, §53).

Among the frames within ±1.5 s of the trigger, prefer:
  · sharp (variance of the Laplacian — low for motion blur),
  · the person in shot (their box, when the track is known),
  · close to the event moment.
Scores are relative within the candidate set, so one blurry camera doesn't
make every frame lose.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..buffer.rolling_buffer import Frame
from ..zones.geometry import BBox


def sharpness(img: np.ndarray, box: BBox | None = None) -> float:
    if box is not None:
        h, w = img.shape[:2]
        x1, y1 = max(0, int(box.x1 * w)), max(0, int(box.y1 * h))
        x2, y2 = min(w, int(box.x2 * w)), min(h, int(box.y2 * h))
        if x2 - x1 > 8 and y2 - y1 > 8:
            img = img[y1:y2, x1:x2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def pick_snapshot(frames: list[Frame], trigger_ts: float, box: BBox | None = None) -> Frame | None:
    if not frames:
        return None
    scored = []
    for f in frames:
        img = cv2.imdecode(np.frombuffer(f.jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue  # corrupt frame: never a snapshot (§56)
        scored.append((f, sharpness(img, box), abs(f.ts - trigger_ts)))
    if not scored:
        return None
    top = max(s for _, s, _ in scored) or 1.0
    span = max(d for _, _, d in scored) or 1.0
    # 70 % sharpness, 30 % closeness to the event moment.
    return max(scored, key=lambda x: 0.7 * x[1] / top + 0.3 * (1 - x[2] / span))[0]


def thumbnail(jpeg: bytes, width: int = 320) -> bytes:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    small = cv2.resize(img, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, out = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return out.tobytes()


def mask_privacy(jpeg: bytes, polygons: list[list[tuple[float, float]]]) -> bytes:
    """Black out PRIVACY zones in stored media too — not just in the AI's copy."""
    if not polygons:
        return jpeg
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    h, w = img.shape[:2]
    for poly in polygons:
        cv2.fillPoly(img, [np.array([[int(x * w), int(y * h)] for x, y in poly], np.int32)], (0, 0, 0))
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return out.tobytes()
