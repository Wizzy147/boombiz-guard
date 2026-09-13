"""Boombiz Guard Agent — entrypoint.

    python -m app.main            (from agent/, venv active)

On start it restarts a stream worker for every guard-enabled camera, so a
rebooted PC (or a restarted Windows Service) is back to watching without
anybody opening the setup UI.
"""

from __future__ import annotations

import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import api, preview
from .api.security import LocalGuard, load_or_create_token
from .config import Settings, settings as default_settings
from .database.db import Database
from .logging_setup import setup_logging
from .security.vault import CredentialVault
from .services.devices import DeviceService
from .services.streams import StreamManager

log = logging.getLogger("guard")

UI_DIST = Path(__file__).resolve().parents[2] / "desktop-ui" / "dist"
VERSION = "0.1.0-phase1"


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
    token = load_or_create_token(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        streams.start()
        for cid in devices.guard_camera_ids():
            await streams.start_guard(cid)
        db.audit("agent_started", None, version=VERSION)
        log.info("Boombiz Guard %s ready — open http://127.0.0.1:%s/#t=<token from %s>",
                 VERSION, settings.port, settings.data_dir / "setup-token")
        yield
        await streams.shutdown()

    app = FastAPI(title="Boombiz Guard Agent", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.devices = devices
    app.state.streams = streams
    app.state.events = events
    app.state.local_guard = LocalGuard(settings, token)
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
