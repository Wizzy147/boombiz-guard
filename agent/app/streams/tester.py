"""Real stream test (Phase 1 §13): FFmpeg is the transport, not OpenCV.

1. ffprobe — codec, resolution, nominal FPS, connect time.
2. ffmpeg decode for N seconds (default 20) to a null sink, reading
   `-progress` — measured FPS, decoded frames, dropped frames, decode errors,
   whether it ran the full duration.
3. A second, short connection — proves the stream comes back after being
   closed (the "automatic reconnect" check).

FFmpeg's stderr can echo the URL, credentials included — every line that is
kept is passed through redact() first.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field

from ..config import settings
from ..security.redact import redact

RTSP_TIMEOUT_US = "5000000"  # 5 s socket timeout, in microseconds


@dataclass
class StreamTestResult:
    connected: bool = False
    connect_ms: int | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    nominal_fps: float | None = None
    measured_fps: float | None = None
    frames: int = 0
    dropped_frames: int = 0
    decode_errors: int = 0
    seconds_requested: int = 0
    seconds_run: float = 0.0
    reconnect_ok: bool | None = None
    auth_failed: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        if not self.connected or self.frames == 0:
            return False
        if self.seconds_run < self.seconds_requested * 0.9:
            return False
        if self.nominal_fps and self.measured_fps is not None and self.measured_fps < self.nominal_fps * 0.7:
            return False
        return self.decode_errors <= max(3, self.frames // 100)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stable"] = self.stable
        return d


def _fps(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    if "/" in rate:
        n, d = rate.split("/", 1)
        return round(float(n) / float(d), 2) if float(d) else None
    return float(rate)


def _codec(name: str | None) -> str | None:
    if not name:
        return None
    return {"h264": "H264", "hevc": "H265", "h265": "H265", "mjpeg": "MJPEG"}.get(name.lower(), name.upper())


def _is_auth_error(text: str) -> bool:
    t = text.lower()
    return "401" in t or "unauthorized" in t


async def ffprobe(url: str, timeout: float = 12.0) -> tuple[dict | None, str]:
    proc = await asyncio.create_subprocess_exec(
        settings.ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US,
        "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", "-i", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None, "The camera took too long to answer."
    if proc.returncode != 0:
        return None, redact(err.decode(errors="replace").strip()[-400:])
    try:
        streams = json.loads(out or b"{}").get("streams") or []
    except json.JSONDecodeError:
        return None, "Unreadable reply from ffprobe."
    return (streams[0] if streams else None), ""


async def decode_run(url: str, seconds: int) -> tuple[int, int, float, int, list[str]]:
    """→ (frames, dropped, seconds_run, decode_errors, error_lines)."""
    proc = await asyncio.create_subprocess_exec(
        settings.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US, "-i", url,
        "-t", str(seconds), "-an", "-f", "null", "-", "-progress", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    frames = dropped = 0
    run_s = 0.0
    errors: list[str] = []

    async def read_progress() -> None:
        nonlocal frames, dropped, run_s
        assert proc.stdout
        async for raw in proc.stdout:
            k, _, v = raw.decode(errors="replace").strip().partition("=")
            if k == "frame" and v.isdigit():
                frames = int(v)
            elif k == "drop_frames" and v.isdigit():
                dropped = int(v)
            elif k == "out_time_us" and v.lstrip("-").isdigit():
                run_s = max(run_s, int(v) / 1_000_000)

    async def read_errors() -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                errors.append(redact(line)[:300])

    try:
        await asyncio.wait_for(asyncio.gather(read_progress(), read_errors(), proc.wait()), seconds + 20)
    except asyncio.TimeoutError:
        proc.kill()
        errors.append("The stream stopped responding during the test.")
    return frames, dropped, run_s, len(errors), errors[-10:]


async def test_stream(url_with_creds: str, seconds: int | None = None) -> StreamTestResult:
    seconds = seconds or settings.stream_test_seconds
    res = StreamTestResult(seconds_requested=seconds)
    t0 = time.perf_counter()
    info, err = await ffprobe(url_with_creds)
    if not info:
        res.auth_failed = _is_auth_error(err)
        res.errors.append("The username or password was not accepted." if res.auth_failed else (err or "No video stream found."))
        return res
    res.connected = True
    res.connect_ms = int((time.perf_counter() - t0) * 1000)
    res.codec = _codec(info.get("codec_name"))
    res.width, res.height = info.get("width"), info.get("height")
    res.nominal_fps = _fps(info.get("avg_frame_rate")) or _fps(info.get("r_frame_rate"))

    frames, dropped, run_s, n_err, lines = await decode_run(url_with_creds, seconds)
    res.frames, res.dropped_frames, res.seconds_run, res.decode_errors = frames, dropped, round(run_s, 2), n_err
    res.errors += lines
    if run_s > 0:
        res.measured_fps = round(frames / run_s, 2)

    again, _ = await ffprobe(url_with_creds)
    res.reconnect_ok = again is not None
    return res
