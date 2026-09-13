"""Event deduplication (Phase 2 §15, §32).

A key is (camera, track, event type, zone). `once` keys never repeat for the
life of the process (PERSON_DETECTED per track, RESTRICTED_ZONE_ENTRY per
track per zone); others repeat only after their TTL. Memory is bounded: old
keys are pruned.
"""

from __future__ import annotations

import time

# Event types that fire at most once per (camera, track, zone).
ONCE_PER_TRACK = {
    "PERSON_DETECTED", "RESTRICTED_ZONE_ENTRY", "AFTER_HOURS_PERSON",
    "POSSIBLE_UNPAID_EXIT", "EXIT_APPROACH", "TRACK_LOST", "POSSIBLE_CONCEALMENT",
}
DEFAULT_TTL_S = 10.0


class Deduper:
    def __init__(self, max_keys: int = 20_000) -> None:
        self._seen: dict[tuple, float] = {}
        self.max_keys = max_keys

    def allow(self, camera_id: str, track_id: str | None, event_type: str, zone_id: str | None,
              now: float | None = None, ttl: float = DEFAULT_TTL_S) -> bool:
        now = time.monotonic() if now is None else now
        key = (camera_id, track_id, event_type, zone_id)
        last = self._seen.get(key)
        if last is not None and (event_type in ONCE_PER_TRACK and track_id is not None or now - last < ttl):
            return False
        self._seen[key] = now
        if len(self._seen) > self.max_keys:
            for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[: self.max_keys // 4]:
                self._seen.pop(k, None)
        return True
