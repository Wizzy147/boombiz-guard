"""Auto Setup local API (app/setup/service.py). Same protection as every
other setup route: loopback + Host/Origin checks + X-Guard-Token."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..cloud.client import CloudError
from ..services.devices import GuardError
from .routes import _protect

setup_api = APIRouter(prefix="/api/v1/setup", dependencies=[Depends(_protect)])


def _svc(r: Request):  # noqa: ANN202
    return r.app.state.setup


def _plain(e: Exception) -> HTTPException:
    return HTTPException(400, str(e) or "Something went wrong. Try again.")


class ConnectBody(BaseModel):
    device_id: str = Field(max_length=64)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=128)


class ApplyBody(BaseModel):
    camera_ids: list[str] = Field(max_length=64)


class Rect(BaseModel):
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)


class AreasBody(BaseModel):
    camera_id: str = Field(max_length=64)
    products: Rect | None = None
    exit: Rect | None = None


class CameraBody(BaseModel):
    camera_id: str = Field(max_length=64)


class StepBody(BaseModel):
    step: str = Field(pattern="^(person|products|exit)$")


class HelpBody(BaseModel):
    reason: str = Field(pattern="^[a-z_]{2,40}$")


class CompleteBody(BaseModel):
    test_passed: bool | None = None


@setup_api.get("/state")
async def state(r: Request) -> dict:
    return _svc(r).state()


@setup_api.post("/start")
async def start(r: Request) -> dict:
    return _svc(r).start()


@setup_api.get("/checks")
async def checks(r: Request) -> dict:
    return _svc(r).checks_status()


@setup_api.post("/checks/rerun")
async def rerun(r: Request) -> dict:
    return _svc(r).start_checks()


@setup_api.get("/found")
async def found(r: Request) -> dict:
    return {"devices": _svc(r).found()}


@setup_api.post("/connect")
async def connect(r: Request, body: ConnectBody) -> dict:
    try:
        return await _svc(r).connect(body.device_id, body.username.strip(), body.password)
    except GuardError as e:
        raise _plain(e) from None


@setup_api.post("/survey")
async def survey(r: Request) -> dict:
    return await _svc(r).survey()


@setup_api.get("/recommendation")
async def recommendation(r: Request) -> dict:
    return _svc(r).recommendation()


@setup_api.post("/apply")
async def apply(r: Request, body: ApplyBody) -> dict:
    try:
        return await _svc(r).apply(body.camera_ids)
    except (ValueError, GuardError) as e:
        raise _plain(e) from None


@setup_api.post("/areas")
async def areas(r: Request, body: AreasBody) -> dict:
    try:
        return _svc(r).areas(body.camera_id, body.products.model_dump() if body.products else None,
                             body.exit.model_dump() if body.exit else None)
    except ValueError as e:
        raise _plain(e) from None


@setup_api.post("/link")
async def link(r: Request) -> dict:
    """Demo mode → "Activate Boombiz Guard": a browser sign-in link that
    carries what Guard found, so the cloud can recommend the package."""
    try:
        name = f"{r.app.state.incidents.location_code()} Guard PC"
        return await _svc(r).link(name)
    except CloudError as e:
        raise _plain(e) from None


@setup_api.post("/test/start")
async def test_start(r: Request, body: CameraBody) -> dict:
    try:
        return r.app.state.guard_test.start(body.camera_id)
    except ValueError as e:
        raise _plain(e) from None


@setup_api.get("/test")
async def test_status(r: Request) -> dict:
    return r.app.state.guard_test.status()


@setup_api.post("/test/skip")
async def test_skip(r: Request, body: StepBody) -> dict:
    try:
        return r.app.state.guard_test.skip(body.step)
    except ValueError as e:
        raise _plain(e) from None


@setup_api.post("/test/checks")
async def test_checks(r: Request) -> dict:
    try:
        return r.app.state.guard_test.run_checks()
    except ValueError as e:
        raise _plain(e) from None


@setup_api.post("/help")
async def need_help(r: Request, body: HelpBody) -> dict:
    """Auto Setup gave up: "Technical assistance required". Recorded for the
    funnel; the screen offers a Boombiz visit and Advanced Setup."""
    _svc(r).help(body.reason)
    return {"ok": True}


@setup_api.post("/advanced")
async def advanced(r: Request) -> dict:
    r.app.state.telemetry.report("advanced")
    return {"ok": True}


@setup_api.post("/complete")
async def complete(r: Request, body: CompleteBody) -> dict:
    return _svc(r).complete(body.test_passed)
