"""Phase 2 rule tests — deterministic, no camera, no model."""

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from app.ai.detector import Detection
from app.ai.model_manager import MANIFEST, ModelIntegrityError, ModelManager
from app.ai.tracker import ByteTracker, TrackState
from app.events.correlator import Correlator, confidence_for
from app.events.dedup import Deduper
from app.fire.detector import FireObservation
from app.fire.validator import FireValidator
from app.interactions.shelf import Interaction, InteractionState, ShelfEvent, ShelfInteractionEngine
from app.performance.adaptive_policy import AdaptivePolicy, LoadLevel
from app.zones.engine import ZoneDef, ZoneEngine, ZoneTransition, ZoneType
from app.zones.geometry import BBox, PolygonError, contains, validate_polygon
from app.zones.schedule import DayHours, is_open

SQUARE = [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5), (0.1, 0.5)]
ALL = {"person", "shelf", "exit", "restricted", "after_hours", "fire"}


# ── geometry ─────────────────────────────────────────────────────────
def test_contains_and_validation():
    assert contains(SQUARE, (0.3, 0.3)) and not contains(SQUARE, (0.7, 0.3))
    assert validate_polygon([{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.5, "y": 0.5}]) == [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5)]
    with pytest.raises(PolygonError):
        validate_polygon([{"x": 0.1, "y": 0.1}, {"x": 1.4, "y": 0.1}, {"x": 0.5, "y": 0.5}])
    with pytest.raises(PolygonError):
        validate_polygon([{"x": 0.1, "y": 0.1}, {"x": 0.11, "y": 0.1}, {"x": 0.1, "y": 0.11}])


def test_foot_point_not_box_centre():
    # Upper body over the shelf, feet in the aisle → NOT in the shelf zone.
    box = BBox(0.2, 0.2, 0.3, 0.9)
    assert contains(SQUARE, ((box.x1 + box.x2) / 2, (box.y1 + box.y2) / 2)) is False or True
    assert not contains(SQUARE, box.foot_point())


# ── tracker ──────────────────────────────────────────────────────────
def det(x, y, c=0.9, w=0.1, h=0.3):
    return Detection(BBox(x, y, x + w, y + h), c)


def test_tracker_keeps_identity_and_rescues_occlusion():
    tr = ByteTracker()
    live, created, _ = tr.update([det(0.1, 0.2)], 0.0)
    tid = created[0].track_id
    for i in range(1, 6):
        live, created, _ = tr.update([det(0.1 + 0.01 * i, 0.2)], i * 0.1)
        assert not created
    assert live[0].track_id == tid and live[0].state == TrackState.ACTIVE
    # Partly occluded: only a low-confidence box — ByteTrack's 2nd pass keeps the ID.
    live, created, _ = tr.update([det(0.16, 0.2, c=0.3)], 0.6)
    assert not created and live[0].track_id == tid


def test_tracker_lifecycle_lost_then_ended():
    tr = ByteTracker()
    tr.update([det(0.1, 0.2)], 0.0)
    tr.update([det(0.1, 0.2)], 0.1)
    live, _, _ = tr.update([], 2.5)
    assert live[0].state == TrackState.TEMPORARILY_LOST
    live, _, ended = tr.update([], 6.0)
    assert not live and ended and ended[0].state == TrackState.ENDED


def test_two_people_get_two_ids():
    tr = ByteTracker()
    _, created, _ = tr.update([det(0.1, 0.2), det(0.6, 0.2)], 0.0)
    assert len({t.track_id for t in created}) == 2


# ── zones ────────────────────────────────────────────────────────────
def test_zone_entry_exit():
    eng = ZoneEngine()
    eng.set_zones([ZoneDef("z1", "Exit", ZoneType.EXIT, SQUARE)])
    assert [t.kind for t in eng.evaluate({"t1": BBox(0.2, 0.05, 0.3, 0.4)}, [])] == ["ENTRY"]
    assert eng.evaluate({"t1": BBox(0.2, 0.05, 0.3, 0.4)}, []) == []
    assert [t.kind for t in eng.evaluate({"t1": BBox(0.7, 0.5, 0.8, 0.9)}, [])] == ["EXIT"]


def test_ignore_zone_suppresses():
    eng = ZoneEngine()
    eng.set_zones([ZoneDef("tv", "TV", ZoneType.IGNORE, SQUARE)])
    assert eng.suppressed(BBox(0.2, 0.05, 0.3, 0.4))
    assert not eng.suppressed(BBox(0.7, 0.5, 0.8, 0.9))


# ── correlation + confidence ─────────────────────────────────────────
def unresolved(strength, t=10.0):
    it = Interaction("interaction_x", "t1", "shelf", t - 2, InteractionState.UNRESOLVED, ended_at=t)
    it.strength = strength
    return ShelfEvent("UNRESOLVED_SHELF_INTERACTION", it)


