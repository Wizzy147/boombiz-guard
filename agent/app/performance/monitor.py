"""Resource monitor (Phase 2 §42). Samples every few seconds; keeps a
smoothed "sustained" CPU so the policy reacts to load, not to spikes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import psutil

from ..config import settings
from .adaptive_policy import AdaptivePolicy, Plan

log = logging.getLogger(__name__)


class ResourceMonitor:
    def __init__(self, policy: AdaptivePolicy, interval_s: float = 3.0, window: int = 5) -> None:
        self.policy = policy
        self.interval_s = interval_s
        self._cpu: deque[float] = deque(maxlen=window)  # ~15 s at 3 s intervals
        self.snapshot: dict = {}
        self.plan: Plan = policy.plan()
        self._task: asyncio.Task | None = None
        # Test hook: pretend the CPU is this busy (GUARD_FAKE_CPU).
        self.forced_cpu: float | None = None
        psutil.cpu_percent(interval=None)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="resource-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def sample(self) -> Plan:
        cpu = self.forced_cpu if self.forced_cpu is not None else psutil.cpu_percent(interval=None)
        self._cpu.append(cpu)
        sustained = sum(self._cpu) / len(self._cpu)
        vm = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage(str(settings.data_dir.anchor or "/")).percent
        except OSError:
            disk = None
        temp = None
        try:  # rarely available on Windows; §42 "if accessible"
            temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
            for entries in temps.values():
                if entries:
                    temp = entries[0].current
                    break
        except Exception:
            pass
        before = self.plan.level
        self.plan = self.policy.update(sustained)
        if self.plan.level != before:
            log.info("ai_fps_changed level=%s primary_fps=%s secondary_fps=%s cpu=%.0f",
                     self.plan.level.value, self.plan.primary_fps, self.plan.secondary_fps, sustained)
            if self.plan.level.value in ("HEAVY", "CRITICAL"):
                log.warning("high_cpu sustained=%.0f", sustained)
        self.snapshot = {
            "at": time.time(), "cpu_percent": cpu, "cpu_sustained": round(sustained, 1),
            "ram_percent": vm.percent, "disk_percent": disk, "temperature_c": temp,
            "plan": self.plan.to_dict(),
        }
        return self.plan

    async def _loop(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:
                log.exception("resource monitor sample failed")
            await asyncio.sleep(self.interval_s)
