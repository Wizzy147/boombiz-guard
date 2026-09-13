"""Phase 2 Definition of Done, on REAL footage, through the REAL AI worker.

    agent\\.venv\\Scripts\\python lab\\dod.py

Each scenario builds a 640×360 / 10 fps clip from a Pexels video, optionally
painting a red "product" onto a shelf that disappears at a set moment, then
feeds every frame through CameraAIWorker._process — YOLOX-nano, tracker,
zones, shelf heuristic, correlator — exactly as the live agent does.

  A  static camera, product taken while a shopper stands at the stall, who
     then walks off through the exit      → exactly ONE POSSIBLE_UNPAID_EXIT,
                                             on the same person as the shelf
                                             interaction (§64)
  B  same footage, product left in place  → NO unpaid-exit
  C  MOVING handheld camera, product left → NO unpaid-exit (a camera that
                                             moves must not read as "item gone")

Stock clips are not CCTV and a painted product is not a real shelf — this
proves the pipeline end to end, not detection accuracy in a real shop.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2

LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB.parent / "agent"))

from app.ai.backends.onnx import ONNXCPUBackend  # noqa: E402
from app.ai.detector import PersonDetector  # noqa: E402
from app.ai.model_manager import ModelManager  # noqa: E402
from app.ai.pose import PoseEstimator  # noqa: E402
from app.ai.worker import CameraAIWorker  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.events.service import EventService  # noqa: E402
from app.performance.adaptive_policy import AdaptivePolicy  # noqa: E402
from app.performance.monitor import ResourceMonitor  # noqa: E402
from app.zones.engine import ZoneDef, ZoneType  # noqa: E402

Poly = list[tuple[float, float]]


def rect(x1: float, y1: float, x2: float, y2: float) -> Poly:
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


@dataclass
class Scenario:
    label: str
    source: str
    out: str
    shelf: Poly
    exit: Poly
    cashier: Poly
    product: tuple[float, float, float, float] | None  # x, y, w, h normalised
    remove_at: float | None
    expect_exit: bool


MARKET = dict(source="market-853928.mp4", shelf=rect(0.72, 0.20, 0.98, 0.45), exit=rect(0.00, 0.75, 0.30, 1.00),
              cashier=rect(0.00, 0.00, 0.15, 0.30), product=(0.90, 0.25, 0.06, 0.07))
SHOP = dict(source="shop-4750083.mp4", shelf=rect(0.66, 0.00, 0.92, 0.26), exit=rect(0.60, 0.68, 0.95, 1.00),
            cashier=rect(0.00, 0.60, 0.25, 1.00), product=(0.84, 0.08, 0.06, 0.07))

SCENARIOS = [
    Scenario("A — static camera: product taken, shopper leaves by the exit", out="dod-A.mp4", remove_at=8.0,
             expect_exit=True, **MARKET),
    Scenario("B — static camera: product left on the shelf", out="dod-B.mp4", remove_at=None, expect_exit=False, **MARKET),
    Scenario("C — moving camera: product left on the shelf", out="dod-C.mp4", remove_at=None, expect_exit=False, **SHOP),
]


def build_clip(s: Scenario) -> Path:
    out = LAB / "clips" / s.out
    # tpad: hold the last frame 4 s. A real camera keeps watching the shelf
    # after the shopper leaves; the stock clip just stops, which would cut off
    # the ~1.7 s settle-then-compare check.
    vf = "scale=640:360,fps=10,tpad=stop_mode=clone:stop_duration=4"
    if s.product:
        x, y, w, h = s.product
        enable = f":enable='lt(t,{s.remove_at})'" if s.remove_at is not None else ""
        vf += f",drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:color=red@1:t=fill{enable}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(LAB / "clips" / s.source), "-vf", vf,
                    "-an", "-c:v", "libx264", "-preset", "veryfast", str(out)], check=True)
    return out


def run(s: Scenario, clip: Path) -> list[dict]:
    mm = ModelManager(LAB.parent / "agent" / "models")
    path, _ = mm.verified_path("person-nano")
    backend = ONNXCPUBackend()
    backend.load_model(str(path))
    events = EventService(Database(":memory:"))
    zones = [ZoneDef("shelf_a", "Shelf A", ZoneType.SHELF, s.shelf), ZoneDef("exit_1", "Exit", ZoneType.EXIT, s.exit),
             ZoneDef("till", "Cashier", ZoneType.CASHIER, s.cashier)]
    w = CameraAIWorker("cam_lab", "Lab", detector=PersonDetector(backend, threshold=0.45), events=events,
                       monitor=ResourceMonitor(AdaptivePolicy()), versions=mm.versions("person-nano"),
                       features={"person": True, "shelf": True, "exit": True, "restricted": True,
                                 "after_hours": False, "fire": False, "concealment": True},
                       priority="PRIMARY", zones=zones, schedule=[])
    # Concealment on, exactly as shipped (default ON). None of these clips
    # shows anyone pocketing anything, so any POSSIBLE_CONCEALMENT here is a
    # false positive.
    ppath, _ = mm.verified_path("pose-rtm-t")
    pose_backend = ONNXCPUBackend(threads=2)
    pose_backend.load_model(str(ppath))
    w.pose = PoseEstimator(pose_backend)
    cap = cv2.VideoCapture(str(clip))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        ok, jpeg = cv2.imencode(".jpg", frame)
        for d in w._process(jpeg.tobytes(), i / 10.0):
            e = events.emit(d, w.versions)
            if e and e["event_type"] in ("SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION", "EXIT_APPROACH",
                                         "POSSIBLE_UNPAID_EXIT", "POSSIBLE_CONCEALMENT"):
                extra = e["confidence"] or ""
                if e["event_type"] == "UNRESOLVED_SHELF_INTERACTION":
                    m = e["metadata"]
                    extra += (f"  shelf changed {m.get('change_frac')}, camera shift {m.get('camera_shift_px')}px "
                              f"({m.get('strength')})")
                print(f"  t={i / 10:5.1f}s  {e['event_type']:<30} {e['track_id'] or '':<11} {extra}")
        i += 1
    for tid, its in w.shelf.history.items():
        for it in its:
            if it.announced:
                print(f"  · {it.id} {tid}: {it.state.value}  shelf changed {it.change_frac}  "
                      f"camera shift {it.control_change}px  brightness {it.brightness_change}  "
                      f"verifiable {it.verifiable}  strength {it.strength}")
    return events.list(limit=5000)


def main() -> int:
    all_ok = True
    for s in SCENARIOS:
        print(f"\n{s.label}")
        evs = run(s, build_clip(s))
        exits = [e for e in evs if e["event_type"] == "POSSIBLE_UNPAID_EXIT"]
        hidden = [e for e in evs if e["event_type"] == "POSSIBLE_CONCEALMENT"]
        no_false_concealment = not hidden
        print(f"  → {'PASS' if no_false_concealment else 'FAIL'}: false POSSIBLE_CONCEALMENT = {len(hidden)} (expected 0)")
        all_ok &= no_false_concealment
        if s.expect_exit:
            ok = len(exits) == 1
            if ok:
                tid = exits[0]["track_id"]
                mine = {e["event_type"] for e in evs if e["track_id"] == tid}
                ok = {"PERSON_DETECTED", "SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION", "EXIT_APPROACH"} <= mine
                print(f"  → {'PASS' if ok else 'FAIL'}: one POSSIBLE_UNPAID_EXIT ({exits[0]['confidence']}) on {tid}; "
                      f"same person has: {sorted(mine - {'ZONE_ENTRY', 'ZONE_EXIT', 'TRACK_LOST'})}")
            else:
                print(f"  → FAIL: expected exactly one POSSIBLE_UNPAID_EXIT, got {len(exits)}")
        else:
            ok = not exits
            print(f"  → {'PASS' if ok else 'FAIL'}: unpaid-exit events = {len(exits)} (expected 0)")
        all_ok &= ok
    print("\nDefinition of Done:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
