"""Covered / turned camera check (ai/tamper.py). Synthetic frames, no camera."""

import cv2
import numpy as np

from app.ai.tamper import TamperConfig, TamperDetector
from app.incidents.classifier import RULES
from app.notify.feed import GROUPS


def shelves(shift: int = 0) -> np.ndarray:
    """A shop scene: rows of 'products' with sharp edges."""
    img = np.full((360, 640, 3), 90, np.uint8)
    rng = np.random.default_rng(7)
    for row in range(4):
        y = 40 + row * 80
        cv2.line(img, (0, y + 60), (640, y + 60), (30, 30, 30), 4)
        for x in range(10, 620, 34):
            c = tuple(int(v) for v in rng.integers(40, 230, 3))
            cv2.rectangle(img, (x + shift, y), (x + shift + 24, y + 55), c, -1)
    return img


def wall() -> np.ndarray:
    """Turned to face somewhere else: detail, but none of the usual edges."""
    img = np.full((360, 640, 3), 150, np.uint8)
    for x in range(0, 640, 90):
        cv2.line(img, (x, 0), (x + 200, 360), (60, 60, 60), 5)
    return img


def covered() -> np.ndarray:
    return np.full((360, 640, 3), 12, np.uint8)


def run(det: TamperDetector, frame: np.ndarray, start: float, seconds: float) -> list[str]:
    out = []
    t = start
    while t < start + seconds:
        k = det.observe(frame, t)
        if k:
            out.append(k)
        t += 1.0
    return out


def learned() -> TamperDetector:
    det = TamperDetector()
    assert run(det, shelves(), 0, 70) == []  # learns the scene, no alert
    return det


def test_covered_lens_is_reported_once_after_30s():
    det = learned()
    assert run(det, covered(), 70, 25) == []  # a hand in front for a moment isn't tamper
    assert run(det, covered(), 95, 60) == ["COVERED"]  # confirmed once, not every second


def test_turned_camera_is_reported():
    det = learned()
    assert run(det, wall(), 70, 40) == ["TURNED"]


def test_normal_scene_and_small_changes_are_quiet():
    det = learned()
    assert run(det, shelves(shift=3), 70, 120) == []  # stock nudged a few pixels


def test_lights_off_keeps_the_same_edges():
    det = learned()
    dim = (shelves().astype(np.float32) * 0.45).astype(np.uint8)  # night mode / lights off
    assert run(det, dim, 70, 60) == []


def test_new_episode_needs_the_picture_back_first():
    det = learned()
    assert run(det, covered(), 70, 40) == ["COVERED"]
    assert run(det, shelves(), 110, 15) == []  # back to normal for > clear_s
    assert run(det, covered(), 125, 40) == ["COVERED"]  # covered again = new alert


def test_turned_is_not_judged_before_the_scene_is_learned():
    det = TamperDetector(TamperConfig(learn_s=60))
    assert run(det, wall(), 0, 40) == []  # nothing learned yet to compare with


def test_tamper_is_an_urgent_camera_problem():
    rule = RULES["CAMERA_TAMPERED"]
    assert rule.severity == "HIGH" and rule.creates_incident and rule.family == "health"
    assert RULES["CAMERA_OFFLINE"].severity == "HIGH"
    assert GROUPS["CAMERA_TAMPERED"] == "health"
