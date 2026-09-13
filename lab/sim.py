"""Simulated CCTV lab — a stand-in shop network on loopback.

    agent\\.venv\\Scripts\\python lab\\sim.py

Until real Hikvision/Dahua/V380 hardware arrives, this is what Phase 1 is
proven against. Every piece speaks the real protocol, so the agent runs its
real code paths — only the devices are fake:

  127.0.0.2  "Hikvision DVR"   ISAPI (HTTP Digest) on :8000, RTSP on :8554
                               ch1 Entrance, ch2 Product Shelves (main+sub),
                               ch3 Rear Door listed but OFFLINE
  127.0.0.3  "ONVIF camera"    WS-Discovery (UDP :3702), ONVIF SOAP on :8000
                               with WS-Security PasswordDigest, RTSP on :554
  127.0.0.4  "V380 camera"     only its cloud/P2P port :8800 — no local stream
  127.0.0.5  "generic camera"  RTSP only, on :554 (/stream1, /stream2)

RTSP is MediaMTX (lab/bin, Digest auth); video is FFmpeg test patterns.
Login for every device: admin / Lab#2026

Control API on 127.0.0.1:7499:
  GET  /stats                    failed-login counts per device (lockout check)
  POST /drop/{stream}?seconds=N  kill a camera's feed for N s (reconnect test)

Run the agent against it with lab\\run-agent.cmd.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import signal
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

LAB = Path(__file__).resolve().parent
RUN = LAB / "run"
MEDIAMTX = LAB / "bin" / "mediamtx.exe"
FFMPEG = os.environ.get("GUARD_FFMPEG", "ffmpeg")

USER, PASSWORD = "admin", "Lab#2026"
PUB_USER, PUB_PASS = "publisher", "pubpass"

HIK_IP, ONVIF_IP, V380_IP, GENERIC_IP = "127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"
ONVIF_UUID = "urn:uuid:5f5a69c2-e0ae-504f-829b-00000000lab3"

FAILED_LOGINS: dict[str, int] = {"hikvision": 0, "onvif": 0}
LOCK_AFTER = 5  # like a real recorder: 5 bad logins and the account locks

# (name, rtsp host:port, path, size, fps, pattern)
STREAMS = [
    ("hik-ch1-main", f"{HIK_IP}:8554", "Streaming/Channels/101", "960x540", 12, "testsrc2"),
    ("hik-ch1-sub", f"{HIK_IP}:8554", "Streaming/Channels/102", "480x270", 8, "testsrc2"),
    # Phase 2: "Product Shelves" plays real people (a Pexels shop clip, looped)
    # so the AI pipeline sees actual shoppers. Falls back to a test pattern if
    # lab/clips is empty. The sub stream is what Guard analyses.
    ("hik-ch2-main", f"{HIK_IP}:8554", "Streaming/Channels/201", "960x540", 12, "clip:shop-4750083.mp4"),
    ("hik-ch2-sub", f"{HIK_IP}:8554", "Streaming/Channels/202", "640x360", 10, "clip:shop-4750083.mp4"),
    ("onvif-main", f"{ONVIF_IP}:554", "onvif/main", "960x540", 12, "rgbtestsrc"),
    ("onvif-sub", f"{ONVIF_IP}:554", "onvif/sub", "480x270", 8, "rgbtestsrc"),
    ("generic-main", f"{GENERIC_IP}:554", "stream1", "960x540", 12, "yuvtestsrc"),
    ("generic-sub", f"{GENERIC_IP}:554", "stream2", "480x270", 8, "yuvtestsrc"),
]
DROPPED: dict[str, float] = {}


# ── MediaMTX ──────────────────────────────────────────────────────────
def mediamtx_config(listen: str) -> str:
    return textwrap.dedent(f"""\
        logLevel: warn
        authMethod: internal
        authInternalUsers:
          - user: {PUB_USER}
            pass: "{PUB_PASS}"
            permissions: [{{action: publish}}]
          - user: {USER}
            pass: "{PASSWORD}"
            permissions: [{{action: read}}]
        rtsp: true
        rtspTransports: [tcp]
        rtspAddress: {listen}
        rtspAuthMethods: [digest]
        rtmp: false
        hls: false
        webrtc: false
        srt: false
        # MediaMTX 1.21 added Media-over-QUIC, on by default at :8892 — three
        # instances would fight over it.
        moq: false
        api: false
        metrics: false
        pprof: false
        playback: false
        paths:
          all_others:
        """)


async def run_mediamtx(listen: str, name: str) -> None:
    RUN.mkdir(exist_ok=True)
    cfg = RUN / f"mediamtx-{name}.yml"
    cfg.write_text(mediamtx_config(listen), encoding="utf-8")
    while True:
        # cwd=RUN: MediaMTX writes auto.crt/auto.key (a private key) into its
        # working directory; keep them in the git-ignored run folder.
        proc = await asyncio.create_subprocess_exec(str(MEDIAMTX), str(cfg), cwd=str(RUN))
        await proc.wait()
        print(f"[lab] mediamtx {name} exited ({proc.returncode}); restarting")
        await asyncio.sleep(2)


async def run_publisher(name: str, hostport: str, path: str, size: str, fps: int, pattern: str) -> None:
    await asyncio.sleep(2)  # let MediaMTX bind first
    url = f"rtsp://{PUB_USER}:{PUB_PASS}@{hostport}/{path}"
    while True:
        until = DROPPED.get(name, 0)
        now = asyncio.get_running_loop().time()
        if until > now:
            await asyncio.sleep(until - now)
            continue
        if pattern.startswith("clip:") and (LAB / "clips" / pattern[5:]).exists():
            w, h = size.split("x")
            source = ["-stream_loop", "-1", "-re", "-i", str(LAB / "clips" / pattern[5:]),
                      "-vf", f"scale={w}:{h},fps={fps}", "-an"]
        else:
            pat = "smptebars" if pattern.startswith("clip:") else pattern
            source = ["-re", "-f", "lavfi", "-i", f"{pat}=size={size}:rate={fps}"]
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-loglevel", "error", *source,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-g", str(fps * 2), "-pix_fmt", "yuv420p",
            "-f", "rtsp", "-rtsp_transport", "tcp", url,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        PUBLISHERS[name] = proc
        await proc.wait()
        PUBLISHERS.pop(name, None)
        await asyncio.sleep(1)


PUBLISHERS: dict[str, asyncio.subprocess.Process] = {}


# ── HTTP Digest (server side, qop=auth) ───────────────────────────────
NONCES: set[str] = set()


def digest_ok(request: Request, realm: str) -> bool:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("digest "):
        return False
    p = dict(re.findall(r'(\w+)="?([^",]*)"?', h[7:]))
    if p.get("username") != USER or p.get("nonce") not in NONCES:
        return False
    ha1 = hashlib.md5(f"{USER}:{realm}:{PASSWORD}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{request.method}:{p.get('uri', '')}".encode()).hexdigest()
    if p.get("qop"):
        expect = hashlib.md5(f"{ha1}:{p['nonce']}:{p.get('nc')}:{p.get('cnonce')}:{p['qop']}:{ha2}".encode()).hexdigest()
    else:
        expect = hashlib.md5(f"{ha1}:{p['nonce']}:{ha2}".encode()).hexdigest()
    return secrets.compare_digest(expect, p.get("response", ""))


def challenge(realm: str, body: str = "") -> Response:
    nonce = secrets.token_hex(16)
    NONCES.add(nonce)
    return Response(body, status_code=401, headers={
        "WWW-Authenticate": f'Digest realm="{realm}", qop="auth", nonce="{nonce}", algorithm=MD5',
        "Server": "App-webs/",
    })


# ── "Hikvision DVR" (ISAPI) ───────────────────────────────────────────
HIK_REALM = "DS-7208HQHI-K1"
hik = FastAPI()


@hik.middleware("http")
async def hik_auth(request: Request, call_next):  # noqa: ANN001, ANN201
    if FAILED_LOGINS["hikvision"] >= LOCK_AFTER:
        return challenge(HIK_REALM, "<userCheck><statusString>user locked</statusString></userCheck>")
    if not request.headers.get("authorization"):
        return challenge(HIK_REALM)
    if not digest_ok(request, HIK_REALM):
        FAILED_LOGINS["hikvision"] += 1
        return challenge(HIK_REALM)
    return await call_next(request)


@hik.get("/ISAPI/System/deviceInfo")
async def hik_info() -> Response:
    return Response(textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <DeviceInfo version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
          <deviceName>Lab Shop DVR</deviceName><model>DS-7208HQHI-K1</model>
          <serialNumber>DS-7208HQHI-K10820200101CCRRLAB0001</serialNumber>
          <firmwareVersion>V4.30.005</firmwareVersion><deviceType>DVR</deviceType>
          <alarmOutNum>1</alarmOutNum>
        </DeviceInfo>"""), media_type="application/xml")


