"""Generic RTSP — the fallback for cameras with no ONVIF and no known brand
API (PRD §9), and the home of the installer's manual RTSP URL (Phase 1 §23).

Account-lockout safety: many cheap cameras lock after 3–5 bad logins. So the
common paths are first probed WITHOUT credentials (a 404 tells us a path is
absent, a 401 tells us it exists), and the installer's credentials are sent
exactly ONCE, to the first path that exists. Only after that one attempt
succeeds are the other paths opened with them.
"""

from __future__ import annotations

from urllib.parse import urlparse

from ..streams.rtsp_probe import rtsp_describe, rtsp_options
from .base import AuthResult, CCTVAdapter, Channel, Credentials, DeviceInfo, DeviceType, StreamProfile

# Paths widely documented by OEM firmware. Order: most common first.
COMMON_PATHS: tuple[str, ...] = (
    "/Streaming/Channels/101",
    "/cam/realmonitor?channel=1&subtype=0",
    "/live/ch00_0",
    "/stream1",
    "/h264Preview_01_main",
    "/11",
    "/live",
    "/h264",
    "/media/video1",
    "/",
)

# Main → sub stream pairs where the convention is known.
SUB_OF: dict[str, str] = {
    "/Streaming/Channels/101": "/Streaming/Channels/102",
    "/cam/realmonitor?channel=1&subtype=0": "/cam/realmonitor?channel=1&subtype=1",
    "/live/ch00_0": "/live/ch00_1",
    "/stream1": "/stream2",
    "/h264Preview_01_main": "/h264Preview_01_sub",
    "/11": "/12",
}


class GenericRtspAdapter(CCTVAdapter):
    key = "rtsp"
    priority = 90

    def __init__(self, rtsp_port: int = 554, manual_uri: str | None = None) -> None:
        self.rtsp_port = rtsp_port
        self.manual_uri = manual_uri
        self._found: str | None = None

    def _uri(self, host: str, path: str) -> str:
        return f"rtsp://{host}:{self.rtsp_port}{path}"

    async def probe(self, host: str, port: int | None = None) -> bool:
        return await rtsp_options(host, self.rtsp_port)

    async def _candidate_paths(self, host: str) -> list[str]:
        """Paths that answered 200/401 without credentials, in COMMON_PATHS order."""
        if self.manual_uri:
            return [self.manual_uri]
        out = []
        for path in COMMON_PATHS:
            r = await rtsp_describe(self._uri(host, path), None)
            if r.status == 0:
                return out  # nothing listening — stop, don't hammer
            if r.status in (200, 401):
                out.append(self._uri(host, path))
        return out

    async def _existing_path(self, host: str) -> str | None:
        paths = await self._candidate_paths(host)
        return paths[0] if paths else None

    async def authenticate(self, host: str, port: int | None, creds: Credentials) -> AuthResult:
        """At most ONE rejected login, ever.

        Many servers answer 401 to every path before they check whether it
        exists. So: send the credentials to the first candidate. 401 → they are
        wrong, stop. 404 (or any non-401) → the server ACCEPTED them and the
        path simply isn't there, so trying the next candidate with the same,
        now-proven credentials cannot lock the account.
        """
        candidates = await self._candidate_paths(host)
        if not candidates:
            return AuthResult.UNSUPPORTED
        for uri in candidates:
            r = await rtsp_describe(uri, creds)
            if r.ok:
                self._found = uri
                return AuthResult.OK
            if r.unauthorized:
                return AuthResult.BAD_CREDENTIALS
            if r.status == 0:
                return AuthResult.UNREACHABLE
        return AuthResult.UNSUPPORTED

    async def get_device_info(self, host: str, port: int | None, creds: Credentials) -> DeviceInfo:
        return DeviceInfo(device_type=DeviceType.CAMERA, channel_count=1, rtsp=True)

    async def list_channels(self, host: str, port: int | None, creds: Credentials) -> list[Channel]:
        main = self._found or await self._existing_path(host)
        if not main:
            return []
        first = await rtsp_describe(main, creds)
        if not first.ok:
            return []
        profiles = [StreamProfile(token="main", name="Main stream", uri=main, codec=first.codec, is_main=True)]
        path = main.split(f":{self.rtsp_port}", 1)[-1] if not self.manual_uri else urlparse(main).path
        sub_path = SUB_OF.get(path)
        if sub_path:
            sub_uri = self._uri(host, sub_path)
            sub = await rtsp_describe(sub_uri, creds)
            if sub.ok:
                profiles.append(StreamProfile(token="sub", name="Sub stream", uri=sub_uri, codec=sub.codec))
        return [Channel(number=1, name="Camera 01", online=True, profiles=profiles)]
