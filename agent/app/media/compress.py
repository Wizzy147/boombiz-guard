"""Smaller copies of incident media for Low Bandwidth Mode (Phase 4 §96, §98).

Only the copy that goes to the cloud is shrunk — the evidence on this PC
stays full quality. Both functions fall back to the original bytes on any
failure, and only return the smaller copy if it really is smaller: a slow
upload is better than no upload.
"""

from __future__ import annotations

import subprocess
import sys

import cv2
import numpy as np

from ..config import settings

BELOW_NORMAL = 0x00004000


def shrink_snapshot(jpeg: bytes, max_width: int = 960, quality: int = 70) -> bytes:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    h, w = img.shape[:2]
    if w > max_width:
        img = cv2.resize(img, (max_width, max(1, int(h * max_width / w))), interpolation=cv2.INTER_AREA)
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return out.tobytes() if ok and len(out) < len(jpeg) else jpeg


def shrink_clip(mp4: bytes, height: int = 360, fps: int = 10, crf: int = 32, timeout_s: float = 60.0) -> bytes:
    """Re-encode to ≤360p, 10 fps — a 15 s clip lands well under 1 MB while
    people and hands stay recognisable. Same fragmented-MP4-in-RAM approach
    as clip.py: plaintext never touches disk."""
    args = [
        settings.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "mp4", "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-vf", f"scale=-2:'min({height},ih)',fps={fps}", "-threads", "1",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
    ]
    kwargs = {"creationflags": BELOW_NORMAL} if sys.platform == "win32" else {}
    try:
        proc = subprocess.run(args, input=mp4, capture_output=True, timeout=timeout_s, **kwargs)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return mp4
    if proc.returncode != 0 or not proc.stdout or len(proc.stdout) >= len(mp4):
        return mp4
    return proc.stdout
