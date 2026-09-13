"""The RTSP Digest answer must go back on the connection that issued the
nonce — the bug the simulated lab caught. This fake server behaves like
MediaMTX and most camera firmware: nonces are per-connection."""

import asyncio
import hashlib
import secrets

from app.adapters.base import Credentials
from app.streams.rtsp_probe import rtsp_describe

USER, PASSWORD, REALM = "admin", "Lab#2026", "ipcam"
SDP = "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"


async def _serve(state: dict):
    async def handle(reader, writer):
        nonce = None
        state["connections"] += 1
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                cseq = next(l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("cseq"))
                auth = next((l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("authorization")), None)
                uri = head.split(" ")[1]
                ok = False
                if auth and nonce:
                    p = dict(x.split("=", 1) for x in auth[7:].replace('"', "").split(", "))
                    ha1 = hashlib.md5(f"{USER}:{REALM}:{PASSWORD}".encode()).hexdigest()
                    ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
                    ok = p.get("nonce") == nonce and p.get("response") == hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
                if auth:
                    state["auth_attempts"] += 1
                if ok:
                    writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: {len(SDP)}\r\n\r\n{SDP}".encode())
                else:
                    nonce = secrets.token_hex(8)  # bound to THIS connection
                    writer.write(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: Digest realm="{REALM}", nonce="{nonce}"\r\n\r\n'.encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def test_digest_answer_uses_same_connection():
    state = {"connections": 0, "auth_attempts": 0}
    server = await _serve(state)
    port = server.sockets[0].getsockname()[1]
    async with server:
        r = await rtsp_describe(f"rtsp://127.0.0.1:{port}/stream1", Credentials(USER, PASSWORD))
    assert r.status == 200 and r.codec == "H264"
    assert state["connections"] == 1


async def test_wrong_password_is_one_attempt():
    state = {"connections": 0, "auth_attempts": 0}
    server = await _serve(state)
    port = server.sockets[0].getsockname()[1]
    async with server:
        r = await rtsp_describe(f"rtsp://127.0.0.1:{port}/stream1", Credentials(USER, "wrong"))
    assert r.status == 401
    assert state["auth_attempts"] == 1
