"""Camera tamper check (owner decision 2026-09-14).

A thief rarely smashes a camera — they cover it, spray it, or turn it to face
the wall, and the video keeps arriving, so the offline check never fires. This
watches the picture itself, about once a second, on frames the AI worker has
already decoded (cheap: a 160×90 greyscale copy).

    COVERED  the picture lost almost all its detail (tape, a hand, paint, a bag)
    TURNED   the picture still has detail, but it isn't the scene this camera
             learned — its edges no longer line up with the usual ones

Edges, not colours, are compared, so the lights going off and the camera
switching to night vision don't count as "turned": the shelves are still where
they were. A state has to hold for CONFIRM_S (30 s) before it's reported, so a
person standing close to the lens or a lorry passing doesn't. It's reported
ONCE per episode; the picture must be back to normal for CLEAR_S before a new
episode can start.

Known limits (say them out loud in pilots): a camera in a room that goes fully
dark with no night vision reads as COVERED — it is blind either way; a slow,
small nudge is absorbed into the learned scene; big stock moves in front of the
camera can read as TURNED. Thresholds live in TamperConfig for pilot tuning.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

SIZE = (160, 90)


@dataclass
class TamperConfig:
    sample_interval_s: float = 1.0
    learn_s: float = 60.0           # steady picture needed before TURNED can be judged
    confirm_s: float = 30.0         # a state must hold this long to be reported
    clear_s: float = 10.0           # normal again for this long ends the episode
    min_detail: float = 7.0         # grey-level std below this = no detail
    min_edge_density: float = 0.006 # share of pixels that are edges, below this = no detail
    min_match: float = 0.3          # chance-corrected edge match with the learned scene, below = turned
    learn_rate: float = 0.02        # how fast the learned scene follows slow changes


def _dilate(m: np.ndarray) -> np.ndarray:
    return cv2.dilate(m.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0


def edge_match(ref: np.ndarray, cur: np.ndarray) -> float:
    """How well two edge maps line up, 0 = no better than chance, 1 = the same.

    Measured both ways — today's edges on the usual ones, and the usual edges
    still there today — each corrected for what a random picture of the same
    busyness would score, and the weaker one wins. A busy shelf scene covers a
    lot of the frame, so without the correction almost anything "overlaps".
    """
    if not ref.any() or not cur.any():
        return 0.0
    ref_d, cur_d = _dilate(ref), _dilate(cur)

    def corrected(hits: np.ndarray, total: np.ndarray, cover: np.ndarray) -> float:
        share = float(hits.sum()) / float(total.sum())
        chance = float(cover.mean())
        return (share - chance) / max(1e-6, 1.0 - chance)

    return min(corrected(cur & ref_d, cur, ref_d), corrected(ref & cur_d, ref, cur_d))


class TamperDetector:
    def __init__(self, cfg: TamperConfig | None = None) -> None:
        self.cfg = cfg or TamperConfig()
        self._ref: np.ndarray | None = None   # float edge-probability map
        self._learned_s = 0.0
        self._last_t: float | None = None
        self._suspect: tuple[str, float] | None = None
        self._reported = False
        self._ok_since: float | None = None
        self.last: dict = {}

    def _edges(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        small = cv2.resize(frame, SIZE, interpolation=cv2.INTER_AREA)
        grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        grey = cv2.GaussianBlur(grey, (3, 3), 0)
        return cv2.Canny(grey, 40, 120) > 0, float(grey.std())

    def observe(self, frame: np.ndarray, now: float) -> str | None:
        """→ "COVERED" | "TURNED" once, when a tamper is confirmed; otherwise None."""
        cfg = self.cfg
        if self._last_t is not None and now - self._last_t < cfg.sample_interval_s:
            return None
        dt = 0.0 if self._last_t is None else min(5.0, now - self._last_t)
        self._last_t = now

        edges, detail = self._edges(frame)
        density = float(edges.mean())
        match = None
        if detail < cfg.min_detail or density < cfg.min_edge_density:
            state = "COVERED"
        else:
            state = "OK"
            if self._ref is not None and self._learned_s >= cfg.learn_s:
                match = edge_match(self._ref > 0.5, edges)
                if match < cfg.min_match:
                    state = "TURNED"
        self.last = {"state": state, "detail": round(detail, 1), "edge_density": round(density, 4),
                     "match": None if match is None else round(match, 3)}

        if state == "OK":
            # Learn only from a normal picture, so a covered or turned camera
            # can never become "the usual scene".
            e = edges.astype(np.float32)
            self._ref = e if self._ref is None else (1 - cfg.learn_rate) * self._ref + cfg.learn_rate * e
            self._learned_s += dt
            self._suspect = None
            if self._reported:
                self._ok_since = self._ok_since if self._ok_since is not None else now
                if now - self._ok_since >= cfg.clear_s:
                    self._reported, self._ok_since = False, None
            return None

        self._ok_since = None
        if self._suspect is None or self._suspect[0] != state:
            self._suspect = (state, now)
            return None
        if not self._reported and now - self._suspect[1] >= cfg.confirm_s:
            self._reported = True
            return state
        return None
