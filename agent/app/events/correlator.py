"""Event correlation (Phase 2 §23–29) — detections become a few events worth
reading, and at most ONE "Possible unpaid exit" per person.

Per-track context:
    unresolved shelf interactions (with strength and time)
    whether they passed through a CASHIER zone after the interaction
    whether an exit/restricted/after-hours event was already raised

POSSIBLE_UNPAID_EXIT when a track enters an EXIT zone while it has an
unresolved interaction younger than the correlation window (5 min, §26).

Confidence (§29) — rule-based, never a percentage:
    strong unresolved + no cashier visit  → HIGH
    strong unresolved + cashier visit     → MEDIUM   (§28: possible payment)
    weak unresolved   + no cashier visit  → MEDIUM
    weak unresolved   + cashier visit     → LOW
The event is always called "Possible unpaid exit" — never theft.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..interactions.shelf import Interaction, ShelfEvent
from ..zones.engine import ZoneTransition, ZoneType
from .service import EventDraft


@dataclass
class CorrelatorConfig:
    correlation_window_s: float = 300.0  # §26
    after_hours_persist_s: float = 2.0   # §16
    # A quick grab-and-go reaches the door before the shelf "after" check has
    # settled (~1.7 s). An interaction that turns out unresolved this soon
    # after the same person entered the exit still counts.
    late_resolution_s: float = 15.0


@dataclass
class TrackContext:
    track_id: str
    first_seen: float
    unresolved: list[Interaction] = field(default_factory=list)
    cashier_visits: list[float] = field(default_factory=list)
    announced_person: bool = False
    exit_entered_at: float | None = None
    exit_zone_id: str | None = None
    concealed_at: float | None = None  # POSSIBLE_CONCEALMENT seen on this person


_UP = {"LOW": "MEDIUM", "MEDIUM": "MEDIUM", "HIGH": "HIGH"}


def with_concealment(conf: str, concealed: bool) -> str:
    """Concealment can raise LOW → MEDIUM and never higher on its own (§33):
    HIGH still needs strong shelf evidence and no cashier visit."""
    return _UP[conf] if concealed else conf


def confidence_for(strength: str | None, visited_cashier: bool) -> str:
    # UNVERIFIED = the shelf was blocked (crowd) so Guard never got to look.
    # "We couldn't check" must never read like evidence.
    if strength == "UNVERIFIED":
        return "LOW"
    strong = strength == "STRONG"
    if strong and not visited_cashier:
        return "HIGH"
    if strong or not visited_cashier:
        return "MEDIUM"
    return "LOW"


class Correlator:
    def __init__(self, camera_id: str, config: CorrelatorConfig | None = None) -> None:
        self.camera_id = camera_id
        self.cfg = config or CorrelatorConfig()
        self.ctx: dict[str, TrackContext] = {}

    def _c(self, tid: str, now: float) -> TrackContext:
        return self.ctx.setdefault(tid, TrackContext(tid, now))

    def process(
        self, *, now: float, active_tracks: dict[str, float], promoted: list[str], ended: list[str],
        transitions: list[ZoneTransition], shelf_events: list[ShelfEvent], store_open: bool,
        features: set[str], zone_of_track: dict[str, set[str]],
        concealments: list[tuple[str, str | None, str]] | None = None,
    ) -> list[EventDraft]:
        out: list[EventDraft] = []
        cam = self.camera_id

        # (track, shelf zone, region) from the concealment engine. ALWAYS LOW.
        for tid, shelf_id, region in concealments or []:
            if "concealment" not in features:
                continue
            c = self._c(tid, now)
            c.concealed_at = now
            out.append(EventDraft(cam, "POSSIBLE_CONCEALMENT", tid, shelf_id, "LOW", "LOW",
                                  metadata={"body_region": region, "experimental": True,
                                            "note": "A hand went from the shelf to the body. People also do "
                                                    "this for a phone or money — review, never accuse."}))

        for tid in promoted:
            c = self._c(tid, now)
            if not c.announced_person and "person" in features:
                c.announced_person = True
                out.append(EventDraft(cam, "PERSON_DETECTED", tid))

        for ev in shelf_events:
            it = ev.interaction
            c = self._c(it.track_id, now)
            if ev.kind == "SHELF_INTERACTION" and "shelf" in features:
                out.append(EventDraft(cam, "SHELF_INTERACTION", it.track_id, it.shelf_zone_id, "INFO",
                                      metadata={"interaction_id": it.id}, dedup_ttl=2.0))
            elif ev.kind == "UNRESOLVED_SHELF_INTERACTION" and "shelf" in features:
                c.unresolved.append(it)
                out.append(EventDraft(cam, "UNRESOLVED_SHELF_INTERACTION", it.track_id, it.shelf_zone_id, "LOW",
                                      "MEDIUM" if it.strength == "STRONG" else "LOW",
                                      metadata=it.to_dict(), dedup_ttl=1.0))
                # Already at the door by the time the shelf check settled.
                if ("exit" in features and c.exit_entered_at is not None
                        and now - c.exit_entered_at <= self.cfg.late_resolution_s):
                    out.append(self._unpaid_exit(c, [it], c.exit_zone_id, now, late=True))

        for tr in transitions:
            z = tr.zone
            c = self._c(tr.track_id, now)
            if tr.kind == "ENTRY":
                out.append(EventDraft(cam, "ZONE_ENTRY", tr.track_id, z.id, metadata={"zone_type": z.zone_type.value, "zone_name": z.name}, dedup_ttl=3.0))
                if z.zone_type == ZoneType.CASHIER:
                    c.cashier_visits.append(now)
                if z.zone_type in (ZoneType.RESTRICTED, ZoneType.STOCKROOM) and "restricted" in features:
                    out.append(EventDraft(cam, "RESTRICTED_ZONE_ENTRY", tr.track_id, z.id, "HIGH", "HIGH",
                                          metadata={"zone_name": z.name}))
                if z.zone_type == ZoneType.EXIT and "exit" in features:
                    c.exit_entered_at, c.exit_zone_id = now, z.id
                    out.append(EventDraft(cam, "EXIT_APPROACH", tr.track_id, z.id, "INFO"))
                    live = [i for i in c.unresolved if now - (i.ended_at or i.started_at) <= self.cfg.correlation_window_s]
                    c.unresolved = live
                    if live:
                        out.append(self._unpaid_exit(c, live, z.id, now))
            else:
                out.append(EventDraft(cam, "ZONE_EXIT", tr.track_id, z.id, metadata={"zone_type": z.zone_type.value}, dedup_ttl=3.0))

        # After-hours (§16): persist > N s, in a monitored area (any occupiable
        # zone; if the camera has no zones, anywhere in view).
        if not store_open and "after_hours" in features:
            for tid, first in active_tracks.items():
                if now - first >= self.cfg.after_hours_persist_s:
                    zones = zone_of_track.get(tid, set())
                    out.append(EventDraft(cam, "AFTER_HOURS_PERSON", tid, next(iter(zones), None), "CRITICAL", "HIGH",
                                          metadata={"visible_s": round(now - first, 1)}))

        for tid in ended:
            out.append(EventDraft(cam, "TRACK_LOST", tid))
            self.ctx.pop(tid, None)
        return out

    def _unpaid_exit(self, c: TrackContext, live: list[Interaction], zone_id: str | None, now: float,
                     late: bool = False) -> EventDraft:
        best = next((i for i in live if i.strength == "STRONG"), live[0])
        since = best.ended_at or best.started_at
        visited = any(t >= since for t in c.cashier_visits)
        concealed = c.concealed_at is not None and c.concealed_at >= best.started_at
        conf = with_concealment(confidence_for(best.strength, visited), concealed)
        return EventDraft(
            self.camera_id, "POSSIBLE_UNPAID_EXIT", c.track_id, zone_id,
            "HIGH" if conf != "LOW" else "LOW", conf,
            metadata={"interactions": [i.id for i in live], "shelf_zone_id": best.shelf_zone_id,
                      "cashier_visit": visited, "strength": best.strength, "resolved_after_exit": late,
                      "concealment_seen": concealed,
                      "note": "Review the clip. This is not an accusation of theft."},
        )
