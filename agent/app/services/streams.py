"""Owns every FFmpeg stream worker.

Two kinds:
  · Guard workers — one per guard-enabled camera, always running, restarted
    on boot (that is the "reboot, Guard reconnects" half of the Definition of
    Done). Phase 2 attaches the AI to these.
  · Preview workers — started on demand when the setup UI opens a preview,
    stopped 30 s after the last viewer leaves. A camera that already has a
    Guard worker previews from it instead of opening a second stream.

Previews are served to the browser as MJPEG from the agent, so the browser
never sees an RTSP URL, let alone a password (Phase 1 §17). Access to a
preview is a single-camera ticket that expires in two minutes — an <img> tag
can't send the API token header, and the long-lived token must not go in a URL.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Awaitable, Callable

from ..streams.worker import StreamWorker

PREVIEW_IDLE_S = 30
TICKET_TTL_S = 120

UrlResolver = Callable[[str], str | None]
EventSink = Callable[[str, str, dict], Awaitable[None]]


class StreamManager:
    def __init__(self, resolve_url: UrlResolver, on_event: EventSink | None = None) -> None:
        self._resolve = resolve_url
        self._on_event = on_event
        self.guard: dict[str, StreamWorker] = {}
        self._previews: dict[str, tuple[StreamWorker, float]] = {}
        self._tickets: dict[str, tuple[str, float]] = {}
        self._reaper: asyncio.Task | None = None

    def start(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop(), name="preview-reaper")

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for w in list(self.guard.values()) + [w for w, _ in self._previews.values()]:
            await w.stop()
        self.guard.clear()
        self._previews.clear()

    # ── Guard workers ───────────────────────────────────────────────
    async def start_guard(self, camera_id: str) -> bool:
        if camera_id in self.guard:
            return True
        url = self._resolve(camera_id)
        if not url:
            return False
        preview = self._previews.pop(camera_id, None)
        if preview:
            await preview[0].stop()
        w = StreamWorker(camera_id, url, on_event=self._on_event)
        self.guard[camera_id] = w
        w.start()
        return True

    async def stop_guard(self, camera_id: str) -> None:
        w = self.guard.pop(camera_id, None)
        if w:
            await w.stop()

    async def restart_guard(self, camera_id: str) -> None:
        """After credentials change: the running worker holds the old URL."""
        if camera_id in self.guard:
            await self.stop_guard(camera_id)
            await self.start_guard(camera_id)

    # ── Previews ────────────────────────────────────────────────────
    def preview_worker(self, camera_id: str) -> StreamWorker | None:
        if camera_id in self.guard:
            return self.guard[camera_id]
        entry = self._previews.get(camera_id)
        if entry:
            self._previews[camera_id] = (entry[0], time.monotonic())
            return entry[0]
        url = self._resolve(camera_id)
        if not url:
            return None
        w = StreamWorker(camera_id, url, out_fps=4)
        w.start()
        self._previews[camera_id] = (w, time.monotonic())
        return w

    def touch(self, camera_id: str) -> None:
        entry = self._previews.get(camera_id)
        if entry:
            self._previews[camera_id] = (entry[0], time.monotonic())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for cam, (w, last) in list(self._previews.items()):
                if now - last > PREVIEW_IDLE_S:
                    self._previews.pop(cam, None)
                    await w.stop()
            for t, (_, exp) in list(self._tickets.items()):
                if exp < now:
                    self._tickets.pop(t, None)

    # ── Tickets ─────────────────────────────────────────────────────
    def issue_ticket(self, camera_id: str) -> str:
        t = secrets.token_urlsafe(24)
        self._tickets[t] = (camera_id, time.monotonic() + TICKET_TTL_S)
        return t

    def check_ticket(self, ticket: str, camera_id: str) -> bool:
        entry = self._tickets.get(ticket)
        return bool(entry and entry[0] == camera_id and entry[1] > time.monotonic())

    # ── Health ──────────────────────────────────────────────────────
    def health(self) -> dict[str, dict]:
        out = {cid: {**w.health.to_dict(), "role": "guard"} for cid, w in self.guard.items()}
        for cid, (w, _) in self._previews.items():
            out.setdefault(cid, {**w.health.to_dict(), "role": "preview"})
        return out
