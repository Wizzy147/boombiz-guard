"""Upload policy for shops on weak or metered internet (Phase 4 §98–99).

    NORMAL          details, snapshots and clips as recorded
    LOW             details first as always; snapshots shrunk to 960 px,
                    clips re-encoded to 360p / 10 fps for the upload only
    METADATA_ONLY   incident details only; snapshots and clips wait here.
                    Temporary: switches back to NORMAL after 24 h so nobody
                    forgets it on and loses remote evidence for weeks.

The backlog cap (§99) limits how much media may wait for upload (default
3 GB). See SyncQueue.enforce_cap for what gets skipped first.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import CloudLink

MODES = ("NORMAL", "LOW", "METADATA_ONLY")
METADATA_ONLY_HOURS = 24
DEFAULT_CAP_BYTES = 3 * 1024**3
MIN_CAP_GB, MAX_CAP_GB = 1, 20


class Bandwidth:
    def __init__(self, link: "CloudLink") -> None:
        self.link = link

    def get(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        try:
            d = json.loads(self.link._get("cloud_bandwidth") or "{}")
        except ValueError:
            d = {}
        mode = d.get("mode") if d.get("mode") in MODES else "NORMAL"
        until = d.get("until")
        if mode == "METADATA_ONLY" and until and datetime.fromisoformat(until) <= now:
            self.link._put("cloud_bandwidth", json.dumps({"mode": "NORMAL", "until": None}))
            self.link.db.audit("cloud_bandwidth_changed", None, mode="NORMAL", reason="metadata-only period ended")
            mode, until = "NORMAL", None
        return {"mode": mode, "until": until if mode == "METADATA_ONLY" else None,
                "cap_gb": round(self.cap_bytes() / 1024**3, 1)}

    def mode(self) -> str:
        return self.get()["mode"]

    def set(self, mode: str, now: datetime | None = None) -> dict:
        if mode not in MODES:
            raise ValueError("Unknown bandwidth mode.")
        now = now or datetime.now(timezone.utc)
        until = (now + timedelta(hours=METADATA_ONLY_HOURS)).isoformat() if mode == "METADATA_ONLY" else None
        self.link._put("cloud_bandwidth", json.dumps({"mode": mode, "until": until}))
        self.link.db.audit("cloud_bandwidth_changed", None, mode=mode, until=until)
        return self.get(now)

    def cap_bytes(self) -> int:
        raw = self.link._get("cloud_media_cap_bytes")
        try:
            return max(1, int(raw)) if raw else DEFAULT_CAP_BYTES
        except ValueError:
            return DEFAULT_CAP_BYTES

    def set_cap_gb(self, gb: float) -> None:
        if not MIN_CAP_GB <= gb <= MAX_CAP_GB:
            raise ValueError(f"Choose between {MIN_CAP_GB} and {MAX_CAP_GB} GB.")
        self.link._put("cloud_media_cap_bytes", str(int(gb * 1024**3)))
        self.link.db.audit("cloud_media_cap_changed", None, gb=gb)
