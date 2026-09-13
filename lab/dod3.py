"""Phase 3 Definition of Done (§85), on REAL footage, fully offline.

    agent\\.venv\\Scripts\\python lab\\dod3.py

The Phase 2 "product taken" market clip goes through the real per-camera AI
worker, and the SAME frames feed the real rolling buffer — exactly as the
live agent wires them. Then the real incident engine, media worker (encrypted
snapshot + clip), alarm engine (a network relay simulated on 127.0.0.1 that
counts on/off switches), review with PIN sign-in, Keep Evidence and retention.

No network beyond loopback is touched: this is the "store has no internet"
case by construction.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import uvicorn
from fastapi import FastAPI

LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB.parent / "agent"))

from app.ai.backends.onnx import ONNXCPUBackend  # noqa: E402
from app.ai.detector import PersonDetector  # noqa: E402
from app.ai.model_manager import ModelManager  # noqa: E402
from app.ai.worker import CameraAIWorker  # noqa: E402
from app.alarms.service import AlarmService  # noqa: E402
from app.buffer.rolling_buffer import RollingBuffer  # noqa: E402
from app.buffer.segment_manager import BufferManager  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.models import AlarmOutput, AlarmRule, Camera, Device, Incident  # noqa: E402
from app.events.service import EventService  # noqa: E402
from app.incidents.service import IncidentService  # noqa: E402
from app.media.encryption import MediaCrypto  # noqa: E402
from app.media.worker import MediaWorker  # noqa: E402
from app.performance.adaptive_policy import AdaptivePolicy  # noqa: E402
from app.performance.monitor import ResourceMonitor  # noqa: E402
from app.retention.service import RetentionService  # noqa: E402
from app.review.auth import LocalAuth  # noqa: E402
from app.security.vault import CredentialVault  # noqa: E402
from app.zones.engine import ZoneDef, ZoneType  # noqa: E402

sys.path.insert(0, str(LAB))
from dod import MARKET, SCENARIOS, build_clip  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}", flush=True)


# ── a fake network relay on loopback ─────────────────────────────────
RELAY = {"on": 0, "off": 0}
relay_app = FastAPI()


@relay_app.get("/relay/on")
async def _on() -> dict:
    RELAY["on"] += 1
    return {"ok": True}


@relay_app.get("/relay/off")
async def _off() -> dict:
    RELAY["off"] += 1
    return {"ok": True}


def serve_relay(port: int) -> None:
    cfg = uvicorn.Config(relay_app, host="127.0.0.1", port=port, log_level="error")
    threading.Thread(target=uvicorn.Server(cfg).run, daemon=True).start()
    time.sleep(1.0)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guard-dod3-"))
    serve_relay(7498)
    db = Database(tmp / "guard.db")
    with db.session() as s:
        s.add(Device(id="dev_lab", ip_address="127.0.0.2"))
        s.add(Camera(id="cam_lab", device_id="dev_lab", name="Main Exit", guard_enabled=True))
    vault = CredentialVault(db)
    crypto = MediaCrypto(tmp / "media.key")
    buffers = BufferManager(type("S", (), {"guard": {}})(), db)
    buffers.buffers["cam_lab"] = RollingBuffer("cam_lab")
    media = MediaWorker(db, crypto, tmp / "incidents", buffers.privacy_polygons)
    retention = RetentionService(db, tmp / "incidents")
    alarms = AlarmService(db, vault)
    with db.session() as s:
        for o in s.query(AlarmOutput).all():
            o.enabled = False  # no PC beeping during the run
        r = s.query(AlarmRule).filter_by(incident_type="POSSIBLE_UNPAID_EXIT").one()
        r.enabled, r.duration_seconds = True, 1
    alarms.add_output("Door buzzer relay", "NETWORK_RELAY", None,
                      {"on_url": "http://127.0.0.1:7498/relay/on", "off_url": "http://127.0.0.1:7498/relay/off"})
    incidents = IncidentService(db, buffers, media, alarms, retention)
    incidents.start()
    incidents.set_location_code("LAG")
    events = EventService(db)
    events.listeners.append(incidents.on_ai_event)

    mm = ModelManager(LAB.parent / "agent" / "models")
    p, _ = mm.verified_path("person-nano")
    be = ONNXCPUBackend()
    be.load_model(str(p))
    sc = next(s for s in SCENARIOS if s.out == "dod-A.mp4")
    zones = [ZoneDef("shelf_a", "Shelf A", ZoneType.SHELF, MARKET["shelf"]),
             ZoneDef("exit_1", "Exit", ZoneType.EXIT, MARKET["exit"]),
             ZoneDef("till", "Cashier", ZoneType.CASHIER, MARKET["cashier"])]
    w = CameraAIWorker("cam_lab", "Main Exit", detector=PersonDetector(be, threshold=0.45), events=events,
                       monitor=ResourceMonitor(AdaptivePolicy()), versions=mm.versions("person-nano"),
                       features={"person": True, "shelf": True, "exit": True, "restricted": True,
                                 "after_hours": False, "fire": False, "concealment": False},
                       priority="PRIMARY", zones=zones, schedule=[])

    clip = build_clip(sc)
    cap = cv2.VideoCapture(str(clip))
    buf = buffers.get("cam_lab")
    # Frame clock: a monotonic base, 10 fps — the same clock for buffer and AI.
    base, i = time.monotonic() - 1000, 0
    print("\nrunning the product-taken clip through AI → incidents …")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        ts = base + i / 10.0
        jpeg = cv2.imencode(".jpg", frame)[1].tobytes()
        buf.push(jpeg, ts)  # the stream worker feeds the buffer…
        for d in w._process(jpeg, ts):  # …and the AI, with the same frame
            events.emit(d, w.versions)
        await asyncio.sleep(0)
        i += 1
    # Keep the "camera" running a little past the clip so the 10 s post window closes.
    last = jpeg
    for k in range(115):  # ≥ 10 s past the trigger, as a live camera would
        buf.push(last, base + (i + k) / 10.0)
    await asyncio.sleep(1.5)  # let the alarm task run

    with db.session() as s:
        rows = s.query(Incident).all()
    check(len(rows) == 1, "Exactly one incident for the whole shelf→exit chain", f"{len(rows)} incident(s)")
    if not rows:
        return 1
    inc_id = rows[0].id
    d = incidents.get(inc_id)
    check(d["incident_type"] == "POSSIBLE_UNPAID_EXIT" and d["status"] == "UNREVIEWED",
          "POSSIBLE_UNPAID_EXIT created, starts UNREVIEWED", d["ref"])
    check(d["ref"].startswith("BG-LAG-"), "Human-readable ID with the installer's location code", d["ref"])
    seq = [t["event"] for t in d["timeline"]]
    check({"SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION", "EXIT_APPROACH"} <= set(seq),
          "Timeline explains why", " → ".join(e for e in seq if e != "ZONE_ENTRY"))

    # media worker (run the queued jobs now instead of waiting on the clock)
    from app.database.models import MediaJob

    with db.session() as s:
        for j in s.query(MediaJob).all():
            j.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    media.run_pending_now()
    d = incidents.get(inc_id)
    check(d["has_snapshot"], "Snapshot captured")
    check(d["has_clip"] and d["clip_duration_seconds"] <= 15.0, "Clip ≤ 15 s", f"{d['clip_duration_seconds']} s")
    clip_bytes, _ = incidents.media_bytes(inc_id, "clip", None, "OWNER")
    f = tmp / "check.mp4"
    f.write_bytes(clip_bytes)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                            "-of", "json", str(f)], capture_output=True, text=True)
    info = json.loads(probe.stdout or "{}")
    dur = float(info.get("format", {}).get("duration", 0))
    check(probe.returncode == 0 and 0 < dur <= 15.05, "Clip plays (ffprobe)",
          f"{info.get('streams', [{}])[0].get('codec_name')} {info.get('streams', [{}])[0].get('width')}x"
          f"{info.get('streams', [{}])[0].get('height')}, {dur:.1f} s")
    raw = [p.read_bytes()[:64] for p in (tmp / "incidents").rglob("*.enc")]
    check(raw and all(b"ftyp" not in b and not b.startswith(b"\xff\xd8") for b in raw),
          "Snapshot, clip and metadata are encrypted on disk", f"{len(raw)} files")
    check(not list((tmp / "incidents").rglob("*.mp4")) and not list((tmp / "incidents").rglob("*.jpg")),
          "No plaintext video or image written anywhere")

    check(RELAY["on"] >= 1, "Local alarm fired (network relay switched on)", f"on={RELAY['on']}")
    await asyncio.sleep(1.5)
    check(RELAY["off"] >= 1, "…and switched off after its duration", f"off={RELAY['off']}")

    auth = LocalAuth(db)
    auth.create_user("Ada", "OWNER", "1234")
    mgr = auth.create_user("Musa", "MANAGER", "2345")
    sec = auth.create_user("Chinedu", "SECURITY", "3456")
    s_sec, s_mgr = auth.sign_in(sec["id"], "3456"), auth.sign_in(mgr["id"], "2345")
    d = incidents.act(inc_id, "acknowledge", s_sec)
    check(d["status"] == "ACKNOWLEDGED" and d["acknowledged_by"] == "Chinedu", "Security acknowledges")
    d = incidents.act(inc_id, "confirm", s_mgr, note="Walked out with the red item, did not pay.")
    check(d["status"] == "CONFIRMED" and d["reviewed_by"] == "Musa", "Manager reviews → CONFIRMED")
    audit = [a.action for a in db.recent_audit(100)]
    check({"incident_created", "alarm_triggered", "incident_acknowledged", "incident_confirmed"} <= set(audit),
          "Every step is in the audit log")

    # retention: a second incident that is NOT kept expires; the confirmed one is kept as evidence
    s_owner = auth.sign_in(auth.users()[0]["id"], "1234")
    incidents.keep(inc_id, s_owner, True)
    incidents.on_ai_event({"camera_id": "cam_lab", "track_id": "other", "event_type": "RESTRICTED_ZONE_ENTRY",
                           "confidence": "HIGH", "occurred_at": datetime.now(timezone.utc).isoformat(), "metadata": {}})
    res = retention.cleanup(now=datetime.now(timezone.utc) + timedelta(days=31))
    with db.session() as s:
        kept = s.get(Incident, inc_id)
        other = s.query(Incident).filter(Incident.id != inc_id).one()
        check(kept.deleted_at is None and kept.clip_path, "Kept evidence survives 31-day retention")
        check(other.deleted_at is not None, "Unkept incident expires under retention", f"expired={res['expired']}")

    ok = all(r for r, _ in results)
    print(f"\n{sum(r for r, _ in results)}/{len(results)} passed — Phase 3 Definition of Done: {'PASS' if ok else 'FAIL'}")
    await incidents.stop()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
