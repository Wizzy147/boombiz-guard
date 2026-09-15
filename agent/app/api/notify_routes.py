"""Pop-up notification feed (tray app) and cloud link (phone push)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..cloud.client import CloudError
from ..notify.feed import feed
from ..review.permissions import require
from .incident_routes import _role
from .routes import _protect

notify_api = APIRouter(prefix="/api/v1", dependencies=[Depends(_protect)])


@notify_api.get("/notifications")
async def notifications(r: Request, after: str | None = None, limit: int = 50) -> dict:
    """New pop-up-worthy items since `after` (ISO time). The tray polls this."""
    try:
        return feed(r.app.state.db, after, min(max(limit, 1), 200))
    except ValueError:
        raise HTTPException(400, "Bad cursor.") from None


@notify_api.get("/cloud/status")
async def cloud_status(r: Request) -> dict:
    link = r.app.state.cloud
    # While a pairing code or browser sign-in is waiting, ask the cloud now
    # instead of on the 60-second loop, so the screen flips to "Linked" as the
    # owner confirms it.
    if (link.state.get("pairing_code") or link.state.get("link_code")) and not link.state.get("paired"):
        await link.refresh()
    sync = getattr(r.app.state, "sync", None)
    lic = getattr(r.app.state, "licence", None)
    return {**link.state, "queued": link.queued(), "cloud_url": link.base,
            "sync": sync.status() if sync else None, "licence": lic.current() if lic else None}


@notify_api.post("/cloud/pair")
async def cloud_pair(r: Request) -> dict:
    require(_role(r), "configure_retention")  # owner, or the installer at setup
    try:
        return await r.app.state.cloud.start_pairing(r.app.state.incidents.location_code() + " Guard PC",
                                                     r.app.state.version)
    except CloudError as e:
        raise HTTPException(400, str(e)) from None


class ActivateBody(BaseModel):
    code: str = Field(min_length=8, max_length=20)


@notify_api.post("/cloud/activate")
async def cloud_activate(r: Request, body: ActivateBody) -> dict:
    """Redeem an installer activation code (GARD-XXXX-XXXX) made in Boombiz Guard."""
    require(_role(r), "configure_retention")  # owner, or the installer at setup
    try:
        return await r.app.state.cloud.activate(body.code, r.app.state.incidents.location_code() + " Guard PC",
                                                r.app.state.version)
    except CloudError as e:
        raise HTTPException(400, str(e)) from None


class BandwidthBody(BaseModel):
    mode: Literal["NORMAL", "LOW", "METADATA_ONLY"]
    cap_gb: float | None = Field(default=None, ge=1, le=20)


@notify_api.get("/cloud/bandwidth")
async def get_bandwidth(r: Request) -> dict:
    return r.app.state.sync.bandwidth.get()


@notify_api.put("/cloud/bandwidth")
async def put_bandwidth(r: Request, body: BandwidthBody) -> dict:
    """§98 upload mode and §99 backlog cap. Owner (or installer at setup)."""
    require(_role(r), "configure_retention")
    bw = r.app.state.sync.bandwidth
    if body.cap_gb is not None:
        bw.set_cap_gb(body.cap_gb)
    out = bw.set(body.mode)
    r.app.state.sync.enforce_cap()
    return out


@notify_api.post("/cloud/unpair")
async def cloud_unpair(r: Request) -> dict:
    require(_role(r), "configure_retention")
    r.app.state.cloud.unpair()
    return {"ok": True}
