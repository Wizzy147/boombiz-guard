# Phase 2 — Local AI detection & tracking

```
stream worker (Phase 1, 10 fps MJPEG) ──► per-camera queue (max 2, newest wins)
   ──► scheduler gate (target FPS from the load policy)
   ──► decode ──► privacy mask ──► YOLOX person detector (ONE shared model)
   ──► ignore-zone filter ──► ByteTrack-lite ──► zone engine (foot point)
   ──► shelf interaction heuristic ──► correlator ──► ai_events (SQLite)
   └─► fire/smoke heuristic at 2 fps ──► 5-of-8 confirmation ──► ai_events
```

No cloud, no uploads. The AI layer never receives a camera URL or password —
only decoded frames from the stream workers.

## Components

| Module | What it does |
|---|---|
| `ai/backends/` | `InferenceBackend` interface; `ONNXCPUBackend` (≤4 threads, half the cores, so the POS keeps the rest). Coral/Hailo/TensorRT/OpenVINO slot in later. |
| `ai/model_manager.py` | Loads a model **only** if its SHA-256 matches the manifest. Records model + heuristic versions on every event. |
| `ai/detector.py` | YOLOX (Apache-2.0), person class only, letterbox 416, grid decode, NMS. |
| `ai/tracker.py` | ByteTrack two-pass matching (high then low confidence = occlusion rescue), constant-velocity prediction, NEW → ACTIVE → TEMPORARILY_LOST (2 s) → ENDED (5 s). No appearance model, no faces. |
| `zones/` | Normalised polygons, validated on input. Foot-point membership. IGNORE drops detections; PRIVACY also blacks the area out of the frame before analysis. |
| `interactions/shelf.py` | Reach + local motion + dwell (feet nearly still) for ≥500 ms → SHELF_INTERACTION. After the person leaves: settle 1 s, compare the shelf to its "before" picture. |
| `events/correlator.py` | Per-track context → EXIT_APPROACH / POSSIBLE_UNPAID_EXIT / RESTRICTED / AFTER_HOURS with rule-based confidence. |
| `fire/` | Experimental colour + flicker + motion heuristic behind a `FireModel` slot; OFF by default. |
| `performance/` | Sustained-CPU monitor + adaptive policy with hysteresis. |

## Shelf verdicts

| Situation | Verdict | Alert effect |
|---|---|---|
| Shelf back to how it was | RESOLVED | none |
| Shelf changed ≥1.5 % (≥5 % = STRONG), camera still, lighting steady | UNRESOLVED | feeds unpaid-exit |
| Camera moved (>1.5 px, confident) or shelf lighting shifted | not verifiable → closed | none |
| Shelf blocked by other people for 5 s | UNRESOLVED, **UNVERIFIED** | unpaid-exit is always **LOW** |

Unpaid-exit confidence: STRONG + no cashier → HIGH · STRONG + cashier or WEAK +
no cashier → MEDIUM · WEAK + cashier or UNVERIFIED → LOW. An interaction that
turns unresolved within 15 s of the same person entering the exit still
counts (grab-and-go reaches the door before the shelf check settles).

## Concealment (§33) — experimental, ON by default, always LOW

A body-pose model (RTMPose-t, `ai/pose.py`) runs **only** on a person who
just had a SHELF_INTERACTION, at up to 5 fps, for 20 s. POSSIBLE_CONCEALMENT
needs all of:

1. a wrist at (or within 4 % of) the shelf during the interaction;
2. after they step back, that same hand at their own **waist/pocket band**
   (around the hip keypoints, inside the body's width);
3. held there ≥ 0.6 s, within 8 s of leaving the shelf.

Chest height is deliberately not a concealment region — that is how people
carry a product they mean to pay for. A hand at the pocket is still also how
people reach for a phone or money, so the event is **always LOW** and can only
raise a POSSIBLE_UNPAID_EXIT from LOW to MEDIUM. It never creates one on its
own, and it is the first feature paused under CPU load. Switch it off per
camera in Zones → "AI on this camera".

**Pose model licence: REVIEW REQUIRED before commercial launch.** RTMPose code
is Apache-2.0, but the only official ONNX weights ("body7") were trained
partly on research-only datasets (MPII, AI Challenger). Approved for pilots
only (2026-09-13). Candidates for a clean replacement: MoveNet (Apache-2.0
weights) or an RTMPose retrained on COCO only.

Walking hands swing at hip height, so the hold only counts while the person
is nearly still, and people under 20 % of the frame height aren't judged.

Verified: 10 rule tests (shelf→pocket fires once; hand that never touched the
shelf, a brief touch, a pocket 9 s later, chest height, walking arm-swing, a
far-away person — all ignored;
confidence bump capped; switch off = silent). On real footage 100 % of
577 keypoints land on their own person, 9.4 ms per pose, and the three
`lab/dod.py` scenarios raise zero false concealments. **Not verified:** a true
positive on real footage — no clip of someone pocketing an item exists in the
lab. That needs staged pilot footage.

## Load policy (§43–44)

| Sustained CPU | Level | FPS (primary / secondary) | Paused |
|---|---|---|---|
| < 70 % | NORMAL | 10 / 10 | — |
| 70–85 % | MODERATE | 7 / 7 | concealment |
| 85–92 % | HEAVY | 5 / 3 | concealment |
| > 92 % | CRITICAL | 3 / 3 | concealment, shelf, fire |

Person, exit, restricted and after-hours are never paused. Any level above
NORMAL shows "Guard is operating in reduced-performance mode".

## How it was verified (no shop hardware yet)

- **41 unit tests** — geometry, tracker lifecycle and occlusion rescue, zones,
  every confidence combination, late resolution, dedup, load policy,
  business hours incl. overnight, 5-of-8 fire confirmation, tampered model
  refused, real-model JSON regression.
- **`lab/dod.py`** — real Pexels footage through the real worker:
  A static camera, product taken → exactly one POSSIBLE_UNPAID_EXIT on the same
  person as the shelf interaction; B product left → none; C moving camera → none.
- **Live lab** — two RTSP cameras through the running agent: ~8–10 AI fps each,
  ~16 ms/inference (YOLOX-nano, 4 CPU threads), 0.02–0.1 s frame-to-event lag,
  simulated 95 % CPU → CRITICAL with protection kept.

## Known limits — say these out loud in pilots

- Stock footage is not CCTV and a painted product is not a shelf. Real
  accuracy (detection rate, false-alert rate) only comes from the §71 pilot.
- The shelf heuristic cannot see small items on a busy shelf, and a person who
  stands still and gestures at a shelf can register an interaction. Thresholds
  are exposed in `ShelfConfig` for pilot tuning.
- A crowded aisle often blocks the shelf check → UNVERIFIED (LOW) events.
- Fire/smoke is a heuristic, not a trained model: warning-only, OFF by default.
- Concealment has no real-footage true-positive yet, and its pose weights
  need a licence review before commercial launch (see above).
- Benchmarks were taken on one PC (12 logical cores, 7.7 GB RAM). The §60
  matrix (i5-8th, i5-6th, low-end POS) has not been run. RAM on this PC sat
  near 88 % with the whole lab running.
- No 8–12 h soak run yet.
