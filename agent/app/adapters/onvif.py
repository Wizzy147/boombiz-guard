"""ONVIF adapter — the portable route, tried first (PRD §9).

A deliberately small SOAP client over httpx instead of onvif-zeep: Guard needs
six operations, and zeep's WSDL loading is slow, bundles badly with
PyInstaller, and chokes on the non-conforming WSDLs cheap OEM firmware ships.

Auth is WS-Security UsernameToken with PasswordDigest. Many recorders reject
a digest whose Created time is off by more than a few seconds, and shop DVRs
rarely have the right time — so the device's clock is read first
(GetSystemDateAndTime is unauthenticated by spec) and Created is written in
the DEVICE's time.
"""

from __future__ import annotations

import base64
import hashlib
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import httpx

from .base import (
    AuthResult,
    CCTVAdapter,
    Channel,
    Credentials,
    DeviceInfo,
    DeviceType,
    StreamProfile,
)

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}

_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Header>{security}</s:Header><s:Body>{body}</s:Body></s:Envelope>"""

_SECURITY = """<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{user}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
</UsernameToken></Security>"""


class OnvifError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def ws_security(creds: Credentials, clock_offset: timedelta) -> str:
    nonce = os.urandom(16)
    created = (datetime.now(timezone.utc) + clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + creds.password.encode()).digest()).decode()
    return _SECURITY.format(
        user=_xml_escape(creds.username), digest=digest, nonce=base64.b64encode(nonce).decode(), created=created
    )


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text else None


def _codec(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.upper()
    return {"H264": "H264", "H.264": "H264", "H265": "H265", "HEVC": "H265", "JPEG": "MJPEG"}.get(raw, raw)


def strip_credentials(uri: str) -> str:
    """Some firmware embeds user:pass in GetStreamUri. Guard never stores that."""
    p = urlparse(uri)
    if p.username or p.password:
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        p = p._replace(netloc=host)
    return urlunparse(p)


class OnvifClient:
    def __init__(self, endpoint: str, creds: Credentials | None, timeout: float = 6.0) -> None:
        self.endpoint = endpoint
        self.creds = creds
        self.timeout = timeout
        self.clock_offset = timedelta(0)
        self.media_endpoint: str | None = None

    async def call(self, body: str, *, endpoint: str | None = None, auth: bool = True) -> ET.Element:
        security = ws_security(self.creds, self.clock_offset) if (auth and self.creds) else ""
        payload = _ENVELOPE.format(security=security, body=body)
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            try:
                r = await client.post(
                    endpoint or self.endpoint,
                    content=payload.encode(),
                    headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                )
            except httpx.HTTPError as e:
                raise OnvifError(f"Device did not respond ({type(e).__name__}).") from None
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise OnvifError(f"Device sent an unreadable ONVIF reply (HTTP {r.status_code}).") from None
        fault = root.find(".//s:Fault", NS)
        if fault is not None or r.status_code >= 400:
            text = " ".join(t.strip() for t in (fault.itertext() if fault is not None else []) if t.strip())
            is_auth = r.status_code in (400, 401, 403) and any(
                k in text for k in ("NotAuthorized", "Sender not Authorized", "FailedAuthentication", "Unauthorized")
            ) or r.status_code == 401
            raise OnvifError(text or f"ONVIF error (HTTP {r.status_code}).", auth=is_auth)
        body_el = root.find("s:Body", NS)
        if body_el is None:
            raise OnvifError("ONVIF reply had no body.")
        return body_el

    async def sync_clock(self) -> None:
        try:
            body = await self.call("<tds:GetSystemDateAndTime/>", auth=False)
        except OnvifError:
            return
        utc = body.find(".//tt:UTCDateTime", NS)
        if utc is None:
            return
        try:
            d = utc.find("tt:Date", NS)
            t = utc.find("tt:Time", NS)
            device_now = datetime(
                int(_text(d, "tt:Year")), int(_text(d, "tt:Month")), int(_text(d, "tt:Day")),
                int(_text(t, "tt:Hour")), int(_text(t, "tt:Minute")), int(_text(t, "tt:Second")),
                tzinfo=timezone.utc,
            )
        except (TypeError, ValueError):
            return
        self.clock_offset = device_now - datetime.now(timezone.utc)

    async def device_information(self) -> dict[str, str | None]:
        body = await self.call("<tds:GetDeviceInformation/>")
        r = body.find("tds:GetDeviceInformationResponse", NS)
        return {k: _text(r, f"tds:{k}") for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")}

    async def capabilities(self) -> ET.Element:
        body = await self.call("<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
        media = body.find(".//tt:Media/tt:XAddr", NS)
        if media is not None and media.text:
            self.media_endpoint = self._rehost(media.text.strip())
        return body

    def _rehost(self, url: str) -> str:
        """Devices behind NAT/DHCP often advertise a stale IP — keep ours.

        Only the HOST is swapped. The port stays the device's own: an RTSP URI
        on :554 must not inherit the ONVIF web port it was fetched over.
        """
        ours = urlparse(self.endpoint).hostname or ""
        theirs = urlparse(url)
        netloc = f"{ours}:{theirs.port}" if theirs.port else ours
        return urlunparse(theirs._replace(netloc=netloc))

    async def profiles(self) -> list[ET.Element]:
        body = await self.call("<trt:GetProfiles/>", endpoint=self.media_endpoint)
        return body.findall(".//trt:Profiles", NS)

    async def stream_uri(self, token: str) -> str | None:
        req = (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{_xml_escape(token)}</trt:ProfileToken></trt:GetStreamUri>"
        )
        body = await self.call(req, endpoint=self.media_endpoint)
        uri = _text(body, ".//tt:Uri")
        return strip_credentials(self._rehost(uri)) if uri else None


def default_endpoint(host: str, port: int | None) -> str:
    return f"http://{host}:{port or 80}/onvif/device_service"


class OnvifAdapter(CCTVAdapter):
    key = "onvif"
    priority = 10

    def __init__(self, endpoint: str | None = None) -> None:
        self._endpoint = endpoint

    def _client(self, host: str, port: int | None, creds: Credentials | None) -> OnvifClient:
        return OnvifClient(self._endpoint or default_endpoint(host, port), creds)

    async def probe(self, host: str, port: int | None = None) -> bool:
        c = self._client(host, port, None)
        try:
            await c.call("<tds:GetSystemDateAndTime/>", auth=False)
            return True
        except OnvifError:
            return False

    async def authenticate(self, host: str, port: int | None, creds: Credentials) -> AuthResult:
        c = self._client(host, port, creds)
        await c.sync_clock()
        try:
            await c.device_information()
            return AuthResult.OK
        except OnvifError as e:
            if e.auth:
                return AuthResult.BAD_CREDENTIALS
            return AuthResult.UNREACHABLE

    async def get_device_info(self, host: str, port: int | None, creds: Credentials) -> DeviceInfo:
        c = self._client(host, port, creds)
        await c.sync_clock()
        info = await c.device_information()
        caps = await c.capabilities()
        profiles = await c.profiles()
        sources = {_text(p, "tt:VideoSourceConfiguration/tt:SourceToken") for p in profiles}
        sources.discard(None)
        n = max(len(sources), 1)
        return DeviceInfo(
            manufacturer=info.get("Manufacturer"),
            model=info.get("Model"),
            firmware=info.get("FirmwareVersion"),
            serial_number=info.get("SerialNumber"),
            device_type=_guess_type(info.get("Model"), n),
            channel_count=n,
            onvif=True,
            rtsp=True,
            audio=caps.find(".//tt:AudioSources", NS) is not None or any(
                p.find("tt:AudioEncoderConfiguration", NS) is not None for p in profiles
            ),
            alarm_output=(_text(caps, ".//tt:IO/tt:RelayOutputs") or "0") not in ("0", ""),
            ptz=caps.find(".//tt:PTZ", NS) is not None,
        )

    async def list_channels(self, host: str, port: int | None, creds: Credentials) -> list[Channel]:
        c = self._client(host, port, creds)
        await c.sync_clock()
        await c.capabilities()
        by_source: dict[str, Channel] = {}
        for p in await c.profiles():
            token = p.get("token") or ""
            source = _text(p, "tt:VideoSourceConfiguration/tt:SourceToken") or token
            enc = p.find("tt:VideoEncoderConfiguration", NS)
            uri = await c.stream_uri(token)
            if not uri:
                continue
            w = _text(enc, "tt:Resolution/tt:Width")
            h = _text(enc, "tt:Resolution/tt:Height")
            fps = _text(enc, "tt:RateControl/tt:FrameRateLimit")
            ch = by_source.setdefault(
                source,
                Channel(number=len(by_source) + 1, name=_text(p, "tt:VideoSourceConfiguration/tt:Name") or _text(p, "tt:Name") or f"Camera {len(by_source) + 1}"),
            )
            ch.profiles.append(
                StreamProfile(
                    token=token,
                    name=_text(p, "tt:Name") or token,
                    uri=uri,
                    codec=_codec(_text(enc, "tt:Encoding")),
                    width=int(w) if w else None,
                    height=int(h) if h else None,
                    fps=float(fps) if fps else None,
                )
            )
        for ch in by_source.values():
            mark_main(ch.profiles)
        return list(by_source.values())


def mark_main(profiles: list[StreamProfile]) -> None:
    """Largest resolution is the main stream; Guard prefers the rest (PRD §17)."""
    if not profiles:
        return
    main = max(profiles, key=lambda p: (p.width or 0) * (p.height or 0))
    for p in profiles:
        p.is_main = p is main


def _guess_type(model: str | None, channels: int) -> DeviceType:
    m = (model or "").upper()
    if "NVR" in m or m.startswith(("DS-76", "DS-77", "DS-96", "DHI-NVR", "NVR")):
        return DeviceType.NVR
    if "DVR" in m or "XVR" in m or m.startswith(("DS-72", "DS-71", "DS-73", "DH-XVR", "DHI-XVR")):
        return DeviceType.DVR
    if channels > 1:
        return DeviceType.NVR
    return DeviceType.CAMERA
