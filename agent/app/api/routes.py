"""Phase 1 local REST API (§24), plus the pieces the setup UI needs:
manual add, preview tickets, recent events, audit trail.

Every route below the router is protected by LocalGuard (origin + token)
except the two preview routes, which use a single-camera ticket instead.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..database.models import iso_utc
from ..health.system import system_snapshot
from ..services.devices import DeviceService, GuardError


class AuthBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=128)


class ManualBody(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    ip_address: str = Field(default="", max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=128)
    connection_type: str = Field(default="auto", pattern="^(auto|onvif|rtsp|hikvision|dahua|v380)$")
    rtsp_url: str | None = Field(default=None, max_length=512)


class TestBody(BaseModel):
    seconds: int | None = Field(default=None, ge=5, le=60)


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


def _svc(request: Request) -> DeviceService:
    return request.app.state.devices


def _protect(request: Request) -> None:
    g = request.app.state.local_guard
    g.check_origin(request)
    g.check_token(request)


def _plain(fn):  # noqa: ANN001, ANN202
    """GuardError → 400 with its message; anything else is logged, not leaked."""

    async def wrapper(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        try:
            return await fn(*a, **kw)
        except GuardError as e:
            raise HTTPException(409 if e.__class__.__name__ == "LimitError" else 400, str(e)) from None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    import inspect

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


api = APIRouter(prefix="/api/v1", dependencies=[Depends(_protect)])
preview = APIRouter(prefix="/api/v1")


# ── discovery ─────────────────────────────────────────────────────────
@api.post("/discovery/start")
async def discovery_start(request: Request, bg: BackgroundTasks) -> dict:
    svc = _svc(request)
    if svc.scanner and svc.scanner.progress.get("stage") not in ("idle", "done"):
        return {"started": False, "status": svc.discovery_status()}
    task = asyncio.create_task(svc.run_discovery())
    request.app.state.discovery_task = task
    await asyncio.sleep(0)
    return {"started": True, "status": svc.discovery_status()}


@api.get("/discovery/status")
async def discovery_status(request: Request) -> dict:
    svc = _svc(request)
    return {"status": svc.discovery_status(), "devices": svc.devices()}


# ── devices ───────────────────────────────────────────────────────────
@api.get("/devices")
async def list_devices(request: Request) -> dict:
    return {"devices": _svc(request).devices()}


@api.post("/devices/manual")
@_plain
async def add_manual(request: Request, body: ManualBody) -> dict:
    return await _svc(request).add_manual(
        name=body.name, ip=body.ip_address.strip(), port=body.port, username=body.username.strip(),
        password=body.password, connection_type=body.connection_type, rtsp_url=body.rtsp_url,
    )


@api.get("/devices/{device_id}")
async def get_device(request: Request, device_id: str) -> dict:
    d = _svc(request).device_dict(device_id)
    if not d:
        raise HTTPException(404, "That device is no longer in the list.")
    return d


@api.post("/devices/{device_id}/authenticate")
@_plain
async def authenticate(request: Request, device_id: str, body: AuthBody) -> dict:
    return await _svc(request).authenticate(device_id, body.username.strip(), body.password)


@api.post("/devices/{device_id}/test")
@_plain
async def test_device(request: Request, device_id: str, body: TestBody | None = None) -> dict:
    svc = _svc(request)
    cams = svc.cameras(device_id)
    if not cams:
        raise GuardError("Connect to this device first.")
    results = [await svc.test_camera(c["id"], body.seconds if body else None) for c in cams if c["online"]]
    return {"device": svc.device_dict(device_id), "results": results}


@api.get("/devices/{device_id}/channels")
async def device_channels(request: Request, device_id: str) -> dict:
    return {"channels": _svc(request).cameras(device_id)}


@api.delete("/devices/{device_id}")
@_plain
async def remove_device(request: Request, device_id: str) -> dict:
    await _svc(request).remove_device(device_id)
    return {"ok": True}


# ── cameras ───────────────────────────────────────────────────────────
@api.get("/cameras")
async def list_cameras(request: Request) -> dict:
    svc = _svc(request)
    return {"cameras": svc.cameras(), "max_guard_cameras": svc.settings.max_guard_cameras}


@api.get("/cameras/{camera_id}")
async def get_camera(request: Request, camera_id: str) -> dict:
    c = _svc(request).camera_dict(camera_id)
    if not c:
        raise HTTPException(404, "That camera is no longer in the list.")
    return c


@api.post("/cameras/{camera_id}/test-stream")
@_plain
async def test_stream(request: Request, camera_id: str, body: TestBody | None = None) -> dict:
    return await _svc(request).test_camera(camera_id, body.seconds if body else None)


@api.post("/cameras/{camera_id}/enable")
@_plain
async def enable(request: Request, camera_id: str) -> dict:
    return await _svc(request).set_guard(camera_id, True)


@api.post("/cameras/{camera_id}/disable")
@_plain
async def disable(request: Request, camera_id: str) -> dict:
    return await _svc(request).set_guard(camera_id, False)


@api.post("/cameras/{camera_id}/rename")
@_plain
async def rename(request: Request, camera_id: str, body: RenameBody) -> dict:
    return _svc(request).rename_camera(camera_id, body.name)


@api.post("/cameras/{camera_id}/preview-ticket")
async def preview_ticket(request: Request, camera_id: str) -> dict:
    if not _svc(request).camera_dict(camera_id):
        raise HTTPException(404, "That camera is no longer in the list.")
    return {"ticket": request.app.state.streams.issue_ticket(camera_id), "expires_in": 120}


# ── system ────────────────────────────────────────────────────────────
@api.get("/system/health")
async def health(request: Request) -> dict:
    svc = _svc(request)
    streams = request.app.state.streams.health()
    guard = svc.guard_camera_ids()
    online = sum(1 for cid in guard if streams.get(cid, {}).get("status") in ("ONLINE", "DEGRADED"))
    return {
        "system": system_snapshot(),
        "guard_cameras": {"online": online, "total": len(guard), "limit": svc.settings.max_guard_cameras},
        "streams": streams,
        "events": list(request.app.state.events)[-50:],
    }


@api.get("/system/audit")
async def audit(request: Request) -> dict:
    rows = request.app.state.db.recent_audit(200)
    return {"audit": [{"action": r.action, "target": r.target, "detail": r.detail, "at": iso_utc(r.at)} for r in rows]}


# ── preview (ticketed) ────────────────────────────────────────────────
def _check_ticket(request: Request, camera_id: str, ticket: str) -> None:
    request.app.state.local_guard.check_origin(request)
    if not request.app.state.streams.check_ticket(ticket, camera_id):
        raise HTTPException(401, "Preview link expired. Refresh the page.")


@preview.get("/cameras/{camera_id}/preview")
async def mjpeg(request: Request, camera_id: str, ticket: str) -> StreamingResponse:
    """RTSP → FFmpeg → MJPEG, so the browser never sees the camera address or password."""
    _check_ticket(request, camera_id, ticket)
    streams = request.app.state.streams
    worker = streams.preview_worker(camera_id)
    if not worker:
        raise HTTPException(404, "This camera has no stream to preview.")

    async def frames():  # noqa: ANN202
        boundary = b"--frame\r\n"
        last = None
        while True:
            if await request.is_disconnected():
                return
            streams.touch(camera_id)
            jpeg = worker.latest_jpeg
            if jpeg is not None and jpeg is not last:
                last = jpeg
                yield boundary + b"Content-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@preview.get("/cameras/{camera_id}/snapshot.jpg")
async def snapshot(request: Request, camera_id: str, ticket: str) -> Response:
    _check_ticket(request, camera_id, ticket)
    streams = request.app.state.streams
    worker = streams.preview_worker(camera_id)
    if not worker:
        raise HTTPException(404, "This camera has no stream to preview.")
    for _ in range(80):  # up to ~8 s for the first frame
        if worker.latest_jpeg:
            break
        await asyncio.sleep(0.1)
    if not worker.latest_jpeg:
        raise HTTPException(504, worker.health.last_error or "No picture from this camera yet.")
    return Response(worker.latest_jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
