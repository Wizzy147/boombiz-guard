"""Scrub CCTV secrets out of anything that could be written down.

PRD §43 / Phase 1 §6: logs, errors and API responses must never carry a
password, an Authorization header, or an RTSP URL with credentials in it.

Every log record passes through `RedactingFilter`, and every string that
leaves the agent in an error message passes through `redact()`. Redaction is
by pattern, so it also catches secrets that arrive inside third-party
exception text (FFmpeg, httpx) — which is where they usually leak from.
"""

from __future__ import annotations

import logging
import re

MASK = "***"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # scheme://user:pass@host  → scheme://***:***@host  (rtsp, http, https, rtmp …)
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]*@"), rf"\1{MASK}:{MASK}@"),
    # scheme://user@host (no password, username alone is still identifying)
    (re.compile(r"(?i)\b(rtsp://)[^\s/@:]+@"), rf"\1{MASK}@"),
    # Authorization: Basic xxx / Digest a="..", response=".." / Bearer xxx —
    # the WHOLE header value: a Digest header's secret parts come after commas.
    (re.compile(r"(?i)(authorization\s*[:=]\s*)[^\r\n]+"), rf"\1{MASK}"),
    # password=..., pwd:..., "password": "..."
    (
        re.compile(r"(?i)([\"']?(?:password|passwd|pwd|pass)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;}]+)"),
        rf"\1{MASK}",
    ),
    # WS-Security UsernameToken bodies in ONVIF SOAP
    (re.compile(r"(?is)(<(?:\w+:)?Password\b[^>]*>).*?(</(?:\w+:)?Password>)"), rf"\1{MASK}\2"),
    (re.compile(r"(?is)(<(?:\w+:)?Nonce\b[^>]*>).*?(</(?:\w+:)?Nonce>)"), rf"\1{MASK}\2"),
]


def redact(text: object) -> str:
    """Return `text` as a string with every known secret shape masked."""
    s = str(text)
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


class RedactingFilter(logging.Filter):
    """Rewrites each record's final message before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad %-format must not take logging down
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True