@hik.get("/ISAPI/ContentMgmt/InputProxy/channels/status")
async def hik_channels() -> Response:
    rows = [(1, "Entrance", "true"), (2, "Product Shelves", "true"), (3, "Rear Door", "false")]
    items = "".join(
        f"<InputProxyChannelStatus><id>{i}</id><name>{n}</name><online>{o}</online></InputProxyChannelStatus>"
        for i, n, o in rows
    )
    return Response(
        f'<?xml version="1.0"?><InputProxyChannelStatusList xmlns="http://www.hikvision.com/ver20/XMLSchema">{items}</InputProxyChannelStatusList>',
        media_type="application/xml",
    )


# ── "ONVIF camera" ────────────────────────────────────────────────────
onvif = FastAPI()
SOAP = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>{}</s:Body></s:Envelope>')
FAULT_AUTH = ('<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized</s:Value>'
              '</s:Subcode></s:Code><s:Reason><s:Text xml:lang="en">Sender not Authorized</s:Text></s:Reason></s:Fault>')


def wsse_ok(xml: str) -> bool:
    def grab(tag: str) -> str | None:
        m = re.search(rf"<(?:\w+:)?{tag}\b[^>]*>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
        return m.group(1).strip() if m else None

    user, digest, nonce, created = grab("Username"), grab("Password"), grab("Nonce"), grab("Created")
    if user != USER or not (digest and nonce and created):
        return False
    expect = base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode() + PASSWORD.encode()).digest()).decode()
    return secrets.compare_digest(expect, digest)


