"""One RollingBuffer per Guard camera, fed by the SAME stream worker the AI
uses (Phase 3 §48). No second connection to the camera or DVR.

A sync loop attaches buffers to guard cameras and re-attaches when a stream
worker is replaced (e.g. after new credentials).
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from ..database.db import Database
from ..database.models import Zone
from ..services.streams import StreamManager
from .rolling_buffer import RollingBuffer

log = logging.getLogger(__name__)


class BufferManager:
    def __init__(self, streams: StreamManager, db: Database, seconds: float = 10.0) -> None:
        self.streams = streams
        self.db = db
        self.seconds = seconds
        self.buffers: dict[str, RollingBuffer] = {}
        self._attached: dict[str, object] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="buffer-sync")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                self.sync()
            except Exception:
                log.exception("buffer sync failed")
            await asyncio.sleep(3)

    def sync(self) -> None:
        live = dict(self.streams.guard)
        for cid in list(self.buffers):
            if cid not in live:
                self._detach(cid)
        for cid, worker in live.items():
            buf = self.buffers.setdefault(cid, RollingBuffer(cid, self.seconds))
            if self._attached.get(cid) is not worker:
                old = self._attached.get(cid)
                if old is not None and buf.push in getattr(old, "subscribers", []):
                    old.subscribers.remove(buf.push)
                worker.subscribers.append(buf.push)
                self._attached[cid] = worker

    def _detach(self, cid: str) -> None:
        buf = self.buffers.pop(cid, None)
        w = self._attached.pop(cid, None)
        if buf and w is not None and buf.push in w.subscribers:
            w.subscribers.remove(buf.push)

    def get(self, camera_id: str) -> RollingBuffer | None:
        return self.buffers.get(camera_id)

    def privacy_polygons(self, camera_id: str) -> list[list[tuple[float, float]]]:
        with self.db.session() as s:
            return [[(p[0], p[1]) for p in json.loads(z.polygon_json)]
                    for z in s.scalars(select(Zone).where(Zone.camera_id == camera_id, Zone.zone_type == "PRIVACY",
                                                          Zone.enabled.is_(True)))]

    def memory_bytes(self) -> int:
        return sum(b.memory_bytes() for b in self.buffers.values())
