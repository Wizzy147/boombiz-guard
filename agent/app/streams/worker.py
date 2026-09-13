"""A long-running stream worker: one FFmpeg process per camera, restarted with
capped backoff when it dies (Phase 1 §18), exposing the latest JPEG frame for
preview and the health numbers from Phase 1 §19.

FFmpeg decodes the stream and re-encodes a small, low-rate MJPEG to stdout;
this reads JPEG frames off the pipe by their SOI/EOI markers. Phase 2 will
tap the same process for the AI frames — the transport is already here.

Reconnect ladder: 2 s → 5 s → 10 s → 30 s, then 30 s forever. An auth error
(401) STOPS the worker instead of retrying: the saved credentials are wrong
now, and hammering a recorder with them is how its account gets locked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Awaitable, Callable

from ..config import settings
from ..security.redact import redact

log = logging.getLogger(__name__)

BACKOFF = (2, 5, 10, 30)
SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class CameraState(StrEnum):
    STARTING = "STARTING"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    AUTH_ERROR = "AUTH_ERROR"
    STREAM_ERROR = "STREAM_ERROR"
    STOPPED = "STOPPED"


@dataclass
class Health:
    status: CameraState = CameraState.STARTING
    last_frame_at: datetime | None = None
    stream_fps: float = 0.0
    decode_fps: float = 0.0
    reconnect_count: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "stream_fps": round(self.stream_fps, 1),
            "decode_fps": round(self.decode_fps, 1),
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }


EventSink = Callable[[str, str, dict], Awaitable[None]]


class StreamWorker:
    def __init__(
        self,
        camera_id: str,
        url_with_creds: str,
        *,
        out_fps: float = 5.0,
        width: int = 640,
        on_event: EventSink | None = None,
        offline_after_s: float = 15.0,
    ) -> None:
        self.camera_id = camera_id
        self._url = url_with_creds
        self.out_fps = out_fps
        self.width = width
        self.health = Health()
        self.latest_jpeg: bytes | None = None
        self._frame_times: deque[float] = deque(maxlen=50)
        self._on_event = on_event
        self._offline_after = offline_after_s
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._was_online = False
        self.frame_event = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        if not self._task or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name=f"stream:{self.camera_id}")

    async def stop(self) -> None:
        self._stopping = True
        await self._kill()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.health.status = CameraState.STOPPED

    async def _kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                pass

    async def _emit(self, event: str, **data: object) -> None:
        if self._on_event:
            try:
                await self._on_event(event, self.camera_id, dict(data))
            except Exception:  # an event sink must never kill the stream
                log.exception("event sink failed")

    # ── main loop ────────────────────────────────────────────────────
    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            started = time.monotonic()
            outcome = await self._run_once()
            if self._stopping:
                break
            if outcome == "auth":
                self.health.status = CameraState.AUTH_ERROR
                self.health.last_error = "The saved CCTV password was rejected. Re-enter it in Guard setup."
                await self._emit("CAMERA_AUTH_ERROR")
                return
            # A run that stayed up for a while resets the ladder.
            if time.monotonic() - started > 60:
                attempt = 0
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            self.health.reconnect_count += 1
            if self._was_online:
                self._was_online = False
                self.health.status = CameraState.OFFLINE
                await self._emit("CAMERA_OFFLINE", error=self.health.last_error)
            elif self.health.status not in (CameraState.STREAM_ERROR, CameraState.OFFLINE):
                self.health.status = CameraState.OFFLINE
            log.info("camera %s reconnecting in %ss (attempt %s)", self.camera_id, delay, attempt)
            await asyncio.sleep(delay)

    async def _run_once(self) -> str:
        args = [
            settings.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", self._url,
            "-an", "-vf", f"fps={self.out_fps},scale={self.width}:-2",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "7", "pipe:1",
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            self.health.status = CameraState.STREAM_ERROR
            self.health.last_error = "FFmpeg is missing from this Guard installation."
            return "error"

        stderr_tail: deque[str] = deque(maxlen=20)

        async def drain_stderr() -> None:
            assert self._proc and self._proc.stderr
            async for raw in self._proc.stderr:
                stderr_tail.append(redact(raw.decode(errors="replace").strip()))

        err_task = asyncio.create_task(drain_stderr())
        watchdog = asyncio.create_task(self._watchdog())
        try:
            await self._read_frames()
            await self._proc.wait()
        finally:
            watchdog.cancel()
            await self._kill()
            err_task.cancel()

        tail = " ".join(stderr_tail)
        if "401" in tail or "Unauthorized" in tail:
            return "auth"
        if tail:
            self.health.last_error = tail[-300:]
        if not self._was_online:
            self.health.status = CameraState.STREAM_ERROR if tail else CameraState.OFFLINE
        return "error"

    async def _read_frames(self) -> None:
        assert self._proc and self._proc.stdout
        buf = bytearray()
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(SOI)
                end = buf.find(EOI, start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    if start > 0:
                        del buf[:start]
                    break
                await self._on_frame(bytes(buf[start:end + 2]))
                del buf[:end + 2]

    async def _on_frame(self, jpeg: bytes) -> None:
        now = time.monotonic()
        self.latest_jpeg = jpeg
        self._frame_times.append(now)
        self.health.last_frame_at = datetime.now(timezone.utc)
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            fps = (len(self._frame_times) - 1) / span if span > 0 else 0.0
            self.health.decode_fps = self.health.stream_fps = fps
        degraded = self.health.decode_fps and self.health.decode_fps < self.out_fps * 0.5 and len(self._frame_times) > 10
        self.health.status = CameraState.DEGRADED if degraded else CameraState.ONLINE
        if not self._was_online:
            self._was_online = True
            self.health.last_error = None
            await self._emit("CAMERA_RECONNECTED" if self.health.reconnect_count else "CAMERA_ONLINE")
        self.frame_event.set()
        self.frame_event.clear()

    async def _watchdog(self) -> None:
        """A stream that stays connected but stops sending frames is offline too."""
        while True:
            await asyncio.sleep(2)
            last = self._frame_times[-1] if self._frame_times else None
            if self._was_online and last and time.monotonic() - last > self._offline_after:
                self.health.last_error = "No video received for %ds." % self._offline_after
                await self._kill()
                return
