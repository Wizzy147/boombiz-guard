"""Hikvision cameras, DVRs and NVRs — ISAPI for identity and channels, RTSP
for video (Phase 1 §20).

ONVIF is still tried first (priority 10 vs 20): this adapter exists because
many Hikvision recorders ship with ONVIF switched OFF, and because ISAPI
names channels the way the shop named them ("Entrance"), where ONVIF says
"VideoSource_1".

RTSP path convention: /Streaming/Channels/<ch>01 = main, <ch>02 = sub.

UNTESTED ON HARDWARE until the lab kit arrives — verified against the
simulated recorder in lab/ only. The response shapes follow Hikvision's
published ISAPI schema.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from ..streams.rtsp_probe import rtsp_describe
from .base import AuthResult, CCTVAdapter, Channel, Credentials, DeviceInfo, DeviceType, StreamProfile
from .onvif import mark_main


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    f = el.find(path)
    return f.text.strip() if f is not None and f.text else None


class HikvisionAdapter(CCTVAdapter):
    key = "hikvision"
    priority = 20

    def __init__(self, rtsp_port: int = 554) -> None:
        self.rtsp_port = rtsp_port

    def _base(self, host: str, port: int | None) -> str:
        scheme = "https" if port == 443 else "http"
        return f"{scheme}://{host}:{port or 80}"

    async def _get(self, host: str, port: int | None, path: str, creds: Credentials | None) -> httpx.Response:
        auth = httpx.DigestAuth(creds.username, creds.password) if creds else None
        async with httpx.AsyncClient(timeout=6.0, verify=False) as c:
            return await c.get(self._base(host, port) + path, auth=auth)

    async def probe(self, host: str, port: int | None = None) -> bool:
        try:
            r = await self._get(host, port, "/ISAPI/System/deviceInfo", None)
        except httpx.HTTPError:
            return False
        # Unauthenticated ISAPI answers 401 with a Digest challenge; the
        # realm or server header is what marks it as Hikvision.
        hdrs = " ".join(f"{k}:{v}" for k, v in r.headers.items()).lower()
        return r.status_code in (200, 401) and ("hikvision" in hdrs or "dnvrs" in hdrs or "isapi" in hdrs or "ds-" in hdrs)

    async def authenticate(self, host: str, port: int | None, creds: Credentials) -> AuthResult:
        try:
            r = await self._get(host, port, "/ISAPI/System/deviceInfo", creds)
        except httpx.HTTPError:
            return AuthResult.UNREACHABLE
        if r.status_code == 200:
            return AuthResult.OK
        if r.status_code == 401:
            # Hikvision locks the account after repeated failures and says so.
            return AuthResult.LOCKED if "lock" in r.text.lower() else AuthResult.BAD_CREDENTIALS
        if r.status_code == 403:
            return AuthResult.LOCKED
        return AuthResult.UNSUPPORTED

    async def get_device_info(self, host: str, port: int | None, creds: Credentials) -> DeviceInfo:
        r = await self._get(host, port, "/ISAPI/System/deviceInfo", creds)
        r.raise_for_status()
        d = _strip_ns(ET.fromstring(r.content))
        kind = (_t(d, "deviceType") or "").upper()
        model = _t(d, "model")
        channels = await self.list_channels(host, port, creds)
        if "NVR" in kind:
            dtype = DeviceType.NVR
        elif "DVR" in kind:
            dtype = DeviceType.DVR
        elif "IPCAMERA" in kind or "IPC" in kind:
            dtype = DeviceType.CAMERA
        else:
            dtype = DeviceType.NVR if len(channels) > 1 else DeviceType.CAMERA
        return DeviceInfo(
            manufacturer="Hikvision",
            model=model,
            firmware=_t(d, "firmwareVersion"),
            serial_number=_t(d, "serialNumber"),
            device_type=dtype,
            channel_count=len(channels),
            rtsp=True,
            alarm_output=(_t(d, "alarmOutNum") or "0") not in ("0", ""),
        )

    async def list_channels(self, host: str, port: int | None, creds: Credentials) -> list[Channel]:
        names: dict[int, tuple[str, bool]] = {}
        # Recorders list their inputs here (analogue AND IP); a camera 404s.
        for path, tag in (
            ("/ISAPI/ContentMgmt/InputProxy/channels/status", "InputProxyChannelStatus"),
            ("/ISAPI/System/Video/inputs/channels", "VideoInputChannel"),
        ):
            try:
                r = await self._get(host, port, path, creds)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            root = _strip_ns(ET.fromstring(r.content))
            for el in root.iter(tag):
                try:
                    n = int(_t(el, "id") or 0)
                except ValueError:
                    continue
                online = (_t(el, "online") or _t(el, "videoInputEnabled") or "true").lower() == "true"
                names.setdefault(n, (_t(el, "name") or f"Camera {n:02d}", online))
        if not names:
            names[1] = ("Camera 01", True)

        out: list[Channel] = []
        for n in sorted(names):
            name, online = names[n]
            profiles = []
            for suffix, pname in (("01", "Main stream"), ("02", "Sub stream")):
                uri = f"rtsp://{host}:{self.rtsp_port}/Streaming/Channels/{n}{suffix}"
                probe = await rtsp_describe(uri, creds) if online else None
                if probe and probe.ok:
                    profiles.append(StreamProfile(token=f"{n}{suffix}", name=pname, uri=uri, codec=probe.codec))
            # Resolution isn't in the SDP; the stream test fills it in. Mark
            # main by Hikvision's convention instead of by size.
            for p in profiles:
                p.is_main = p.token.endswith("01")
            if profiles and not any(p.is_main for p in profiles):
                mark_main(profiles)
            out.append(Channel(number=n, name=name, online=online and bool(profiles), profiles=profiles))
        return out
