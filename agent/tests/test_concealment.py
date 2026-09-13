"""Concealment rules on synthetic body points — no model needed."""

import numpy as np

from app.ai.pose import L_HIP, L_SHOULDER, L_WRIST, R_HIP, R_SHOULDER, R_WRIST, Pose
from app.events.correlator import Correlator, with_concealment
from app.interactions.concealment import ConcealmentEngine
from app.interactions.shelf import Interaction, InteractionState, ShelfEvent
from app.zones.engine import ZoneDef, ZoneTransition, ZoneType
from app.zones.geometry import BBox

SHELF = ZoneDef("shelf", "Shelf", ZoneType.SHELF, [(0.6, 0.2), (0.9, 0.2), (0.9, 0.5), (0.6, 0.5)])
EXIT = ZoneDef("exit", "Exit", ZoneType.EXIT, [(0.0, 0.7), (0.3, 0.7), (0.3, 1.0), (0.0, 1.0)])
BOX = BBox(0.35, 0.1, 0.55, 0.9)


def pose(right_wrist, left_wrist=(0.38, 0.55)):
    pts = np.zeros((17, 2))
    sc = np.full(17, 0.9)
    pts[L_SHOULDER], pts[R_SHOULDER] = (0.40, 0.25), (0.50, 0.25)
    pts[L_HIP], pts[R_HIP] = (0.41, 0.55), (0.49, 0.55)
    pts[R_WRIST], pts[L_WRIST] = right_wrist, left_wrist
    return Pose(pts, sc)


def run_engine(sequence, start=0.0, step=0.2, speed=0.0, box=BOX):
    eng = ConcealmentEngine()
    eng.on_shelf_interaction("t1", "shelf", start)
    t, hits = start, []
    for wrist in sequence:
        t += step
        eng.tracks["t1"].last_pose_at = -1  # bypass the fps gate in tests
        r = eng.update("t1", box, pose(wrist), SHELF, t, speed)
        if r:
            hits.append((round(t, 1), r))
    return hits


def test_shelf_then_pocket_is_concealment():
    seq = [(0.7, 0.3)] * 3 + [(0.55, 0.45)] + [(0.48, 0.56)] * 5  # shelf → moving → waist, held
    hits = run_engine(seq)
    assert len(hits) == 1 and hits[0][1] == "waist"


def test_hand_that_never_touched_shelf_is_ignored():
    # Hand goes to the pocket, but the person's hand was never at the shelf.
    assert run_engine([(0.45, 0.35)] * 3 + [(0.48, 0.56)] * 6) == []


def test_brief_touch_of_waist_is_not_enough():
    seq = [(0.7, 0.3)] * 3 + [(0.48, 0.56), (0.45, 0.35), (0.48, 0.56), (0.45, 0.35)]
    assert run_engine(seq) == []  # never held for 0.6 s


def test_walking_arm_swing_is_not_concealment():
    # Same hand path, but the person is walking: hands swing at hip height.
    seq = [(0.7, 0.3)] * 3 + [(0.55, 0.45)] + [(0.48, 0.56)] * 5
    assert run_engine(seq, speed=0.2) == []


def test_far_away_person_is_not_judged():
    small = BBox(0.45, 0.4, 0.5, 0.55)  # 15 % of frame height
    seq = [(0.7, 0.3)] * 3 + [(0.55, 0.45)] + [(0.48, 0.56)] * 5
    assert run_engine(seq, box=small) == []


def test_too_late_after_leaving_shelf_is_ignored():
    seq = [(0.7, 0.3)] * 3 + [(0.45, 0.35)] * 45 + [(0.48, 0.56)] * 6  # 9 s later
    assert run_engine(seq) == []


def test_concealment_raises_confidence_one_step_only():
    assert with_concealment("LOW", True) == "MEDIUM"
    assert with_concealment("MEDIUM", True) == "MEDIUM"
    assert with_concealment("HIGH", True) == "HIGH"
    assert with_concealment("LOW", False) == "LOW"


def _unresolved(strength):
    it = Interaction("i1", "t1", "shelf", 8.0, InteractionState.UNRESOLVED, ended_at=10.0)
    it.strength = strength
    return ShelfEvent("UNRESOLVED_SHELF_INTERACTION", it)


FEATS = {"person", "shelf", "exit", "restricted", "after_hours", "concealment"}


def _run(c, now, **kw):
    base = dict(now=now, active_tracks={}, promoted=[], ended=[], transitions=[], shelf_events=[],
                store_open=True, features=FEATS, zone_of_track={})
    base.update(kw)
    return c.process(**base)


def test_concealment_event_is_low_and_bumps_unpaid_exit():
    c = Correlator("cam")
    out = _run(c, 10.5, concealments=[("t1", "shelf", "waist")])
    ev = [e for e in out if e.event_type == "POSSIBLE_CONCEALMENT"]
    assert len(ev) == 1 and ev[0].confidence == "LOW" and ev[0].severity == "LOW"
    _run(c, 11.0, shelf_events=[_unresolved("UNVERIFIED")])   # alone this would be LOW
    out = _run(c, 20.0, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    ex = [e for e in out if e.event_type == "POSSIBLE_UNPAID_EXIT"]
    assert ex[0].confidence == "MEDIUM" and ex[0].metadata["concealment_seen"] is True


def test_concealment_alone_never_creates_unpaid_exit():
    c = Correlator("cam")
    _run(c, 10.5, concealments=[("t1", "shelf", "waist")])
    out = _run(c, 20.0, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    assert not [e for e in out if e.event_type == "POSSIBLE_UNPAID_EXIT"]


def test_concealment_switch_off_emits_nothing():
    c = Correlator("cam")
    out = _run(c, 10.5, concealments=[("t1", "shelf", "waist")], features=FEATS - {"concealment"})
    assert not out
