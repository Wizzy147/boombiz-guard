"""Business hours (Phase 2 §16). No schedule rows → treated as always open,
so after-hours alerts stay silent until an installer sets hours."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass
class DayHours:
    day_of_week: int  # 0 = Monday
    opens_at: str | None
    closes_at: str | None
    closed: bool = False


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def validate_hhmm(s: str | None) -> str | None:
    if s is None or s == "":
        return None
    try:
        _t(s)
    except (ValueError, AttributeError):
        raise ValueError("Times must look like 08:00.") from None
    return s


def is_open(days: list[DayHours], now: datetime) -> bool:
    if not days:
        return True
    today = next((d for d in days if d.day_of_week == now.weekday()), None)
    if today is None or today.closed or not today.opens_at or not today.closes_at:
        return False if today else True
    o, c, t = _t(today.opens_at), _t(today.closes_at), now.time()
    if o <= c:
        return o <= t < c
    return t >= o or t < c  # past-midnight hours, e.g. 18:00–02:00
