"""Incident clip: buffered JPEG frames → H.264 MP4, in memory (Phase 3 §17–19).

FFmpeg reads the frames on stdin and writes a FRAGMENTED MP4 to stdout
(`frag_keyframe+empty_moov` — a normal MP4 needs a seekable file), so the
plaintext clip exists only in RAM and is encrypted before it is written.

Frames arrive at irregular times (network jitter, AI back-pressure), so the
real average rate is passed as the input frame rate and the output is capped
at 15 s with `-t` as a second, independent guard on the hard maximum.

Runs at BELOW-NORMAL process priority on Windows and with one encoder
thread: the POS on the same PC wins (§63, §82).
"""

from __future__ import annotations

import subprocess
import sys

from ..buffer.rolling_buffer import MAX_CLIP_S, Frame
from ..config import settings

BELOW_NORMAL = 0x00004000  # Windows BELOW_NORMAL_PRIORITY_CLASS


class ClipError(RuntimeError):
    pass


def build_clip(frames: list[Frame], timeout_s: float = 60.0) -> tuple[bytes, float]:
    """→ (mp4 bytes, duration seconds). Raises ClipError."""
    if len(frames) < 2:
        raise ClipError("Not enough video was buffered to make a clip.")
    frames = sorted(frames, key=lambda f: f.ts)
    span = frames[-1].ts - frames[0].ts
    if span <= 0:
        raise ClipError("Buffered frames have no duration.")
    fps = max(1.0, min(30.0, (len(frames) - 1) / span))
    duration = min(MAX_CLIP_S, len(frames) / fps)
    args = [
        settings.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "image2pipe", "-framerate", f"{fps:.3f}", "-c:v", "mjpeg", "-i", "pipe:0",
        "-t", f"{MAX_CLIP_S:.1f}",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-threads", "1",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
    ]
    kwargs = {"creationflags": BELOW_NORMAL} if sys.platform == "win32" else {}
    try:
        proc = subprocess.run(args, input=b"".join(f.jpeg for f in frames), capture_output=True,
                              timeout=timeout_s, **kwargs)
    except FileNotFoundError:
        raise ClipError("FFmpeg is missing from this Guard installation.") from None
    except subprocess.TimeoutExpired:
        raise ClipError("Making the clip took too long.") from None
    if proc.returncode != 0 or not proc.stdout:
        raise ClipError("The clip could not be encoded.")
    return proc.stdout, round(duration, 2)
