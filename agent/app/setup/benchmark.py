"""How many cameras can THIS computer protect? (plug-and-play §"computer capacity").

Instead of selling arbitrary camera counts, Guard measures: it runs its own
person model on real frames for a few seconds and sizes the answer from the
measured inference time, the processor and the memory.

    capacity = min(by processor, by memory), 0…16

By processor: the POS always comes first, so Guard may use HALF the
computer. Each protected camera is analysed about 5 times a second in normal
running (the adaptive policy goes up to 10 when the PC is idle and down to 3
when busy, so 5 is the honest middle).

By memory: ~3 GB stays free for Windows and the POS; each camera needs
~350 MB (video decode, the 15-second rolling buffer, the tracker).

A 2-core or 4 GB computer is capped at 1 camera whatever the model speed:
those machines stall the POS long before the AI is the bottleneck.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
import psutil

CPU_SHARE = 0.5
PER_CAMERA_FPS = 5.0
RAM_RESERVED_GB = 3.0
RAM_PER_CAMERA_GB = 0.35
MAX_CAPACITY = 16


@dataclass
class BenchResult:
    infer_ms: float | None
    samples: int
    capacity: int
    verdict: str              # good | limited | unsuitable | unknown
    message: str
    cpu_count: int
    ram_total_gb: float
    people: dict[str, int] = field(default_factory=dict)  # camera id → most people seen in one frame

    def to_dict(self) -> dict:
        return {"infer_ms": self.infer_ms, "samples": self.samples, "capacity": self.capacity,
                "verdict": self.verdict, "message": self.message, "cpu_count": self.cpu_count,
                "ram_total_gb": self.ram_total_gb, "people": self.people}


def capacity_from(infer_ms: float | None, cpu_count: int, ram_total_gb: float) -> int:
    if not infer_ms or infer_ms <= 0:
        return 0
    by_cpu = int((1000.0 * CPU_SHARE) / (infer_ms * PER_CAMERA_FPS))
    by_ram = int(max(0.0, ram_total_gb - RAM_RESERVED_GB) / RAM_PER_CAMERA_GB)
    cap = max(0, min(by_cpu, by_ram, MAX_CAPACITY))
    if cpu_count < 4 or ram_total_gb < 5:
        cap = min(cap, 1)
    return cap


def verdict_for(capacity: int, model_ok: bool) -> tuple[str, str]:
    if not model_ok:
        return "unknown", "Guard's AI model is missing. Reinstall Boombiz Guard."
    if capacity <= 0:
        return "unsuitable", "This computer is too slow to run Guard AI alongside your POS."
    if capacity == 1:
        return "limited", "This computer can protect 1 camera. A faster computer can protect more."
    return "good", f"This computer can reliably protect {capacity} cameras at the same time."


def _synthetic_frame() -> np.ndarray:
    """A shop-sized noisy frame for when no camera is connected yet.
    The detector's cost doesn't depend on content, only on the input size."""
    rng = np.random.default_rng(7)
    return rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)


def _decode(jpeg: bytes) -> np.ndarray | None:
    f = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    return f if f is not None and f.size else None


async def grab_frames(streams, camera_ids: list[str], wait_s: float = 8.0) -> dict[str, bytes]:  # noqa: ANN001
    """One current JPEG per camera, through the same preview workers the setup
    screen uses (the browser never sees a stream address)."""
    workers = {cid: streams.preview_worker(cid) for cid in camera_ids}
    out: dict[str, bytes] = {}
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and len(out) < len([w for w in workers.values() if w]):
        for cid, w in workers.items():
            if w and cid not in out and w.latest_jpeg:
                out[cid] = w.latest_jpeg
            streams.touch(cid)
        await asyncio.sleep(0.25)
    return out


def _run(detector, frames: list[np.ndarray], seconds: float) -> list[float]:  # noqa: ANN001
    times: list[float] = []
    detector.detect(frames[0])  # warm-up: first call allocates
    end = time.monotonic() + seconds
    i = 0
    while time.monotonic() < end or len(times) < 5:
        t0 = time.perf_counter()
        detector.detect(frames[i % len(frames)])
        times.append((time.perf_counter() - t0) * 1000)
        i += 1
        if len(times) >= 200:
            break
    return times


async def run_benchmark(detector, camera_frames: dict[str, bytes] | None = None, seconds: float = 6.0) -> BenchResult:  # noqa: ANN001
    vm = psutil.virtual_memory()
    cpu = psutil.cpu_count(logical=True) or 1
    ram = round(vm.total / 1024**3, 1)
    if detector is None:
        v, msg = verdict_for(0, False)
        return BenchResult(None, 0, 0, v, msg, cpu, ram)
    decoded = {cid: f for cid, j in (camera_frames or {}).items() if (f := _decode(j)) is not None}
    frames = list(decoded.values()) or [_synthetic_frame()]
    times = await asyncio.to_thread(_run, detector, frames, seconds)
    infer = round(statistics.median(times), 1)
    cap = capacity_from(infer, cpu, ram)
    v, msg = verdict_for(cap, True)
    people = {}
    for cid, f in decoded.items():
        dets = await asyncio.to_thread(detector.detect, f)
        people[cid] = len(dets)
    return BenchResult(infer, len(times), cap, v, msg, cpu, ram, people)
