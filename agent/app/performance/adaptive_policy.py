"""Adaptive AI load policy (Phase 2 §6, §43–44). The POS always wins.

Load levels, decided on SUSTAINED CPU (the monitor's smoothed value, so a
one-second spike doesn't flap the FPS):

    NORMAL    CPU < 70      10 FPS
    MODERATE  70–85          7 FPS
    HEAVY     85–92          5 FPS, secondary cameras drop to 3
    CRITICAL  > 92 sustained 3 FPS, experimental modules off,
                             shelf + fire paused, person/exit/restricted/
                             after-hours kept (§44 priority order)

Protection never silently stops: `reduced` is surfaced to the UI as
"Guard is operating in reduced-performance mode".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LoadLevel(StrEnum):
    NORMAL = "NORMAL"
    MODERATE = "MODERATE"
    HEAVY = "HEAVY"
    CRITICAL = "CRITICAL"


# §44, most important first. Under load, features are shed from the END.
FEATURE_PRIORITY = ["person", "exit", "restricted", "after_hours", "fire", "shelf", "concealment"]


@dataclass(frozen=True)
class Plan:
    level: LoadLevel
    primary_fps: float
    secondary_fps: float
    disabled_features: frozenset[str]

    @property
    def reduced(self) -> bool:
        return self.level != LoadLevel.NORMAL

    def fps_for(self, priority: str) -> float:
        return self.primary_fps if priority == "PRIMARY" else self.secondary_fps

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "primary_fps": self.primary_fps,
            "secondary_fps": self.secondary_fps,
            "disabled_features": sorted(self.disabled_features),
            "reduced": self.reduced,
            "message": "Guard is operating in reduced-performance mode." if self.reduced else None,
        }


@dataclass
class AdaptivePolicy:
    max_fps: float = 10.0
    # Hysteresis: step back UP only when CPU is this much below the threshold.
    hysteresis: float = 5.0
    level: LoadLevel = LoadLevel.NORMAL

    def _level_for(self, cpu: float) -> LoadLevel:
        if cpu > 92:
            return LoadLevel.CRITICAL
        if cpu > 85:
            return LoadLevel.HEAVY
        if cpu >= 70:
            return LoadLevel.MODERATE
        return LoadLevel.NORMAL

    def update(self, sustained_cpu: float) -> Plan:
        target = self._level_for(sustained_cpu)
        order = list(LoadLevel)
        if order.index(target) < order.index(self.level):
            # Only relax once clearly below the boundary we crossed.
            if self._level_for(sustained_cpu + self.hysteresis) == target:
                self.level = target
        else:
            self.level = target
        return self.plan()

    def plan(self) -> Plan:
        m = self.max_fps
        if self.level == LoadLevel.NORMAL:
            return Plan(self.level, m, m, frozenset())
        if self.level == LoadLevel.MODERATE:
            return Plan(self.level, min(m, 7), min(m, 7), frozenset({"concealment"}))
        if self.level == LoadLevel.HEAVY:
            return Plan(self.level, min(m, 5), min(m, 3), frozenset({"concealment"}))
        return Plan(self.level, min(m, 3), min(m, 3), frozenset({"concealment", "shelf", "fire"}))
