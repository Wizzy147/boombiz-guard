"""Phase 3 local API (§57): incidents, review, media, alarms, storage,
retention, people & PIN sign-in, Guard Mode.

Two layers of access:
  · X-Guard-Token (device) — the request comes from Guard's own screen on
    this PC. Required everywhere, as in Phases 1–2.
  · X-Guard-Session (person) — a PIN sign-in. Review decisions need one, so
    every confirm / false alert / delete names a real person (§35–37).
    Without a session the caller is the INSTALLER: can view and configure
    alarms, can't review.

Snapshots and clips go to <img>/<video>, which can't send headers, so they
use a one-time media ticket (60 s, one incident, one kind). Files are
decrypted in memory; the browser never sees a path.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..alarms.adapters import AlarmError
from ..incidents.lifecycle import FALSE_ALERT_REASONS, TransitionError
from ..incidents.service import IncidentError
from ..review.auth import AuthError, Session
from ..review.permissions import INSTALLER, Forbidden, require
from .routes import _protect

inc_api = APIRouter(prefix="/api/v1", dependencies=[Depends(_protect)])
media_api = APIRouter(prefix="/api/v1")

_tickets: dict[str, tuple[str, str, float]] = {}  # ticket → (incident, kind, expires)


def _svc(r: Request):  # noqa: ANN202
    return r.app.state.incidents


def _session(r: Request) -> Session | None:
    return r.app.state.auth.session(r.headers.get("x-guard-session"))


def _person(r: Request) -> Session:
    s = _session(r)
    if s is None:
        raise HTTPException(401, "Sign in with your PIN first.")
    return s


def _role(r: Request) -> str:
    s = _session(r)
    return s.role if s else INSTALLER


def _errors(fn):  # noqa: ANN001, ANN202
    import functools
    import inspect

    @functools.wraps(fn)
    async def w(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        try:
            res = fn(*a, **kw)
            return await res if inspect.isawaitable(res) else res
        except Forbidden as e:
            raise HTTPException(403, str(e)) from None
        except (IncidentError, TransitionError, AuthError, AlarmError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
    return w


# ── people & sign-in ──────────────────────────────────────────────────
class SignIn(BaseModel):
    user_id: str = Field(max_length=64)
    pin: str = Field(min_length=4, max_length=6)


class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(pattern="^(OWNER|MANAGER|SECURITY)$")
    pin: str = Field(min_length=4, max_length=6)


@inc_api.get("/auth/users")
async def people(r: Request) -> dict:
    """Names + roles for the PIN pad. No PINs, obviously."""
    return {"users": [u for u in r.app.state.auth.users() if u["active"]], "has_owner": r.app.state.auth.has_owner()}


@inc_api.post("/auth/signin")
@_errors
async def sign_in(r: Request, body: SignIn) -> dict:
    s = r.app.state.auth.sign_in(body.user_id, body.pin)
    return {"session": s.token, "user": {"id": s.user_id, "name": s.name, "role": s.role}}


@inc_api.post("/auth/signout")
async def sign_out(r: Request) -> dict:
    r.app.state.auth.sign_out(r.headers.get("x-guard-session", ""))
    return {"ok": True}


@inc_api.get("/auth/me")
async def me(r: Request) -> dict:
    s = _session(r)
    return {"user": {"id": s.user_id, "name": s.name, "role": s.role} if s else None, "role": _role(r)}


@inc_api.post("/users")
@_errors
async def add_user(r: Request, body: NewUser) -> dict:
    auth = r.app.state.auth
    if auth.has_owner():
        require(_person(r).role, "manage_users")
    elif body.role != "OWNER":
        raise IncidentError("Add the shop owner first.")
    return auth.create_user(body.name, body.role, body.pin)


@inc_api.post("/users/{user_id}/active")
@_errors
async def set_active(r: Request, user_id: str, active: bool) -> dict:
    require(_person(r).role, "manage_users")
    r.app.state.auth.set_active(user_id, active)
    return {"ok": True}


# ── incidents ─────────────────────────────────────────────────────────
def _range(period: str | None, start: str | None, end: str | None) -> tuple[datetime | None, datetime | None]:
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today, None
    if period == "yesterday":
        return today - timedelta(days=1), today
    if period == "7d":
        return today - timedelta(days=7), None
    try:
        return (datetime.fromisoformat(start) if start else None, datetime.fromisoformat(end) if end else None)
    except ValueError:
        raise HTTPException(400, "Dates look like 2026-09-13.") from None


@inc_api.get("/incidents")
async def list_incidents(r: Request, period: str | None = None, start: str | None = None, end: str | None = None,
                         severity: str | None = None, incident_type: str | None = None, camera_id: str | None = None,
                         status: str | None = None, kept: bool | None = None, q: str | None = None,
                         limit: int = 200) -> dict:
    since, until = _range(period, start, end)
    return {"incidents": _svc(r).list(since=since, until=until, severity=severity, incident_type=incident_type,
                                      camera_id=camera_id, status=status, kept=kept, search=q, limit=limit),
            "false_alert_reasons": FALSE_ALERT_REASONS}


@inc_api.get("/incidents/overview")
async def overview(r: Request) -> dict:
    ai = r.app.state.ai.status()
    return {**_svc(r).overview(), "storage": r.app.state.retention.status(),
            "protection": {"running": ai["running"],
                           "cameras_online": sum(1 for c in ai["cameras"] if c["state"] == "RUNNING"),
                           "cameras_total": len(ai["cameras"]), "reduced": ai["performance"]["reduced"]},
            "alarms": [{"name": o["name"], "health": o["health"]} for o in r.app.state.alarms.outputs() if o["enabled"]]}


@inc_api.get("/incidents/{incident_id}")
@_errors
async def get_incident(r: Request, incident_id: str) -> dict:
    return _svc(r).get(incident_id)


class ActBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=40)


# Registered at the END of this file: FastAPI matches in registration order,
# and this catch-all path swallowed /keep and /media-ticket when it came first.
@_errors
async def act(r: Request, incident_id: str, action: str, body: ActBody | None = None) -> dict:
    action = action.replace("-", "_")
    if action not in ("acknowledge", "confirm", "false_alert", "resolve", "escalate", "reopen"):
        raise HTTPException(404, "Not found.")
    b = body or ActBody()
    return _svc(r).act(incident_id, action, _person(r), b.note, b.reason)


class KeepBody(BaseModel):
    keep: bool = True


@inc_api.post("/incidents/{incident_id}/keep")
@_errors
async def keep(r: Request, incident_id: str, body: KeepBody | None = None) -> dict:
    return _svc(r).keep(incident_id, _person(r), (body or KeepBody()).keep)


@inc_api.delete("/incidents/{incident_id}")
@_errors
async def delete(r: Request, incident_id: str) -> dict:
    _svc(r).delete(incident_id, _person(r))
    return {"ok": True}


class ManualBody(BaseModel):
    camera_id: str = Field(max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    severity: str = Field(default="HIGH", pattern="^(LOW|HIGH|CRITICAL)$")
    capture_snapshot: bool = True
    save_last_15s: bool = True


@inc_api.post("/incidents-manual")
@_errors
async def manual(r: Request, body: ManualBody) -> dict:
    return _svc(r).manual(_person(r), body.camera_id, body.description, body.severity,
                          body.capture_snapshot, body.save_last_15s)


@inc_api.post("/incidents/{incident_id}/media-ticket")
@_errors
async def media_ticket(r: Request, incident_id: str, kind: str = "snapshot") -> dict:
    if kind not in ("snapshot", "clip", "thumbnail"):
        raise HTTPException(400, "Unknown media.")
    require(_role(r), "view")
    _svc(r).get(incident_id)  # exists, not deleted
    now = time.time()
    for t, (_, _, exp) in list(_tickets.items()):
        if exp < now:
            _tickets.pop(t, None)
    t = secrets.token_urlsafe(24)
    _tickets[t] = (incident_id, kind, now + 60)
    who = _session(r)
    _tickets_by[t] = who.name if who else INSTALLER
    return {"ticket": t}


_tickets_by: dict[str, str] = {}


@media_api.get("/incidents/{incident_id}/{kind}")
async def media(r: Request, incident_id: str, kind: str, ticket: str) -> Response:
    r.app.state.local_guard.check_origin(r)
    entry = _tickets.get(ticket)
    if not entry or entry[0] != incident_id or entry[1] != kind or entry[2] < time.time():
        raise HTTPException(401, "This link expired. Refresh the page.")
    if kind == "snapshot":  # clips may need several range requests; snapshots are one-shot
        _tickets.pop(ticket, None)
    try:
        who = Session("", "", _tickets_by.get(ticket, INSTALLER), INSTALLER, 0)
        data, ctype = _svc(r).media_bytes(incident_id, kind, who if kind == "clip" else None, INSTALLER)
    except (IncidentError, Forbidden) as e:
        raise HTTPException(404, str(e)) from None
    return Response(data, media_type=ctype, headers={"Cache-Control": "no-store", "Accept-Ranges": "none"})


@inc_api.get("/incidents/{incident_id}/export")
@_errors
async def export(r: Request, incident_id: str) -> Response:
    data, name = _svc(r).export(incident_id, _person(r))
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ── Guard Mode (§61) ──────────────────────────────────────────────────
@inc_api.get("/guard-mode")
async def guard_mode(r: Request) -> dict:
    ov = await overview(r)
    open_alerts = _svc(r).list(status="UNREVIEWED", limit=20)
    return {"protection": ov["protection"], "latest": open_alerts[0] if open_alerts else ov["latest"],
            "unreviewed": len(open_alerts),
            "fullscreen": [i for i in open_alerts if i["severity"] in ("HIGH", "CRITICAL")][:1]}


# ── alarms ────────────────────────────────────────────────────────────
class OutputBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern="^(PC_SOUND|HIKVISION_IO|DAHUA_IO|NETWORK_RELAY|USB_RELAY)$")
    device_id: str | None = Field(default=None, max_length=64)
    config: dict = Field(default_factory=dict)


class RuleBody(BaseModel):
    enabled: bool | None = None
    min_severity: str | None = Field(default=None, pattern="^(INFO|LOW|HIGH|CRITICAL)$")
    alarm_output_id: str | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=120)
    cooldown_seconds: int | None = Field(default=None, ge=5, le=3600)
    repeat_until_ack: bool | None = None


class TestBody(BaseModel):
    output_id: str
    seconds: int = Field(default=2, ge=1, le=3)


@inc_api.get("/alarms")
async def alarms(r: Request) -> dict:
    a = r.app.state.alarms
    return {"outputs": a.outputs(), "rules": a.rules()}


@inc_api.post("/alarms/outputs")
@_errors
async def add_output(r: Request, body: OutputBody) -> dict:
    require(_role(r), "configure_alarms")
    return r.app.state.alarms.add_output(body.name, body.kind, body.device_id, body.config)


@inc_api.post("/alarms/test")
@_errors
async def test_alarm(r: Request, body: TestBody) -> dict:
    require(_role(r), "configure_alarms")
    return await r.app.state.alarms.test(body.output_id, body.seconds)


@inc_api.put("/alarms/rules/{rule_id}")
@_errors
async def update_rule(r: Request, rule_id: str, body: RuleBody) -> dict:
    require(_role(r), "configure_alarms")
    return r.app.state.alarms.update_rule(rule_id, **body.model_dump())


# ── storage & retention ───────────────────────────────────────────────
class RetentionBody(BaseModel):
    incident_type: str | None = None
    severity: str | None = Field(default=None, pattern="^(INFO|LOW|HIGH|CRITICAL)$")
    retention_days: int = Field(ge=1, le=365)
    keep_if_confirmed: bool = False


@inc_api.get("/storage/status")
async def storage(r: Request) -> dict:
    return {**r.app.state.retention.status(), "buffer_ram_bytes": r.app.state.buffers.memory_bytes(),
            "policies": r.app.state.retention.policies()}


@inc_api.post("/storage/cleanup")
@_errors
async def cleanup(r: Request) -> dict:
    require(_person(r).role, "configure_retention")
    return r.app.state.retention.cleanup()


@inc_api.put("/retention")
@_errors
async def set_retention(r: Request, body: RetentionBody) -> dict:
    require(_person(r).role, "configure_retention")
    r.app.state.retention.set_policy(body.incident_type, body.severity, body.retention_days, body.keep_if_confirmed)
    return {"policies": r.app.state.retention.policies()}


class LocationBody(BaseModel):
    code: str = Field(min_length=3, max_length=3)


@inc_api.get("/settings/location-code")
async def get_loc(r: Request) -> dict:
    return {"code": _svc(r).location_code()}


@inc_api.put("/settings/location-code")
@_errors
async def put_loc(r: Request, body: LocationBody) -> dict:
    s = _session(r)
    if s is not None:
        require(s.role, "configure_retention")  # owner, or the installer at setup
    return {"code": _svc(r).set_location_code(body.code)}


# LAST on purpose — see the note on act(): the more specific
# /incidents/{id}/keep and /media-ticket routes above must match first.
inc_api.add_api_route("/incidents/{incident_id}/{action}", act, methods=["POST"])
