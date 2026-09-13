"""V380 / V380 Pro compatibility path (Phase 1 §22).

A V380 label says nothing about whether Guard can use the camera — many are
cloud/P2P-only. So this adapter never trusts the name; it walks the doc's
decision tree and reports honestly:

    ONVIF answers?          → use ONVIF (the ONVIF adapter takes it)
    documented local RTSP?  → test that stream
    neither                 → INCOMPATIBLE with Guard Basic, with the reason,
                              and the advice to connect it through a recorder

The "can a compatible local NVR expose it?" branch is answered at the
recorder, not here: if the shop's NVR has the V380 on a channel, Guard sees
that channel when it connects to the NVR.

UNTESTED ON HARDWARE until the lab kit arrives.
"""

from __future__ import annotations

import asyncio

from ..streams.rtsp_probe import rtsp_describe, rtsp_options
from .base import AuthResult, CCTVAdapter, Channel, Credentials, DeviceInfo, DeviceType, StreamProfile
from .onvif import OnvifAdapter

# The V380 app's proprietary port. Open = almost certainly a V380-family device.
V380_P2P_PORT = 8800
MAIN, SUB = "/live/ch00_0", "/live/ch00_1"

INCOMPATIBLE_REASON = (
    "This camera only streams through its phone app's cloud service. "
    "Guard needs a local video stream. Connect it to a compatible recorder, or use another camera."
)


async def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


class V380Adapter(CCTVAdapter):
    key = "v380"
    priority = 30

    def __init__(self, rtsp_port: int = 554) -> None:
        self.rtsp_port = rtsp_port
        self.last_reason: str | None = None

    async def probe(self, host: str, port: int | None = None) -> bool:
        return await _port_open(host, V380_P2P_PORT)

    async def assess(self, host: str) -> tuple[str, str | None]:
        """→ ("onvif" | "rtsp" | "incompatible", reason). No credentials needed."""
        if await OnvifAdapter().probe(host, 80):
            return "onvif", None
        if await rtsp_options(host, self.rtsp_port):
            r = await rtsp_describe(f"rtsp://{host}:{self.rtsp_port}{MAIN}", None)
            if r.status in (200, 401):
                return "rtsp", None
        return "incompatible", INCOMPATIBLE_REASON

    async def authenticate(self, host: str, port: int | None, creds: Credentials) -> AuthResult:
        route, reason = await self.assess(host)
        self.last_reason = reason
        if route == "incompatible":
            return AuthResult.UNSUPPORTED
        if route == "onvif":
            return await OnvifAdapter().authenticate(host, 80, creds)
        r = await rtsp_describe(f"rtsp://{host}:{self.rtsp_port}{MAIN}", creds)
        if r.ok:
            return AuthResult.OK
        return AuthResult.BAD_CREDENTIALS if r.unauthorized else AuthResult.UNSUPPORTED

    async def get_device_info(self, host: str, port: int | None, creds: Credentials) -> DeviceInfo:
        return DeviceInfo(manufacturer="V380 (generic)", device_type=DeviceType.CAMERA, channel_count=1, rtsp=True)

    async def list_channels(self, host: str, port: int | None, creds: Credentials) -> list[Channel]:
        route, _ = await self.assess(host)
        if route == "onvif":
            return await OnvifAdapter().list_channels(host, 80, creds)
        if route != "rtsp":
            return []
        profiles = []
        for path, name, main in ((MAIN, "Main stream", True), (SUB, "Sub stream", False)):
            uri = f"rtsp://{host}:{self.rtsp_port}{path}"
            r = await rtsp_describe(uri, creds)
            if r.ok:
                profiles.append(StreamProfile(token=path, name=name, uri=uri, codec=r.codec, is_main=main))
        return [Channel(number=1, name="Camera 01", online=bool(profiles), profiles=profiles)]
