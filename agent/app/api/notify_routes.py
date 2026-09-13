"""Pop-up notification feed (tray app) and cloud link (phone push)."""

from __future__ import annotations

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
    # While a pairing code is waiting, ask the cloud now instead of on the
    # 60-second loop, so the screen flips to "Linked" as the owner claims it.
    if link.state.get("pairing_code") and not link.state.get("paired"):
        await link.refresh()
    return {**link.state, "queued": link.queued(), "cloud_url": link.base}


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


@notify_api.post("/cloud/unpair")
async def cloud_unpair(r: Request) -> dict:
    require(_role(r), "configure_retention")
    r.app.state.cloud.unpair()
    return {"ok": True}