def soap(body: str, status: int = 200) -> Response:
    return Response(SOAP.format(body), status_code=status, media_type="application/soap+xml")


def profile(token: str, name: str, w: int, h: int, fps: int) -> str:
    return (f'<trt:Profiles token="{token}" fixed="true"><tt:Name>{name}</tt:Name>'
            f'<tt:VideoSourceConfiguration token="vsc1"><tt:Name>Lab Dome</tt:Name><tt:SourceToken>vs1</tt:SourceToken></tt:VideoSourceConfiguration>'
            f'<tt:VideoEncoderConfiguration token="vec_{token}"><tt:Name>{name}</tt:Name><tt:Encoding>H264</tt:Encoding>'
            f'<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>'
            f'<tt:RateControl><tt:FrameRateLimit>{fps}</tt:FrameRateLimit></tt:RateControl></tt:VideoEncoderConfiguration></trt:Profiles>')


@onvif.post("/onvif/{service}")
async def onvif_service(service: str, request: Request) -> Response:
    xml = (await request.body()).decode(errors="replace")
    op = re.search(r"<s:Body>\s*<(?:\w+:)?(\w+)", xml)
    op = op.group(1) if op else ""
    if op == "GetSystemDateAndTime":
        n = datetime.now(timezone.utc)
        return soap(f"<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
                    f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
                    f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date>"
                    f"</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
    if FAILED_LOGINS["onvif"] >= LOCK_AFTER or not wsse_ok(xml):
        if FAILED_LOGINS["onvif"] < LOCK_AFTER:
            FAILED_LOGINS["onvif"] += 1
        return soap(FAULT_AUTH, 400)
    if op == "GetDeviceInformation":
        return soap("<tds:GetDeviceInformationResponse><tds:Manufacturer>LabVision</tds:Manufacturer>"
                    "<tds:Model>LAB-IPC-2MP</tds:Model><tds:FirmwareVersion>1.0.7</tds:FirmwareVersion>"
                    "<tds:SerialNumber>LAB0003</tds:SerialNumber><tds:HardwareId>HW3</tds:HardwareId>"
                    "</tds:GetDeviceInformationResponse>")
    if op == "GetCapabilities":
        return soap(f"<tds:GetCapabilitiesResponse><tds:Capabilities>"
                    f"<tt:Media><tt:XAddr>http://{ONVIF_IP}:8000/onvif/media_service</tt:XAddr></tt:Media>"
                    f"</tds:Capabilities></tds:GetCapabilitiesResponse>")
    if op == "GetProfiles":
        return soap("<trt:GetProfilesResponse>" + profile("p_main", "MainStream", 960, 540, 12)
                    + profile("p_sub", "SubStream", 480, 270, 8) + "</trt:GetProfilesResponse>")
    if op == "GetStreamUri":
        path = "onvif/sub" if "p_sub" in xml else "onvif/main"
        return soap(f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://{ONVIF_IP}:554/{path}</tt:Uri>"
                    f"</trt:MediaUri></trt:GetStreamUriResponse>")
    return soap("<s:Fault><s:Reason><s:Text>Not implemented</s:Text></s:Reason></s:Fault>", 400)


class WsDiscovery(asyncio.DatagramProtocol):
    def connection_made(self, transport) -> None:  # noqa: ANN001
        self.t = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        text = data.decode(errors="replace")
        if "Probe" not in text:
            return
        rel = re.search(r"MessageID>(.*?)<", text)
        reply = (
            '<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
            f"<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID><w:RelatesTo>{rel.group(1) if rel else ''}</w:RelatesTo></e:Header>"
            "<e:Body><d:ProbeMatches><d:ProbeMatch>"
            f"<w:EndpointReference><w:Address>{ONVIF_UUID}</w:Address></w:EndpointReference>"
            "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
            "<d:Scopes>onvif://www.onvif.org/name/Lab%20Dome onvif://www.onvif.org/hardware/LAB-IPC-2MP "
            "onvif://www.onvif.org/type/video_encoder</d:Scopes>"
            f"<d:XAddrs>http://{ONVIF_IP}:8000/onvif/device_service</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>"
            "</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>"
        )
        self.t.sendto(reply.encode(), addr)


# ── control ───────────────────────────────────────────────────────────
control = FastAPI()


@control.get("/stats")
async def stats() -> JSONResponse:
    return JSONResponse({"failed_logins": FAILED_LOGINS, "publishers": sorted(PUBLISHERS), "dropped": list(DROPPED)})


@control.post("/drop/{name}")
async def drop(name: str, seconds: float = 15) -> PlainTextResponse:
    DROPPED[name] = asyncio.get_running_loop().time() + seconds
    proc = PUBLISHERS.get(name)
    if proc and proc.returncode is None:
        proc.kill()
    return PlainTextResponse(f"dropped {name} for {seconds}s")


@control.post("/reset")
async def reset() -> PlainTextResponse:
    for k in FAILED_LOGINS:
        FAILED_LOGINS[k] = 0
    return PlainTextResponse("ok")


async def serve(app: FastAPI, host: str, port: int) -> None:
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    await uvicorn.Server(cfg).serve()


async def v380_p2p(reader, writer) -> None:  # noqa: ANN001
    writer.close()


async def main() -> None:
    if not MEDIAMTX.exists():
        sys.exit("lab/bin/mediamtx.exe is missing — see lab/README.md")
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(WsDiscovery, local_addr=(ONVIF_IP, 3702))
    await asyncio.start_server(v380_p2p, V380_IP, 8800)
    tasks = [
        run_mediamtx(f"{HIK_IP}:8554", "hik"),
        run_mediamtx(f"{ONVIF_IP}:554", "onvif"),
        run_mediamtx(f"{GENERIC_IP}:554", "generic"),
        serve(hik, HIK_IP, 8000),
        serve(onvif, ONVIF_IP, 8000),
        serve(control, "127.0.0.1", 7499),
        *(run_publisher(*s) for s in STREAMS),
    ]
    print(f"[lab] up — devices on {HIK_IP}, {ONVIF_IP}, {V380_IP}, {GENERIC_IP}; login {USER} / {PASSWORD}")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        subprocess.run(["taskkill", "/F", "/IM", "mediamtx.exe"], capture_output=True)