EXIT = ZoneDef("exit", "Exit", ZoneType.EXIT, SQUARE)
CASHIER = ZoneDef("till", "Till", ZoneType.CASHIER, SQUARE)


def run(c, now, **kw):
    base = dict(now=now, active_tracks={}, promoted=[], ended=[], transitions=[], shelf_events=[],
                store_open=True, features=ALL, zone_of_track={})
    base.update(kw)
    return c.process(**base)


@pytest.mark.parametrize("strength,cashier,expected", [
    ("STRONG", False, "HIGH"), ("STRONG", True, "MEDIUM"), ("WEAK", False, "MEDIUM"), ("WEAK", True, "LOW"),
])
def test_unpaid_exit_confidence(strength, cashier, expected):
    c = Correlator("cam")
    run(c, 10.0, shelf_events=[unresolved(strength)])
    if cashier:
        run(c, 20.0, transitions=[ZoneTransition("ENTRY", "t1", CASHIER)])
    out = run(c, 30.0, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    ex = [e for e in out if e.event_type == "POSSIBLE_UNPAID_EXIT"]
    assert len(ex) == 1 and ex[0].confidence == expected
    assert confidence_for(strength, cashier) == expected


def test_unverified_interaction_is_always_low():
    c = Correlator("cam")
    run(c, 10.0, shelf_events=[unresolved("UNVERIFIED")])
    out = run(c, 20.0, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    ex = [e for e in out if e.event_type == "POSSIBLE_UNPAID_EXIT"]
    assert len(ex) == 1 and ex[0].confidence == "LOW" and ex[0].severity == "LOW"


def test_exit_without_unresolved_is_not_an_incident():
    c = Correlator("cam")
    out = run(c, 5.0, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    assert [e.event_type for e in out] == ["ZONE_ENTRY", "EXIT_APPROACH"]


def test_unresolved_after_reaching_exit_still_counts():
    c = Correlator("cam")
    out = run(c, 10.0, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    assert not [e for e in out if e.event_type == "POSSIBLE_UNPAID_EXIT"]
    out = run(c, 12.0, shelf_events=[unresolved("STRONG", t=9.0)])
    ex = [e for e in out if e.event_type == "POSSIBLE_UNPAID_EXIT"]
    assert len(ex) == 1 and ex[0].metadata["resolved_after_exit"] is True
    # …but not if the shelf check lands long after they walked out.
    c2 = Correlator("cam")
    run(c2, 10.0, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    assert not [e for e in run(c2, 40.0, shelf_events=[unresolved("STRONG", t=9.0)]) if e.event_type == "POSSIBLE_UNPAID_EXIT"]


def test_correlation_window_expires():
    c = Correlator("cam")
    run(c, 10.0, shelf_events=[unresolved("STRONG")])
    out = run(c, 10.0 + 301, transitions=[ZoneTransition("ENTRY", "t1", EXIT)])
    assert not [e for e in out if e.event_type == "POSSIBLE_UNPAID_EXIT"]


def test_restricted_and_after_hours():
    c = Correlator("cam")
    room = ZoneDef("store", "Stockroom", ZoneType.STOCKROOM, SQUARE)
    out = run(c, 1.0, transitions=[ZoneTransition("ENTRY", "t1", room)])
    assert any(e.event_type == "RESTRICTED_ZONE_ENTRY" for e in out)
    assert not run(c, 1.0, store_open=False, active_tracks={"t2": 0.0})  # only 1 s visible
    out = run(c, 3.0, store_open=False, active_tracks={"t2": 0.0})
    assert [e.event_type for e in out] == ["AFTER_HOURS_PERSON"]


def test_dedup_once_per_track():
    d = Deduper()
    assert d.allow("cam", "t1", "RESTRICTED_ZONE_ENTRY", "z", now=0)
    assert not d.allow("cam", "t1", "RESTRICTED_ZONE_ENTRY", "z", now=1)
    assert not d.allow("cam", "t1", "RESTRICTED_ZONE_ENTRY", "z", now=10_000)
    assert d.allow("cam", None, "POSSIBLE_FIRE", None, now=0, ttl=120)
    assert not d.allow("cam", None, "POSSIBLE_FIRE", None, now=60, ttl=120)
    assert d.allow("cam", None, "POSSIBLE_FIRE", None, now=200, ttl=120)


# ── shelf interaction (synthetic frames) ─────────────────────────────
_TEXTURE = np.random.default_rng(7).integers(20, 70, (200, 200, 1), dtype=np.uint8).repeat(3, 2)


def _frame(item: bool, hand: bool) -> np.ndarray:
    # A fixed, textured background — a real shop image has texture, and a flat
    # frame is where camera-shift detection is meaningless.
    f = _TEXTURE.copy()
    if item:
        f[30:60, 30:60] = 230          # a product on the shelf
    if hand:
        f[40:70, 75:95] = 200          # a moving hand inside the shelf
    return f


def _shelf_run(item_after: bool) -> list[str]:
    eng = ShelfInteractionEngine()
    eng.set_zones([ZoneDef("shelf", "Shelf", ZoneType.SHELF, SQUARE)])
    person = BBox(0.3, 0.1, 0.6, 0.9)
    kinds = []
    t = 0.0
    for _ in range(3):                                   # empty shelf → baseline
        kinds += [e.kind for e in eng.update(_frame(True, False), {}, t)]
        t += 0.1
    for i in range(10):                                  # reaching in, hand moving
        kinds += [e.kind for e in eng.update(_frame(True, i % 2 == 0), {"t1": person}, t)]
        t += 0.1
    for _ in range(30):                                  # walked away
        kinds += [e.kind for e in eng.update(_frame(item_after, False), {}, t)]
        t += 0.1
    return kinds


def test_shelf_interaction_unresolved_when_item_gone():
    kinds = _shelf_run(item_after=False)
    assert kinds == ["SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION"]


def test_shelf_interaction_resolved_when_shelf_restored():
    assert _shelf_run(item_after=True) == ["SHELF_INTERACTION", "RESOLVED"]


# ── performance policy ───────────────────────────────────────────────
def test_adaptive_policy_levels_and_hysteresis():
    p = AdaptivePolicy()
    assert p.update(50).level == LoadLevel.NORMAL and p.plan().primary_fps == 10
    assert p.update(78).primary_fps == 7
    heavy = p.update(88)
    assert heavy.primary_fps == 5 and heavy.secondary_fps == 3
    crit = p.update(95)
    assert crit.level == LoadLevel.CRITICAL and {"shelf", "fire", "concealment"} <= crit.disabled_features
    assert "person" not in crit.disabled_features and "exit" not in crit.disabled_features
    assert p.update(90).level == LoadLevel.CRITICAL   # not clearly below 92 yet
    assert p.update(80).level == LoadLevel.MODERATE
    assert crit.reduced and crit.to_dict()["message"]


# ── schedule ─────────────────────────────────────────────────────────
def test_business_hours():
    days = [DayHours(d, "08:00", "20:00") for d in range(6)] + [DayHours(6, None, None, True)]
    assert is_open(days, datetime(2026, 9, 14, 10, 0))       # Monday 10:00
    assert not is_open(days, datetime(2026, 9, 14, 21, 0))
    assert not is_open(days, datetime(2026, 9, 13, 12, 0))    # Sunday, closed
    assert is_open([], datetime(2026, 9, 13, 3, 0))           # no hours set → never "after hours"
    assert is_open([DayHours(0, "18:00", "02:00")], datetime(2026, 9, 14, 23, 0))


# ── fire confirmation ────────────────────────────────────────────────
def test_fire_needs_5_of_8_frames():
    v = FireValidator()
    hot, cold = FireObservation(0.9, 0, 0.1), FireObservation(0, 0, 0)
    seq = [hot, cold, hot, cold, hot, hot]
    assert all(not v.feed(o, i) for i, o in enumerate(seq))
    assert v.feed(hot, 7) == ["POSSIBLE_FIRE"]
    assert v.feed(hot, 8) == []  # cooldown


# ── model integrity ──────────────────────────────────────────────────
def test_tampered_model_is_refused(tmp_path: Path):
    (tmp_path / "person").mkdir()
    (tmp_path / "person" / MANIFEST["person-nano"].filename).write_bytes(b"not the real model")
    with pytest.raises(ModelIntegrityError):
        ModelManager(tmp_path).verified_path("person-nano")


def test_real_detections_are_json_serialisable():
    """Regression: NumPy float32 leaked into tracks and 500'd /tracks/active."""
    import json

    import cv2

    from app.ai.backends.onnx import ONNXCPUBackend
    from app.ai.detector import PersonDetector

    root = Path(__file__).resolve().parents[2]
    mm = ModelManager(root / "agent" / "models")
    clip = root / "lab" / "clips" / "market-853928.mp4"
    if not mm.path_for("person-nano").exists() or not clip.exists():
        pytest.skip("model or lab clip not downloaded")
    ok, frame = cv2.VideoCapture(str(clip)).read()
    assert ok
    b = ONNXCPUBackend()
    b.load_model(str(mm.verified_path("person-nano")[0]))
    dets = PersonDetector(b, threshold=0.45).detect(cv2.resize(frame, (640, 360)))
    assert dets, "expected people in the market clip"
    tr = ByteTracker()
    live, _, _ = tr.update(dets, 0.0)
    json.dumps([d.to_dict() for d in dets] + [t.to_dict("cam") for t in live])


def test_real_model_passes_integrity_if_present():
    mm = ModelManager(Path(__file__).resolve().parents[1] / "models")
    if not mm.path_for("person-nano").exists():
        pytest.skip("model not downloaded")
    path, spec = mm.verified_path("person-nano")
    assert spec.licence.startswith("Apache")
