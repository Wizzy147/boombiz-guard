"""Per-camera rolling pre-event buffer (Phase 3 §15–16, §48–49) — RAM only.

Holds the last `seconds` of the JPEG frames the stream worker already
decodes for the AI (substream, ~640 px, 10 fps). It is continuously
overwritten and never written to disk: Guard is NOT a second recorder
(§3). About 6 MB per camera for 10 s.

When an incident triggers, a `Capture` is opened: it copies the pre-event
frames (≤ pre_s) at that instant, then keeps collecting post-event frames
until trigger + post_s. The capture — not the ring — is what the clip is
built from, so a busy camera can't overwrite an incident's pre-event
context while the clip is still being assembled. Hard cap: 15 s (§17).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

MAX_CLIP_S = 15.0


@dataclass
class Frame:
    ts: float    # monotonic capture time (same clock as the AI)
    jpeg: bytes


@dataclass
class Capture:
    incident_id: str
    camera_id: str
    trigger_ts: float
    pre_s: float
    post_s: float
    frames: list[Frame] = field(default_factory=list)
    closed: bool = False
    # Wall-clock (monotonic) moment the capture opened. If the camera drops
    # right after an incident, post-event frames never arrive; once the post
    # window plus a grace period has passed in real time, the clip is built
    # from what exists (a shorter clip is allowed; no clip is not).
    opened_at: float = field(default_factory=time.monotonic)
    GRACE_S = 5.0

    @property
    def end_ts(self) -> float:
        return self.trigger_ts + self.post_s

    def overdue(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) - self.opened_at > self.post_s + self.GRACE_S

    def clipped(self) -> list[Frame]:
        """Frames inside [trigger − pre, trigger + post], never more than 15 s."""
        start = self.trigger_ts - self.pre_s
        end = min(self.end_ts, start + MAX_CLIP_S)
        return [f for f in self.frames if start <= f.ts <= end]


class RollingBuffer:
    def __init__(self, camera_id: str, seconds: float = 10.0, max_frames: int = 400) -> None:
        self.camera_id = camera_id
        self.seconds = seconds
        self._ring: deque[Frame] = deque(maxlen=max_frames)
        self._captures: list[Capture] = []
        self._lock = threading.Lock()

    def push(self, jpeg: bytes, ts: float) -> None:
        with self._lock:
            self._ring.append(Frame(ts, jpeg))
            while self._ring and ts - self._ring[0].ts > self.seconds:
                self._ring.popleft()
            for c in self._captures:
                if not c.closed and ts <= c.end_ts:
                    c.frames.append(Frame(ts, jpeg))
                elif not c.closed and ts > c.end_ts:
                    c.closed = True
            self._captures = [c for c in self._captures if not c.closed]

    def latest(self) -> Frame | None:
        with self._lock:
            return self._ring[-1] if self._ring else None

    def around(self, ts: float, before: float, after: float) -> list[Frame]:
        with self._lock:
            return [f for f in self._ring if ts - before <= f.ts <= ts + after]

    def start_capture(self, incident_id: str, trigger_ts: float, pre_s: float = 5.0, post_s: float = 10.0) -> Capture:
        pre_s = min(pre_s, self.seconds)
        post_s = min(post_s, MAX_CLIP_S - pre_s)
        cap = Capture(incident_id, self.camera_id, trigger_ts, pre_s, post_s)
        with self._lock:
            cap.frames = [f for f in self._ring if trigger_ts - pre_s <= f.ts <= trigger_ts + post_s]
            if self._ring and self._ring[-1].ts > cap.end_ts:
                cap.closed = True
            else:
                self._captures.append(cap)
        return cap

    def memory_bytes(self) -> int:
        with self._lock:
            return sum(len(f.jpeg) for f in self._ring)
