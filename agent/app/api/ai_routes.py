"""Phase 2 local API (§45) — AI control, zones, events, tracks, performance,
business hours and per-camera AI switches. Same protection as Phase 1:
loopback + Host/Origin checks + X-Guard-Token on every route."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..database.models import Camera, CameraAiConfig, Schedule, Zone
from ..zones.engine import ZoneType
from ..zones.geometry import PolygonError, validate_polygon
from ..zones.schedule import validate_hhmm
from .routes import _protect

ai_api = APIRouter(prefix="/api/v1", dependencies=[Depends(_protect)])


class ZoneBody(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=60)
    zone_type: ZoneType
    polygon: list[dict] = Field(min_length=3, max_length=32)
    sensitivity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH)$")
    enabled: bool = True


class DayBody(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    opens_at: str | None = None
    closes_at: str | None = None
    closed: bool = False


class ScheduleBody(BaseModel):
    days: list[DayBody] = Field(max_length=7)


class AiConfigBody(BaseModel):
    person: bool = True
    shelf: bool = True
    exit: bool = True
    restricted: bool = True
    after_hours: bool = True
    fire: bool = False
    concealment: bool = True
    priority: str = Field(default="NORMAL", pattern="^(PRIMARY|NORMAL)$")


class FeedbackBody(BaseModel):
    feedback: str = Field(pattern="^(ACCURATE|FALSE_EVENT|UNSURE)$")
    note: str | None = Field(default=None, max_length=500)


def _ai(request: Request):  # noqa: ANN202
    return request.app.state.ai


def _db(request: Request):  # noqa: ANN202
    return request.app.state.db


def _zone_dict(z: Zone) -> dict:
    return {"id": z.id, "camera_id": z.camera_id, "name": z.name, "zone_type": z.zone_type,
            "polygon": [{"x": x, "y": y} for x, y in json.loads(z.polygon_json)],
            "enabled": z.enabled, "sensitivity": z.sensitivity}


# ── AI control ────────────────────────────────────────────────────────
@ai_api.get("/ai/status")
async def ai_status(request: Request) -> dict:
    return _ai(request).status()


@ai_api.post("/ai/start")
async def ai_start(request: Request) -> dict:
    return await _ai(request).start()


@ai_api.post("/ai/stop")
async def ai_stop(request: Request) -> dict:
    return await _ai(request).stop()


@ai_api.get("/ai/debug/{camera_id}")
async def ai_debug(request: Request, camera_id: str) -> dict:
    d = _ai(request).debug(camera_id)
    if d is None:
        raise HTTPException(404, "Guard AI isn't running on this camera. Choose it as a Guard camera first.")
    return d


# ── zones ─────────────────────────────────────────────────────────────
@ai_api.get("/zones")
async def list_zones(request: Request, camera_id: str | None = None) -> dict:
    with _db(request).session() as s:
        q = select(Zone).order_by(Zone.created_at)
        if camera_id:
            q = q.where(Zone.camera_id == camera_id)
        return {"zones": [_zone_dict(z) for z in s.scalars(q)]}


def _validated(body: ZoneBody) -> str:
    try:
        return json.dumps(validate_polygon(body.polygon))
    except PolygonError as e:
        raise HTTPException(400, str(e)) from None


@ai_api.post("/zones")
async def create_zone(request: Request, body: ZoneBody) -> dict:
    poly = _validated(body)
    db = _db(request)
    with db.session() as s:
        if not s.get(Camera, body.camera_id):
            raise HTTPException(404, "That camera is no longer in the list.")
        z = Zone(camera_id=body.camera_id, name=body.name.strip(), zone_type=body.zone_type.value,
                 polygon_json=poly, sensitivity=body.sensitivity, enabled=body.enabled)
        s.add(z)
        s.flush()
        out = _zone_dict(z)
    db.audit("zone_created", out["id"], camera=body.camera_id, zone_type=body.zone_type.value)
    _ai(request).reload_camera(body.camera_id)
    return out


@ai_api.put("/zones/{zone_id}")
async def update_zone(request: Request, zone_id: str, body: ZoneBody) -> dict:
    poly = _validated(body)
    db = _db(request)
    with db.session() as s:
        z = s.get(Zone, zone_id)
        if not z or z.camera_id != body.camera_id:
            raise HTTPException(404, "That zone no longer exists.")
        z.name, z.zone_type, z.polygon_json = body.name.strip(), body.zone_type.value, poly
        z.sensitivity, z.enabled = body.sensitivity, body.enabled
        out = _zone_dict(z)
    db.audit("zone_updated", zone_id)
    _ai(request).reload_camera(body.camera_id)
    return out


@ai_api.delete("/zones/{zone_id}")
async def delete_zone(request: Request, zone_id: str) -> dict:
    db = _db(request)
    with db.session() as s:
        z = s.get(Zone, zone_id)
        if not z:
            raise HTTPException(404, "That zone no longer exists.")
        cam = z.camera_id
        s.delete(z)
    db.audit("zone_deleted", zone_id)
    _ai(request).reload_camera(cam)
    return {"ok": True}


# ── events ────────────────────────────────────────────────────────────
@ai_api.get("/events")
async def list_events(request: Request, camera_id: str | None = None, event_type: str | None = None,
                      security_only: bool = False, limit: int = 200) -> dict:
    return {"events": request.app.state.ai_events.list(camera_id=camera_id, event_type=event_type,
                                                        security_only=security_only, limit=limit)}


@ai_api.get("/events/live")
async def live_events(request: Request) -> dict:
    return {"events": list(request.app.state.ai_events.feed)[-100:]}


@ai_api.get("/events/{event_id}")
async def get_event(request: Request, event_id: str) -> dict:
    e = request.app.state.ai_events.get(event_id)
    if not e:
        raise HTTPException(404, "That event no longer exists.")
    return e


@ai_api.post("/events/{event_id}/feedback")
async def event_feedback(request: Request, event_id: str, body: FeedbackBody) -> dict:
    e = request.app.state.ai_events.set_feedback(event_id, body.feedback, body.note)
    if not e:
        raise HTTPException(404, "That event no longer exists.")
    return e


# ── tracks / performance ──────────────────────────────────────────────
@ai_api.get("/tracks/active")
async def active_tracks(request: Request) -> dict:
    return {"tracks": _ai(request).active_tracks()}


@ai_api.get("/performance")
async def performance(request: Request) -> dict:
    ai = _ai(request)
    return {"resources": ai.monitor.snapshot, "plan": ai.monitor.plan.to_dict(),
            "cameras": [w.status() for w in ai.workers.values()]}


@ai_api.post("/performance/simulate-cpu")
async def simulate_cpu(request: Request, cpu: float | None = None) -> dict:
    """Dev/test only (GUARD_DEV=1): pretend the CPU is this busy."""
    if not os.environ.get("GUARD_DEV"):
        raise HTTPException(404, "Not found.")
    ai = _ai(request)
    ai.monitor.forced_cpu = cpu
    for _ in range(6):
        ai.monitor.sample()
    return ai.monitor.plan.to_dict()


# ── business hours ────────────────────────────────────────────────────
@ai_api.get("/schedules")
async def get_schedule(request: Request) -> dict:
    with _db(request).session() as s:
        rows = s.scalars(select(Schedule).order_by(Schedule.day_of_week)).all()
        return {"days": [{"day_of_week": r.day_of_week, "opens_at": r.opens_at, "closes_at": r.closes_at,
                          "closed": r.closed} for r in rows]}


@ai_api.put("/schedules")
async def put_schedule(request: Request, body: ScheduleBody) -> dict:
    if len({d.day_of_week for d in body.days}) != len(body.days):
        raise HTTPException(400, "Each day can only appear once.")
    try:
        days = [(d.day_of_week, validate_hhmm(d.opens_at), validate_hhmm(d.closes_at), d.closed) for d in body.days]
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    db = _db(request)
    with db.session() as s:
        for r in s.scalars(select(Schedule)).all():
            s.delete(r)
        for dow, o, c, closed in days:
            if not closed and (not o or not c):
                raise HTTPException(400, "Give an opening and closing time, or mark the day closed.")
            s.add(Schedule(day_of_week=dow, opens_at=o, closes_at=c, closed=closed))
    db.audit("configuration_changed", None, field="business_hours")
    _ai(request).reload_schedule()
    return await get_schedule(request)


# ── per-camera AI switches ────────────────────────────────────────────
@ai_api.get("/cameras/{camera_id}/ai-config")
async def get_ai_config(request: Request, camera_id: str) -> dict:
    with _db(request).session() as s:
        if not s.get(Camera, camera_id):
            raise HTTPException(404, "That camera is no longer in the list.")
        row = s.get(CameraAiConfig, camera_id) or CameraAiConfig(camera_id=camera_id)
        return {k: getattr(row, k) for k in AiConfigBody.model_fields} | {
            "fire_notice": "Visual AI warning only. Not a replacement for certified fire detection systems.",
            "concealment_notice": "Experimental. Always low confidence: a hand to a pocket is also how people reach for a phone.",
        }


@ai_api.put("/cameras/{camera_id}/ai-config")
async def put_ai_config(request: Request, camera_id: str, body: AiConfigBody) -> dict:
    db = _db(request)
    with db.session() as s:
        if not s.get(Camera, camera_id):
            raise HTTPException(404, "That camera is no longer in the list.")
        row = s.get(CameraAiConfig, camera_id)
        if row is None:
            row = CameraAiConfig(camera_id=camera_id)
            s.add(row)
        for k, v in body.model_dump().items():
            setattr(row, k, v)
    db.audit("configuration_changed", camera_id, field="ai_config")
    _ai(request).reload_camera(camera_id)
    return await get_ai_config(request, camera_id)
