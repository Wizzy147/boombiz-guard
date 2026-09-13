"""Boombiz Guard Agent — entrypoint.

    python -m app.main            (from agent/, venv active)

On start it restarts a stream worker for every guard-enabled camera, so a
rebooted PC (or a restarted Windows Service) is back to watching without
anybody opening the setup UI.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .ai.service import AIService
from .alarms.service import AlarmService
from .api.ai_routes import ai_api
from .api.incident_routes import inc_api, media_api
from .buffer.segment_manager import BufferManager
from .incidents.service import IncidentService
from .media.encryption import MediaCrypto
from .media.worker import MediaWorker
from .retention.service import RetentionService
from .review.auth import LocalAuth
from .api.routes import api, preview
from .events.service import EventService
from .api.security import LocalGuard, load_or_create_token
from .config import Settings, settings as default_settings
from .database.db import Database
from .logging_setup import setup_logging
from .security.vault import CredentialVault
from .services.devices import DeviceService
from .services.streams import StreamManager

log = logging.getLogger("guard")

UI_DIST = Path(__file__).resolve().parents[2] / "desktop-ui" / "dist"
VERSION = "0.3.0-phase3"


def create_app(settings: Settings | None = None, *, db_path: str | None = None, cipher=None) -> FastAPI:  # noqa: ANN001
    settings = settings or default_settings
    db = Database(db_path or settings.db_path)
    vault = CredentialVault(db, cipher)
    events: deque[dict] = deque(maxlen=500)

    async def on_event(event: str, camera_id: str, data: dict) -> None:
        entry = {"event": event, "camera_id": camera_id, "at": datetime.now(timezone.utc).isoformat(), **data}
        events.append(entry)
        db.audit(event, camera_id, **data)
        log.info("event %s camera=%s", event, camera_id)

    holder: dict = {}
    streams = StreamManager(lambda cid: holder["devices"].stream_url(cid), on_event)
    devices = DeviceService(db, vault, streams, settings)
    holder["devices"] = devices
    ai_events = EventService(db)
    ai = AIService(db, streams, ai_events, settings)
    token = load_or_create_token(settings)

    # ── Phase 3: incidents, evidence, alarms, retention, people ─────────
    incidents_dir = settings.data_dir / "incidents"
    crypto = MediaCrypto(settings.data_dir / "media.key", cipher)
    buffers = BufferManager(streams, db)
    media_worker = MediaWorker(db, crypto, incidents_dir, buffers.privacy_polygons)
    retention = RetentionService(db, incidents_dir)
    alarms = AlarmService(db, vault)
    auth = LocalAuth(db)
    incidents = IncidentService(db, buffers, media_worker, alarms, retention, streams=streams)
    ai_events.listeners.append(incidents.on_ai_event)

    async def periodic(name: str, every_s: float, fn, first_delay: float = 5.0) -> None:  # noqa: ANN001
        await asyncio.sleep(first_delay)
        while True:
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("%s failed", name)
            await asyncio.sleep(every_s)

    def integrity() -> None:
        if not db.integrity_ok():
            db.audit("database_integrity_failed", None)
            log.error("database integrity check FAILED")

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        streams.start()
        for cid in devices.guard_camera_ids():
            await streams.start_guard(cid)
        # Local AI starts with the agent: a rebooted PC is protected again
        # without anyone opening the setup UI. A model problem is reported in
        # /ai/status, never a crash.
        await ai.start()
        # §67 restart recovery: DB is open, media folder exists, unfinished
        # media jobs go back on the queue, buffers re-attach to streams.
        incidents_dir.mkdir(parents=True, exist_ok=True)
        buffers.sync()
        buffers.start()
        media_worker.start()
        incidents.start()
        background = [
            asyncio.create_task(periodic("retention", 6 * 3600, retention.cleanup, 30)),
            asyncio.create_task(periodic("alarm health", 60, alarms.check_health, 10)),
            asyncio.create_task(periodic("db integrity", 24 * 3600, integrity, 60)),
        ]
        db.audit("agent_started", None, version=VERSION)
        log.info("Boombiz Guard %s ready — open http://127.0.0.1:%s/#t=<token from %s>",
                 VERSION, settings.port, settings.data_dir / "setup-token")
        yield
        for t in background:
            t.cancel()
        await incidents.stop()
        await media_worker.stop()
        await buffers.stop()
        await ai.stop()
        await streams.shutdown()

    app = FastAPI(title="Boombiz Guard Agent", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.devices = devices
    app.state.streams = streams
    app.state.events = events
    app.state.local_guard = LocalGuard(settings, token)
    app.state.ai = ai
    app.state.ai_events = ai_events
    app.state.incidents = incidents
    app.state.alarms = alarms
    app.state.retention = retention
    app.state.auth = auth
    app.state.buffers = buffers
    app.state.media = media_worker
    app.include_router(inc_api)
    app.include_router(media_api)
    app.include_router(ai_api)
    app.include_router(api)
    app.include_router(preview)

    @app.get("/api/ping")
    async def ping() -> dict:
        return {"ok": True, "version": VERSION}

    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    else:
        @app.get("/")
        async def no_ui() -> HTMLResponse:
            return HTMLResponse("<p>Boombiz Guard agent is running. The setup UI has not been built "
                                "(cd desktop-ui &amp;&amp; npm run build).</p>")

    return app


def run() -> None:
    import uvicorn

    setup_logging()
    app = create_app()
    uvicorn.run(app, host=default_settings.bind_host, port=default_settings.port, log_config=None, access_log=False)


if __name__ == "__main__":
    run()
