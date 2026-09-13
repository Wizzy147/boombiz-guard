"""Dahua cameras, DVRs (XVR) and NVRs — HTTP CGI for identity and channel
names, RTSP for video (Phase 1 §21). ONVIF still runs first.

RTSP path convention: /cam/realmonitor?channel=<n>&subtype=0 (main) / 1 (sub).

UNTESTED ON HARDWARE until the lab kit arrives — verified against the
simulated recorder in lab/ only. CGI shapes follow Dahua's HTTP API docs:
key=value lines, e.g. `table.ChannelTitle[0].Name=Entrance`.
"""

from __future__ import annotations

import re

import httpx

from ..streams.rtsp_probe import rtsp_describe
from .base import AuthResult, CCTVAdapter, Channel, Credentials, DeviceInfo, DeviceType, StreamProfile


def parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class DahuaAdapter(CCTVAdapter):
    key = "dahua"
    priority = 20

    def __init__(self, rtsp_port: int = 554) -> None:
        self.rtsp_port = rtsp_port

    async def _get(self, host: str, port: int | None, path: str, creds: Credentials | None) -> httpx.Response:
        auth = httpx.DigestAuth(creds.username, creds.password) if creds else None
        scheme = "https" if port == 443 else "http"
        async with httpx.AsyncClient(timeout=6.0, verify=False) as c:
            return await c.get(f"{scheme}://{host}:{port or 80}{path}", auth=auth)

    async def probe(self, host: str, port: int | None = None) -> bool:
        try:
            r = await self._get(host, port, "/cgi-bin/magicBox.cgi?action=getDeviceType", None)
        except httpx.HTTPError:
            return False
        challenge = r.headers.get("www-authenticate", "")
        # Dahua's digest realm is "Login to <serial>"; its web server says so too.
        return r.status_code in (200, 401) and ("login to" in challenge.lower() or "dahua" in challenge.lower() or "dh" in r.headers.get("server", "").lower())

    async def authenticate(self, host: str, port: int | None, creds: Credentials) -> AuthResult:
        try:
            r = await self._get(host, port, "/cgi-bin/magicBox.cgi?action=getDeviceType", creds)
        except httpx.HTTPError:
            return AuthResult.UNREACHABLE
        if r.status_code == 200:
            return AuthResult.OK
        if r.status_code == 401:
            return AuthResult.LOCKED if "locked" in r.text.lower() else AuthResult.BAD_CREDENTIALS
        return AuthResult.UNSUPPORTED

    async def get_device_info(self, host: str, port: int | None, creds: Credentials) -> DeviceInfo:
        dtype_raw = parse_kv((await self._get(host, port, "/cgi-bin/magicBox.cgi?action=getDeviceType", creds)).text).get("type", "")
        sysinfo = parse_kv((await self._get(host, port, "/cgi-bin/magicBox.cgi?action=getSystemInfo", creds)).text)
        fw = parse_kv((await self._get(host, port, "/cgi-bin/magicBox.cgi?action=getSoftwareVersion", creds)).text)
        channels = await self.list_channels(host, port, creds)
        kind = (sysinfo.get("deviceType") or dtype_raw).upper()
        if "NVR" in kind:
            dtype = DeviceType.NVR
        elif "XVR" in kind or "DVR" in kind or "HCVR" in kind:
            dtype = DeviceType.DVR
        elif kind.startswith(("IPC", "DH-IPC", "SD")):
            dtype = DeviceType.CAMERA
        else:
            dtype = DeviceType.NVR if len(channels) > 1 else DeviceType.CAMERA
        return DeviceInfo(
            manufacturer="Dahua",
            model=dtype_raw or sysinfo.get("deviceType"),
            firmware=fw.get("version"),
            serial_number=sysinfo.get("serialNumber"),
            device_type=dtype,
            channel_count=len(channels),
            rtsp=True,
        )

    async def list_channels(self, host: str, port: int | None, creds: Credentials) -> list[Channel]:
        titles: dict[int, str] = {}
        try:
            r = await self._get(host, port, "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle", creds)
            if r.status_code == 200:
                for k, v in parse_kv(r.text).items():
                    m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name", k)
                    if m:
                        titles[int(m.group(1)) + 1] = v  # CGI is 0-based, RTSP is 1-based
        except httpx.HTTPError:
            pass
        if not titles:
            titles[1] = "Camera 01"

        out: list[Channel] = []
        for n in sorted(titles):
            profiles = []
            for subtype, pname in ((0, "Main stream"), (1, "Sub stream")):
                uri = f"rtsp://{host}:{self.rtsp_port}/cam/realmonitor?channel={n}&subtype={subtype}"
                probe = await rtsp_describe(uri, creds)
                if probe.ok:
                    profiles.append(
                        StreamProfile(token=f"{n}-{subtype}", name=pname, uri=uri, codec=probe.codec, is_main=subtype == 0)
                    )
            out.append(Channel(number=n, name=titles[n], online=bool(profiles), profiles=profiles))
        return out
