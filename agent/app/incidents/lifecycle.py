"""Incident status lifecycle (Phase 3 §8, §36–39).

The AI never moves an incident past UNREVIEWED — only a person confirms,
dismisses or resolves (§37). ARCHIVED is set by retention, not by people.
"""

from __future__ import annotations

UNREVIEWED, ACKNOWLEDGED, CONFIRMED, FALSE_ALERT = "UNREVIEWED", "ACKNOWLEDGED", "CONFIRMED", "FALSE_ALERT"
RESOLVED, ESCALATED, ARCHIVED = "RESOLVED", "ESCALATED", "ARCHIVED"

TRANSITIONS: dict[str, set[str]] = {
    UNREVIEWED: {ACKNOWLEDGED, CONFIRMED, FALSE_ALERT, ESCALATED, RESOLVED},
    ACKNOWLEDGED: {CONFIRMED, FALSE_ALERT, ESCALATED, RESOLVED},
    ESCALATED: {CONFIRMED, FALSE_ALERT, RESOLVED},
    CONFIRMED: {RESOLVED, ESCALATED},
    FALSE_ALERT: {UNREVIEWED},   # reopen (owner/manager)
    RESOLVED: {UNREVIEWED},      # reopen
    ARCHIVED: set(),
}

ACTION_TO_STATUS = {
    "acknowledge": ACKNOWLEDGED, "confirm": CONFIRMED, "false_alert": FALSE_ALERT,
    "resolve": RESOLVED, "escalate": ESCALATED, "reopen": UNREVIEWED,
}

FALSE_ALERT_REASONS = {
    "CUSTOMER_PAID": "Customer paid",
    "STAFF_ACTIVITY": "Staff activity",
    "ITEM_RETURNED": "Item returned",
    "CAMERA_ANGLE": "Camera angle issue",
    "ZONE_CONFIG": "Zone configured incorrectly",
    "AI_MISTAKE": "AI mistake",
    "OTHER": "Other",
}


class TransitionError(Exception):
    pass


def check(current: str, action: str) -> str:
    target = ACTION_TO_STATUS.get(action)
    if target is None:
        raise TransitionError("Unknown action.")
    if current == target and action == "acknowledge":
        return target  # acknowledging twice is harmless
    if target not in TRANSITIONS.get(current, set()):
        raise TransitionError(f"This incident is {current.replace('_', ' ').lower()} and can't be moved to "
                              f"{target.replace('_', ' ').lower()}.")
    return target
