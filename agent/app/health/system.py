"""Basic system health (Phase 1 deliverable) — the host and the agent itself."""

from __future__ import annotations

import shutil
import time

import psutil

from ..config import settings

_STARTED = time.time()


def system_snapshot() -> dict:
    disk = psutil.disk_usage(str(settings.data_dir.anchor or "/"))
    vm = psutil.virtual_memory()
    return {
        "agent_uptime_s": int(time.time() - _STARTED),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(logical=True),
        "ram_percent": vm.percent,
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "ffmpeg_available": shutil.which(settings.ffmpeg) is not None,
        "ffprobe_available": shutil.which(settings.ffprobe) is not None,
    }
