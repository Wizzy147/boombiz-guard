"""Fire/smoke confirmation (Phase 2 §35–36). Never a single frame.

    POSSIBLE_FIRE   flame score ≥ threshold in ≥5 of the last 8 samples
    POSSIBLE_SMOKE  smoke score ≥ threshold continuously for ≥ smoke_seconds

FIRE_RISK zones lower the thresholds (§36): a flicker by the generator is
worth more attention than one by the shop window. A cooldown stops one fire
becoming a message every second.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .detector import FireObservation


@dataclass
class FireValidator:
    flame_threshold: float = 0.35
    smoke_threshold: float = 0.4
    risk_zone_factor: float = 0.7   # thresholds × this inside FIRE_RISK zones
    window: int = 8
    needed: int = 5
    smoke_seconds: float = 3.0
    cooldown_s: float = 120.0
    _flames: deque = field(default_factory=lambda: deque(maxlen=8))
    _smoke_since: float | None = None
    _last_fire: float = -1e9
    _last_smoke: float = -1e9

    def feed(self, obs: FireObservation, now: float, *, risk_zone: bool = False) -> list[str]:
        f = self.risk_zone_factor if risk_zone else 1.0
        self._flames.append(obs.flame_score >= self.flame_threshold * f)
        out: list[str] = []
        if sum(self._flames) >= self.needed and now - self._last_fire > self.cooldown_s:
            self._last_fire = now
            self._flames.clear()
            out.append("POSSIBLE_FIRE")
        if obs.smoke_score >= self.smoke_threshold * f:
            self._smoke_since = self._smoke_since or now
            if now - self._smoke_since >= self.smoke_seconds and now - self._last_smoke > self.cooldown_s:
                self._last_smoke = now
                self._smoke_since = None
                out.append("POSSIBLE_SMOKE")
        else:
            self._smoke_since = None
        return out
