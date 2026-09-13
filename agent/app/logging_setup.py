"""Local logs with passwords redacted (Phase 1 deliverable).

The RedactingFilter sits on every HANDLER, not on a logger: a logger's
filters only see records created on that exact logger, so a secret logged by
httpx or uvicorn would sail past a root-logger filter. Handler filters see
everything that is actually written.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import settings
from .security.redact import RedactingFilter


def setup_logging(level: int = logging.INFO) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    # A Windows console defaults to cp1252; one "—" or "→" in a message would
    # otherwise print a "--- Logging error ---" traceback instead of the line.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = RedactingFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    console = logging.StreamHandler()
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in (file_handler, console):
        h.setFormatter(fmt)
        h.addFilter(redactor)
        root.addHandler(h)
    # Uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
