"""Product swap at the shelf (shelf replacement) — deterministic synthetic shelf."""

from datetime import datetime, timezone

import numpy as np

from app.events.correlator import Correlator
from app.incidents.classifier import RULES
from app.interactions.shelf import ShelfInteractionEngine
from app.zones.engine import ZoneDef, ZoneTransition, ZoneType
from app.zones.geometry import BBox

SQUARE = [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)]
# A textured shelf back (like wood or a patterned shelf liner).
_TEX = np.random.default_rng(7).integers(60, 160, (200, 200, 3), dtype=np.uint8)
PERSON = BBox(0.3, 0.1, 0.6, 0.9)


def frame(item: str | None, hand: bool = False) -> np.ndarray:
    f = _TEX.copy()
    if item == "A":
        f[30:60, 30:60] = 230            # the real product: a bright box
    elif item == "B":
        f[30:60, 30:60] = 30             # a different, dark item in its place
    if hand:
        f[40:70, 75:95] = 200            # a moving hand inside the shelf
    return f


def run(before: str | None, after: str | None):
    eng = ShelfInteractionEngine()
    eng.set_zones([ZoneDef("shelf", "Shelf", ZoneType.SHELF, SQUARE)])
    events, t = [], 0.0
    for _ in range(3):
        events += eng.update(frame(before), {}, t)
        t += 0.1
    for i in range(10):
        events += eng.update(frame(before, i % 2 == 0), {"t1": PERSON}, t)
        t += 0.1
    for _ in range(30):
        events += eng.update(frame(after), {}, t)
        t += 0.1
    return events


def test_item_taken_is_unresolved_not_a_swap():
    ev = run("A", None)
    assert [e.kind for e in ev] == ["SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION"]
    assert ev[-1].interaction.verdict == "TAKEN"


def test_different_item_left_in_its_place_is_a_possible_swap():
    ev = run("A", "B")
    assert [e.kind for e in ev] == ["SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION"]
    assert ev[-1].interaction.verdict == "REPLACED"


def test_item_put_on_an_empty_spot_is_not_an_alert():
    ev = run(None, "A")
    assert [e.kind for e in ev][-1] == "RESOLVED"
    assert ev[-1].interaction.verdict == "ADDED"


def test_same_item_put_back_is_resolved():
    assert [e.kind for e in run("A", "A")] == ["SHELF_INTERACTION", "RESOLVED"]


def _correlate(ev, extra_transitions=()):
    c = Correlator("cam1")
    feats = {"person", "shelf", "exit"}
    out = c.process(now=10.0, active_tracks={}, promoted=[], ended=[], transitions=[], shelf_events=ev,
                    store_open=True, features=feats, zone_of_track={})
    exit_zone = ZoneDef("exit", "Exit", ZoneType.EXIT, SQUARE)
    out += c.process(now=20.0, active_tracks={}, promoted=[], ended=[], transitions=list(extra_transitions) or
                     [ZoneTransition("ENTRY", "t1", exit_zone)], shelf_events=[], store_open=True, features=feats,
                     zone_of_track={})
    return out


def test_swap_alerts_on_its_own_and_still_feeds_unpaid_exit():
    shelf = [e for e in run("A", "B") if e.kind == "UNRESOLVED_SHELF_INTERACTION"]
    drafts = _correlate(shelf)
    types = [d.event_type for d in drafts]
    assert "POSSIBLE_PRODUCT_REPLACEMENT" in types          # alert even if they stay to pay
    assert "POSSIBLE_UNPAID_EXIT" in types                  # …and walking out still counts
    swap = next(d for d in drafts if d.event_type == "POSSIBLE_PRODUCT_REPLACEMENT")
    assert swap.severity == "HIGH" and swap.confidence in ("LOW", "MEDIUM")
    assert "not an accusation" in swap.metadata["note"]


def test_plain_taking_never_claims_a_swap():
    shelf = [e for e in run("A", None) if e.kind == "UNRESOLVED_SHELF_INTERACTION"]
    assert "POSSIBLE_PRODUCT_REPLACEMENT" not in [d.event_type for d in _correlate(shelf)]


def test_swap_opens_an_incident_in_the_theft_family():
    rule = RULES["POSSIBLE_PRODUCT_REPLACEMENT"]
    assert rule.creates_incident and rule.severity == "HIGH"
    assert rule.family == RULES["POSSIBLE_UNPAID_EXIT"].family  # swap + walk-out = one incident
    assert "Possible" in rule.title


def test_theft_and_swap_sound_the_alarm_by_default(tmp_path):
    from app.alarms.service import DEFAULT_RULES

    on = {t for t, enabled, *_ in DEFAULT_RULES if enabled}
    assert {"POSSIBLE_UNPAID_EXIT", "POSSIBLE_PRODUCT_REPLACEMENT", "POSSIBLE_FIRE"} <= on
    assert "POSSIBLE_CONCEALMENT" not in on  # always LOW, never alarms on its own


def test_swap_pops_up_on_the_pc_and_phone():
    from app.notify.feed import GROUPS

    assert GROUPS["POSSIBLE_PRODUCT_REPLACEMENT"] == "unpaid_exit"
    _ = datetime.now(timezone.utc)
