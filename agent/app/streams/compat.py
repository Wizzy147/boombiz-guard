"""Compatibility classification (PRD §8, Phase 1 §14).

Mandatory (all must pass or the camera is INCOMPATIBLE):
    authentication · video available · codec Guard can decode (H.264/H.265)
    · stable for the whole test · local operation · reconnects after close
Optional (a miss makes it LIMITED, never incompatible):
    substream · ONVIF · alarm/siren output · audio

No percentages: the result is a label plus the checklist that produced it,
which is what an installer can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..adapters.base import Compatibility
from .tester import StreamTestResult

DECODABLE = {"H264", "H265"}


@dataclass
class Check:
    key: str
    label: str
    passed: bool | None  # None = not applicable / not tested
    mandatory: bool
    note: str | None = None


@dataclass
class CompatReport:
    status: Compatibility
    checks: list[Check] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "checks": [c.__dict__ for c in self.checks],
        }


def classify(
    *,
    authenticated: bool,
    test: StreamTestResult | None,
    has_substream: bool,
    onvif: bool,
    alarm_output: bool,
    audio: bool,
    local: bool = True,
) -> CompatReport:
    if not authenticated:
        return CompatReport(Compatibility.AUTH_REQUIRED, summary="Enter the CCTV username and password to test this camera.")
    if test is None:
        return CompatReport(Compatibility.UNKNOWN, summary="Not tested yet.")

    codec_ok = (test.codec or "") in DECODABLE
    checks = [
        Check("auth", "Authentication", not test.auth_failed, True),
        Check("video", "Video available", test.connected and test.frames > 0, True),
        Check("codec", f"Codec ({test.codec or 'unknown'})", codec_ok, True,
              None if codec_ok else "Guard needs H.264 or H.265. Change the camera's encoding in its settings."),
        Check("stable", "Stable stream", test.stable, True,
              None if test.stable else f"{test.measured_fps or 0} fps measured over {test.seconds_run}s"),
        Check("local", "Local operation", local, True),
        Check("reconnect", "Automatic reconnect", bool(test.reconnect_ok), True),
        Check("substream", "Substream", has_substream, False,
              None if has_substream else "Guard will use the main stream, which uses more computer power."),
        Check("onvif", "ONVIF", onvif, False),
        Check("alarm", "Alarm / siren output", alarm_output, False),
        Check("audio", "Audio", audio, False),
    ]
    if not all(c.passed for c in checks if c.mandatory):
        failed = next(c for c in checks if c.mandatory and not c.passed)
        return CompatReport(
            Compatibility.INCOMPATIBLE, checks,
            summary=f"Not compatible: {failed.label.lower()} failed. {failed.note or ''}".strip(),
        )
    missing = [c.label for c in checks if not c.mandatory and not c.passed]
    if missing:
        return CompatReport(
            Compatibility.LIMITED, checks,
            summary="Guard AI can use this camera. Not available: " + ", ".join(missing) + ".",
        )
    return CompatReport(Compatibility.COMPATIBLE, checks, summary="Ready for Guard.")
