"""Agent settings. Environment variables override; nothing here is a secret."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    # A Windows Service has no user profile worth using, so ProgramData.
    base = os.environ.get("PROGRAMDATA")
    if base and os.name == "nt" and not os.environ.get("GUARD_DEV"):
        return Path(base) / "Boombiz Guard"
    return Path(__file__).resolve().parents[1] / "data"


def _tool(name: str) -> str:
    """ffmpeg/ffprobe: GUARD_FFMPEG/GUARD_FFPROBE win; the installed agent uses
    the copies shipped beside it; otherwise whatever is on PATH."""
    env = os.environ.get(f"GUARD_{name.upper()}")
    if env:
        return env
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).parent / "ffmpeg" / f"{name}.exe"
        if bundled.exists():
            return str(bundled)
    return name


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("GUARD_DATA_DIR") or _default_data_dir()))
    # Phase 1 §27: the setup API is localhost-only by default. Binding anything
    # wider is an explicit, logged decision (GUARD_BIND_HOST).
    bind_host: str = os.environ.get("GUARD_BIND_HOST", "127.0.0.1")
    port: int = int(os.environ.get("GUARD_PORT", "7480"))
    # Guard Basic commercial limit (PRD §11): cameras analysed at once.
    max_guard_cameras: int = int(os.environ.get("GUARD_MAX_CAMERAS", "2"))
    discovery_timeout_s: float = float(os.environ.get("GUARD_DISCOVERY_TIMEOUT", "4"))
    # Phase 1 §13: at least 20–30 s before a stream may be called stable.
    stream_test_seconds: int = int(os.environ.get("GUARD_STREAM_TEST_SECONDS", "20"))
    ffmpeg: str = field(default_factory=lambda: _tool("ffmpeg"))
    ffprobe: str = field(default_factory=lambda: _tool("ffprobe"))
    # Extra RTSP port list checked during a scan (standard + common OEM).
    rtsp_ports: tuple[int, ...] = (554, 8554, 10554)
    http_ports: tuple[int, ...] = (80, 8000, 8080, 443)

    # Phase 2: which person model (see app/ai/model_manager.py MANIFEST).
    person_model: str = os.environ.get("GUARD_PERSON_MODEL", "person-nano")
    person_threshold: float = float(os.environ.get("GUARD_PERSON_THRESHOLD", "0.5"))
    pose_model: str = os.environ.get("GUARD_POSE_MODEL", "pose-rtm-t")
    ai_max_fps: float = float(os.environ.get("GUARD_AI_MAX_FPS", "10"))

    @property
    def models_dir(self) -> Path:
        override = os.environ.get("GUARD_MODELS_DIR")
        if override:
            return Path(override)
        installed = self.data_dir / "models"
        return installed if installed.exists() else Path(__file__).resolve().parents[1] / "models"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "guard.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


settings = Settings()
