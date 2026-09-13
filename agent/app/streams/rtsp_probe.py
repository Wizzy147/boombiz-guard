"""A tiny RTSP client: OPTIONS (is there an RTSP server?) and DESCRIBE
(does this path exist, do these credentials open it, what codec is it?).

Used by the generic RTSP, Hikvision, Dahua and V380 adapters to confirm a
stream before FFmpeg is ever started. One DESCRIBE per candidate path, ONE
authentication attempt per path — a 401 after credentials were sent is an
answer, not a prompt to try something else (Phase 1 §9, §11).

The challenge and the answer travel on the SAME TCP connection. MediaMTX and
most IP-camera firmware bind the Digest nonce to the connection that issued
it, so answering on a fresh socket is rejected as a wrong password — which is
exactly the bug the lab caught before this was written this way.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..adapters.base import Credentials


@dataclass
class DescribeResult:
    status: int  # RTSP status code; 0 = no answer
    codec: str | None = None
    has_audio: bool = False
    auth_scheme: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def unauthorized(self) -> bool:
        return self.status == 401


_RTPMAP = re.compile(r"a=rtpmap:\d+\s+([A-Za-z0-9\-]+)/", re.I)
_NET_ERRORS = (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError)


def _codec_from_sdp(sdp: str) -> tuple[str | None, bool]:
    video = None
    audio = False
    section = None
    for line in sdp.splitlines():
        if line.startswith("m="):
            section = line[2:].split(" ", 1)[0]
        m = _RTPMAP.search(line)
        if m and section == "video" and video is None:
            enc = m.group(1).upper()
            video = {"H264": "H264", "H265": "H265", "HEVC": "H265", "JPEG": "MJPEG"}.get(enc, enc)
        if section == "audio":
            audio = True
    return video, audio


def _parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    scheme, _, rest = header.partition(" ")
    params = dict(re.findall(r'(\w+)="?([^",]*)"?', rest))
    return scheme.strip().lower(), params


def _pick_challenge(challenges: list[str]) -> str:
    """Prefer Digest over Basic when a server offers both."""
    for c in challenges:
        if c.lower().startswith("digest"):
            return c
    return challenges[0] if challenges else ""


def _auth_header(challenge: str, method: str, uri: str, creds: Credentials) -> str:
    scheme, p = _parse_challenge(challenge)
    if scheme == "basic":
        token = base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        return f"Basic {token}"
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    ha1 = hashlib.md5(f"{creds.username}:{realm}:{creds.password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return f'Digest username="{creds.username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"'


class _Conn:
    """One RTSP control connection, used for a request/response exchange or two."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float) -> None:
        self.reader, self.writer, self.timeout = reader, writer, timeout

    @classmethod
    async def open(cls, host: str, port: int, timeout: float) -> "_Conn":
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        return cls(reader, writer, timeout)

    async def send(self, request: str) -> tuple[int, dict[str, str], list[str], str]:
        """→ (status, headers, all WWW-Authenticate values, body)."""
        self.writer.write(request.encode())
        await self.writer.drain()
        head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), self.timeout)
        lines = head.decode(errors="replace").split("\r\n")
        m = re.match(r"RTSP/\d\.\d\s+(\d+)", lines[0])
        status = int(m.group(1)) if m else 0
        headers: dict[str, str] = {}
        challenges: list[str] = []
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "www-authenticate":
                    challenges.append(v)
                headers.setdefault(k, v)
        body = ""
        length = int(headers.get("content-length", "0") or 0)
        if length:
            body = (await asyncio.wait_for(self.reader.readexactly(length), self.timeout)).decode(errors="replace")
        return status, headers, challenges, body

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


async def rtsp_options(host: str, port: int = 554, timeout: float = 3.0) -> bool:
    """True when something on host:port speaks RTSP."""
    req = f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: BoombizGuard\r\n\r\n"
    try:
        conn = await _Conn.open(host, port, timeout)
    except _NET_ERRORS:
        return False
    try:
        status, *_ = await conn.send(req)
        return status > 0
    except _NET_ERRORS:
        return False
    finally:
        await conn.close()


def _describe_request(uri: str, cseq: int, auth: str | None) -> str:
    extra = f"Authorization: {auth}\r\n" if auth else ""
    return (
        f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\n"
        f"User-Agent: BoombizGuard\r\n{extra}\r\n"
    )


async def rtsp_describe(uri: str, creds: Credentials | None, timeout: float = 4.0) -> DescribeResult:
    """DESCRIBE `uri` (credential-free), answering at most ONE auth challenge.

    If the server hangs up after the challenge without our answer ever being
    read (some cameras send `Connection: close` on a 401), a fresh challenge is
    fetched on a new connection and the credentials are sent then — still
    once, because the first copy was never delivered.
    """
    p = urlparse(uri)
    host, port = p.hostname or "", p.port or 554

    async def challenge_then_answer(conn: _Conn) -> DescribeResult:
        status, _, challenges, body = await conn.send(_describe_request(uri, 1, None))
        scheme = None
        if status == 401 and creds:
            challenge = _pick_challenge(challenges)
            scheme = _parse_challenge(challenge)[0] if challenge else None
            status, _, _, body = await conn.send(
                _describe_request(uri, 2, _auth_header(challenge, "DESCRIBE", uri, creds))
            )
        codec, audio = _codec_from_sdp(body) if status == 200 else (None, False)
        return DescribeResult(status=status, codec=codec, has_audio=audio, auth_scheme=scheme)

    for attempt in range(2):
        try:
            conn = await _Conn.open(host, port, timeout)
        except _NET_ERRORS:
            return DescribeResult(status=0)
        try:
            return await challenge_then_answer(conn)
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            if attempt == 0 and creds:
                continue  # hung up mid-exchange: our answer was never read
            return DescribeResult(status=0)
        except _NET_ERRORS:
            return DescribeResult(status=0)
        finally:
            await conn.close()
    return DescribeResult(status=0)
