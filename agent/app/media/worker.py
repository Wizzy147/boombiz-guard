"""Background media jobs (Phase 3 §50–52, §67–68, §83).

The AI thread only QUEUES work; this worker does the encoding, one job at a
time, in a thread, at below-normal priority:

    SNAPSHOT  run ~1.5 s after the trigger (so frames just after the event
              moment are candidates too) — priority 2 fire / 4 other
    CLIP      run once trigger + 10 s has passed — priority 1 fire / 5 other

A failed job retries up to 3 times. After a crash, RUNNING jobs go back to
PENDING on start; if the in-memory capture didn't survive, the job ends
"video unavailable" and the incident keeps its metadata (and its snapshot,
if that was already saved) — an incident is never discarded because its
media failed (§52).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from ..buffer.rolling_buffer import Capture
from ..database.db import Database
from ..database.models import Incident, MediaJob
from .clip import ClipError, build_clip
from .encryption import MediaCrypto
from .snapshot import mask_privacy, pick_snapshot, thumbnail

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


class MediaWorker:
    def __init__(self, db: Database, crypto: MediaCrypto, incidents_dir: Path,
                 privacy_for: Callable[[str], list]) -> None:
        self.db = db
        self.crypto = crypto
        self.dir = incidents_dir
        self.privacy_for = privacy_for
        self.captures: dict[str, Capture] = {}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ── queue ────────────────────────────────────────────────────────
    def enqueue(self, incident_id: str, job_type: str, priority: int, delay_s: float) -> None:
        with self.db.session() as s:
            s.add(MediaJob(incident_id=incident_id, job_type=job_type, priority=priority,
                           run_after=_now() + timedelta(seconds=delay_s)))
        try:
            self._wake.set()
        except RuntimeError:
            pass

    def recover(self) -> int:
        with self.db.session() as s:
            stuck = list(s.scalars(select(MediaJob).where(MediaJob.status == "RUNNING")))
            for j in stuck:
                j.status = "PENDING"
        if stuck:
            log.info("media jobs recovered after restart: %s", len(stuck))
        return len(stuck)

    def start(self) -> None:
        self.recover()
        self._task = asyncio.create_task(self._loop(), name="media-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            job_id = self._next()
            if job_id is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                continue
            try:
                await asyncio.to_thread(self.run_job, job_id)
            except Exception:
                log.exception("media job crashed")

    def _next(self) -> str | None:
        with self.db.session() as s:
            q = (select(MediaJob).where(MediaJob.status == "PENDING")
                 .order_by(MediaJob.priority, MediaJob.created_at))
            for j in s.scalars(q):
                if _aware(j.run_after) is None or _aware(j.run_after) <= _now():
                    j.status, j.attempts = "RUNNING", j.attempts + 1
                    return j.id
        return None

    def run_pending_now(self) -> None:
        """Synchronous drain (tests / lab harness)."""
        while (jid := self._next()) is not None:
            self.run_job(jid)

    # ── jobs ─────────────────────────────────────────────────────────
    def run_job(self, job_id: str) -> None:
        with self.db.session() as s:
            j = s.get(MediaJob, job_id)
            inc = s.get(Incident, j.incident_id) if j else None
            if not j or not inc:
                return
            jtype, attempts, ref, iid, cam = j.job_type, j.attempts, inc.ref, inc.id, inc.camera_id
            folder = inc.occurred_at.strftime("%Y/%m/%d") + f"/{ref}"
        cap = self.captures.get(iid)
        try:
            if cap is None:
                raise _Unrecoverable("The video buffer for this incident was lost (Guard restarted).")
            if jtype == "SNAPSHOT":
                self._snapshot(iid, ref, cam, cap, folder)
            elif jtype == "CLIP":
                self._clip(iid, ref, cam, cap, folder)
            self._finish(job_id, "DONE", None)
        except _Unrecoverable as e:
            self._finish(job_id, "FAILED", str(e))
            self._media_failed(iid, jtype)
        except Exception as e:  # ClipError and anything unexpected
            msg = str(e) if isinstance(e, ClipError) else "Media could not be saved."
            log.warning("media job %s %s attempt %s failed: %s", jtype, ref, attempts, e)
            if attempts >= MAX_ATTEMPTS:
                self._finish(job_id, "FAILED", msg)
                self._media_failed(iid, jtype)
            else:
                self._finish(job_id, "PENDING", msg, retry_in=2.0 * attempts)
        self._release(iid)

    def _finish(self, job_id: str, status: str, error: str | None, retry_in: float = 0) -> None:
        with self.db.session() as s:
            j = s.get(MediaJob, job_id)
            if j:
                j.status, j.last_error = status, error
                if retry_in:
                    j.run_after = _now() + timedelta(seconds=retry_in)

    def _snapshot(self, iid: str, ref: str, cam: str, cap: Capture, folder: str) -> None:
        frames = [f for f in cap.frames if abs(f.ts - cap.trigger_ts) <= 1.5] or cap.frames
        best = pick_snapshot(frames, cap.trigger_ts)
        if best is None:
            raise ClipError("No usable picture around the event moment.")
        jpeg = mask_privacy(best.jpeg, self.privacy_for(cam))
        aad = ref.encode()
        n = self.crypto.write(self.dir / folder / "snapshot.jpg.enc", jpeg, aad)
        n += self.crypto.write(self.dir / folder / "thumb.jpg.enc", thumbnail(jpeg), aad)
        with self.db.session() as s:
            inc = s.get(Incident, iid)
            inc.snapshot_path = f"{folder}/snapshot.jpg.enc"
            inc.media_bytes = (inc.media_bytes or 0) + n
            if inc.media_status == "PENDING":
                inc.media_status = "PARTIAL"

    def _clip(self, iid: str, ref: str, cam: str, cap: Capture, folder: str) -> None:
        if not cap.closed and cap.frames and cap.frames[-1].ts < cap.end_ts and not cap.overdue():
            # Post-event frames still arriving (slow camera): try again shortly.
            raise ClipError("Still recording the seconds after the event.")
        # Closed, or overdue because the camera stopped sending: build from
        # what was captured — a shorter clip beats no clip.
        frames = cap.clipped()
        polys = self.privacy_for(cam)
        if polys:
            frames = [type(f)(f.ts, mask_privacy(f.jpeg, polys)) for f in frames]
        mp4, duration = build_clip(frames)
        assert duration <= 15.0  # hard product rule (§17) — build_clip also caps with -t
        n = self.crypto.write(self.dir / folder / "clip.mp4.enc", mp4, ref.encode())
        with self.db.session() as s:
            inc = s.get(Incident, iid)
            inc.clip_path = f"{folder}/clip.mp4.enc"
            inc.clip_duration_seconds = duration
            inc.media_bytes = (inc.media_bytes or 0) + n
            inc.media_status = "READY" if inc.snapshot_path else "PARTIAL"

    def _media_failed(self, iid: str, jtype: str) -> None:
        with self.db.session() as s:
            inc = s.get(Incident, iid)
            if inc:
                inc.media_status = "PARTIAL" if (inc.snapshot_path or inc.clip_path) else "UNAVAILABLE"

    def _release(self, iid: str) -> None:
        """Drop the in-memory capture once no job still needs it."""
        with self.db.session() as s:
            open_jobs = s.scalar(select(MediaJob.id).where(MediaJob.incident_id == iid,
                                                           MediaJob.status.in_(("PENDING", "RUNNING"))))
        if open_jobs is None:
            self.captures.pop(iid, None)

    # ── reading media back (decrypt in memory) ───────────────────────
    def read(self, rel: str | None, ref: str) -> bytes | None:
        if not rel:
            return None
        p = (self.dir / rel).resolve()
        if self.dir.resolve() not in p.parents or not p.exists():
            return None
        return self.crypto.read(p, ref.encode())

    def write_metadata(self, ref: str, occurred_at: datetime, data: dict) -> int:
        folder = occurred_at.strftime("%Y/%m/%d") + f"/{ref}"
        return self.crypto.write(self.dir / folder / "metadata.json.enc",
                                 json.dumps(data, default=str, indent=2).encode(), ref.encode())


class _Unrecoverable(Exception):
    pass
