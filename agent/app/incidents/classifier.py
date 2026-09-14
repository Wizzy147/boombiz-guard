"""Which AI events become incidents (Phase 3 §5–7, §14, §46–47). Configurable.

Anything not listed here stays an EVENT: PERSON_DETECTED, ZONE_ENTRY,
SHELF_INTERACTION, EXIT_APPROACH… are context for the incident timeline,
never an incident on their own. UNRESOLVED_SHELF_INTERACTION is a CANDIDATE
(§47): it waits for the exit correlation in Phase 2 to turn it into
POSSIBLE_UNPAID_EXIT.

POSSIBLE_CONCEALMENT: the PRD lists it as an incident type, but Guard's
concealment signal is always LOW and — by an earlier decision — must never
alert on its own. So by default it joins the timeline of that person's
unpaid-exit incident (raising its confidence one step in Phase 2) and does
NOT open an incident by itself. `creates_incident=True` below turns that
back on for pilots that want to see every one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IncidentRule:
    incident_type: str
    family: str             # grouping key part (§13)
    severity: str           # INFO | LOW | HIGH | CRITICAL (§7)
    window_s: float         # merge/dedup window (§14)
    title: str
    creates_incident: bool = True
    per_track: bool = True  # group by person; False = one per camera


RULES: dict[str, IncidentRule] = {
    "POSSIBLE_UNPAID_EXIT": IncidentRule("POSSIBLE_UNPAID_EXIT", "security", "HIGH", 60, "Possible unpaid exit"),
    # Same family as unpaid exit: swap then walk out is ONE incident for that person.
    "POSSIBLE_PRODUCT_REPLACEMENT": IncidentRule("POSSIBLE_PRODUCT_REPLACEMENT", "security", "HIGH", 60,
                                                 "Possible product swap at shelf"),
    "RESTRICTED_ZONE_ENTRY": IncidentRule("RESTRICTED_AREA_INCIDENT", "restricted", "HIGH", 30, "Restricted area entered"),
    "AFTER_HOURS_PERSON": IncidentRule("AFTER_HOURS_INTRUSION", "after_hours", "CRITICAL", 60,
                                       "Person inside after hours", per_track=False),
    "POSSIBLE_FIRE": IncidentRule("POSSIBLE_FIRE", "fire", "CRITICAL", 30, "Possible fire", per_track=False),
    "POSSIBLE_SMOKE": IncidentRule("POSSIBLE_SMOKE", "fire", "HIGH", 30, "Possible smoke", per_track=False),
    "POSSIBLE_CONCEALMENT": IncidentRule("POSSIBLE_CONCEALMENT", "security", "HIGH", 60,
                                         "Possible concealment (experimental)", creates_incident=False),
    # Camera damage (owner decision 2026-09-14): the owner, manager and security
    # hear at once — HIGH from the start, so it pops up, buzzes phones and goes
    # by WhatsApp. CAMERA_OFFLINE comes from the stream health loop (no video
    # for 30 s: smashed, cable cut, unplugged); CAMERA_TAMPERED from the AI
    # worker (video still arriving, but the lens is covered or turned away).
    "CAMERA_OFFLINE": IncidentRule("CAMERA_OFFLINE", "health", "HIGH", 1e9, "Camera stopped sending video",
                                   per_track=False),
    # 10-min window: a second tamper on the same camera later is a new incident.
    "CAMERA_TAMPERED": IncidentRule("CAMERA_TAMPERED", "health", "HIGH", 600, "Camera covered or turned away",
                                    per_track=False),
}

# Events that don't open an incident but are copied into an open incident's
# timeline when they belong to the same person/camera.
TIMELINE_EVENTS = {
    "PERSON_DETECTED", "ZONE_ENTRY", "SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION",
    "EXIT_APPROACH", "POSSIBLE_CONCEALMENT", "ZONE_EXIT", "POSSIBLE_PRODUCT_REPLACEMENT",
}

SEVERITY_RANK = {"INFO": 0, "LOW": 1, "HIGH": 2, "CRITICAL": 3}
MANUAL_TYPES = {"MANUAL_SECURITY_INCIDENT"}


def rule_for(event_type: str) -> IncidentRule | None:
    return RULES.get(event_type)


def at_least(severity: str, minimum: str | None) -> bool:
    return minimum is None or SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(minimum, 0)
