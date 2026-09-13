"""Visual smoke/fire — EXPERIMENTAL HEURISTIC (Phase 2 §34–36).

There is no validated, well-licensed open fire model yet, so this is a
deliberately conservative colour + flicker + motion heuristic behind the
same interface a real ONNX fire model will implement later (`FireModel`).
It is OFF by default per camera, and every UI surface must say:

    "Visual AI warning only. Not a replacement for certified fire
     detection systems."

Flame cue:  saturated bright red–orange–yellow pixels (HSV) that also
            FLICKER — their mask changes frame to frame. Orange clothing and
            signage are bright and saturated but static, so flicker is what
            separates them.
Smoke cue:  a low-saturation grey region that is growing and moving while
            the scene behind it loses contrast (edges soften).

Runs at 1–3 FPS on a downscaled frame; costs ~1 ms per frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

W = 320  # analysis width


@dataclass
class FireObservation:
    flame_score: float  # 0–1
    smoke_score: float  # 0–1
    flame_frac: float   # fraction of analysed area


class FireModel(Protocol):
    version: str

    def observe(self, frame_bgr: np.ndarray, mask: np.ndarray | None = None) -> FireObservation: ...


class HeuristicFireModel:
    version = "guard-fire-heuristic-0.1.0"

    def __init__(self) -> None:
        self._prev_flame: np.ndarray | None = None
        self._prev_gray: np.ndarray | None = None
        self._prev_smoke_frac = 0.0

    def observe(self, frame_bgr: np.ndarray, mask: np.ndarray | None = None) -> FireObservation:
        h, w = frame_bgr.shape[:2]
        small = cv2.resize(frame_bgr, (W, max(1, int(h * W / w))), interpolation=cv2.INTER_AREA)
        if mask is not None:
            mask = cv2.resize(mask, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hch, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]

        flame = ((hch <= 35) | (hch >= 170)) & (s >= 120) & (v >= 200)
        if mask is not None:
            flame &= mask > 0
        flame_frac = float(flame.mean())
        flicker = 0.0
        if self._prev_flame is not None and flame.any():
            changed = np.logical_xor(flame, self._prev_flame).sum()
            flicker = min(1.0, changed / max(1, flame.sum()))
        self._prev_flame = flame
        # Needs area AND flicker; static orange scores ~0.
        flame_score = min(1.0, flame_frac * 40) * min(1.0, flicker * 2.5)

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        smoke = (s < 40) & (v > 90) & (v < 220)
        if mask is not None:
            smoke &= mask > 0
        smoke_frac = float(smoke.mean())
        smoke_score = 0.0
        if self._prev_gray is not None:
            motion = cv2.absdiff(gray, self._prev_gray)
            moving_grey = float(((motion > 8) & smoke).mean())
            growing = smoke_frac - self._prev_smoke_frac
            edges = float(cv2.Laplacian(gray, cv2.CV_32F).var())
            softness = 1.0 if edges < 60 else max(0.0, 1.0 - (edges - 60) / 240)
            smoke_score = min(1.0, moving_grey * 12) * (0.5 + 0.5 * min(1.0, max(0.0, growing) * 50)) * softness
        self._prev_gray = gray
        self._prev_smoke_frac = smoke_frac
        return FireObservation(round(flame_score, 3), round(smoke_score, 3), round(flame_frac, 4))
