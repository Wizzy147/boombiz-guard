"""Who may do what with an incident (Phase 3 §35).

| Action               | Owner | Manager | Security            | Installer |
|----------------------|-------|---------|---------------------|-----------|
| view / play clip     |  ✓    |   ✓     |  ✓                  |  ✓        |
| acknowledge          |  ✓    |   ✓     |  ✓                  |           |
| confirm / false alert|  ✓    |   ✓     |  limited: not CRITICAL; a manager can overrule |
| resolve / escalate   |  ✓    |   ✓     |  escalate only      |           |
| keep evidence        |  ✓    |   ✓     |                     |           |
| export               |  ✓    |   ✓     |                     |           |
| delete               |  ✓    |         |                     |           |
| retention, users     |  ✓    |         |                     |           |
| alarms (config/test) |  ✓    |   ✓     |                     |  ✓        |
| manual incident      |  ✓    |   ✓     |  ✓                  |           |

The installer (setup token) can configure and test, but cannot review:
review decisions must be attributable to a person who works there.
"""

from __future__ import annotations

OWNER, MANAGER, SECURITY, INSTALLER = "OWNER", "MANAGER", "SECURITY", "INSTALLER"
PEOPLE_ROLES = {OWNER, MANAGER, SECURITY}

_ALLOW: dict[str, set[str]] = {
    "view": {OWNER, MANAGER, SECURITY, INSTALLER},
    "acknowledge": {OWNER, MANAGER, SECURITY},
    "confirm": {OWNER, MANAGER, SECURITY},
    "false_alert": {OWNER, MANAGER, SECURITY},
    "resolve": {OWNER, MANAGER},
    "escalate": {OWNER, MANAGER, SECURITY},
    "reopen": {OWNER, MANAGER},
    "keep": {OWNER, MANAGER},
    "export": {OWNER, MANAGER},
    "delete": {OWNER},
    "configure_retention": {OWNER},
    "manage_users": {OWNER},
    "configure_alarms": {OWNER, MANAGER, INSTALLER},
    "manual_incident": {OWNER, MANAGER, SECURITY},
}

# Security staff's "limited" review (§35): not on CRITICAL incidents.
_SECURITY_LIMITED = {"confirm", "false_alert"}


class Forbidden(Exception):
    pass


def can(role: str, action: str, severity: str | None = None) -> bool:
    if role not in _ALLOW.get(action, set()):
        return False
    if role == SECURITY and action in _SECURITY_LIMITED and severity == "CRITICAL":
        return False
    return True


def require(role: str, action: str, severity: str | None = None) -> None:
    if not can(role, action, severity):
        raise Forbidden("Your role can't do that. Ask the shop owner or a manager.")
