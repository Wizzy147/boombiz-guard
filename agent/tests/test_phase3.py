"""Phase 3 — incidents, evidence, review, retention, alarms. No camera needed:
frames are synthetic JPEGs pushed into a real RollingBuffer."""

import asyncio
import json
import re
import subprocess
import tempfile
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.alarms.adapters import AlarmError
from app.alarms.service import AlarmService
from app.buffer.rolling_buffer import MAX_CLIP_S, RollingBuffer
from app.buffer.segment_manager import BufferManager
from app.database.db import Database
from app.database.models import AlarmOutput, AlarmRule, Camera, Device, Incident, MediaJob
from app.incidents.lifecycle import TransitionError
from app.incidents.service import IncidentError, IncidentService
from app.media.encryption import MediaCrypto
from app.media.worker import MediaWorker
from app.retention.service import GB, RetentionService
from app.review.auth import AuthError, LocalAuth
from app.review.permissions import Forbidden
from app.security.vault import CredentialVault, _InsecureDevCipher

Disk = namedtuple("Disk", "total used free")
CIPHER = _InsecureDevCipher()


class FakeStreams:
    guard: dict = {}


def jpeg(i: int) -> bytes:
    rng = np.random.default_rng(i)
    img = rng.integers(0, 255, (180, 320, 3), dtype=np.uint8)
    cv2.putText(img, str(i), (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    return cv2.imencode(".jpg", img)[1].tobytes()


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / "g.db")
    with db.session() as s:
        s.add(Device(id="dev1", ip_address="10.0.0.2"))
        s.add(Camera(id="cam1", device_id="dev1", name="Main Exit", guard_enabled=True))
        s.add(Camera(id="cam2", device_id="dev1", name="Shelves", guard_enabled=True))
    free = {"v": 200 * GB}
    buffers = BufferManager(FakeStreams(), db)
    for c in ("cam1", "cam2"):
        buffers.buffers[c] = RollingBuffer(c)
    crypto = MediaCrypto(tmp_path / "media.key", CIPHER)
    media = MediaWorker(db, crypto, tmp_path / "incidents", buffers.privacy_polygons)
    retention = RetentionService(db, tmp_path / "incidents", disk_probe=lambda: Disk(500 * GB, 0, free["v"]))
    alarms = AlarmService(db, CredentialVault(db, CIPHER))
    with db.session() as s:  # never beep during tests
        for o in s.query(AlarmOutput).all():
            o.enabled = False
    svc = IncidentService(db, buffers, media, alarms, retention)
    auth = LocalAuth(db)
    return type("S", (), dict(db=db, buffers=buffers, media=media, retention=retention, alarms=alarms,
                              svc=svc, auth=auth, dir=tmp_path / "incidents", free=free))


def evt(etype, track="track_1", cam="cam1", conf="MEDIUM", **meta):
    return {"id": f"e{time.monotonic_ns()}", "camera_id": cam, "track_id": track, "event_type": etype,
            "severity": "HIGH", "confidence": conf, "zone_id": "exit_1",
            "metadata": meta, "occurred_at": datetime.now(timezone.utc).isoformat()}


def fill(buf: RollingBuffer, start: float, seconds: float, fps: float = 10):
    n = int(seconds * fps)
    for i in range(n):
        buf.push(jpeg(i), start + i / fps)


def drain(stack):
    with stack.db.session() as s:
        for j in s.query(MediaJob).all():
            j.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    stack.media.run_pending_now()


def incidents(stack):
    with stack.db.session() as s:
        return s.query(Incident).all()


# ── the Definition-of-Done chain, deterministic ──────────────────────
def test_related_events_become_one_incident_with_clip_and_snapshot(stack):
    buf = stack.buffers.get("cam1")
    t0 = time.monotonic()
    fill(buf, t0 - 6, 6)                                 # 6 s already buffered
    for e in ("PERSON_DETECTED", "SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION", "EXIT_APPROACH"):
        stack.svc.on_ai_event(evt(e))
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))  # repeat → merged, not a 2nd incident
    fill(buf, time.monotonic(), 11.5)                    # the 10 s after, and a bit
    drain(stack)
    rows = incidents(stack)
    assert len(rows) == 1
    inc = stack.svc.get(rows[0].id)
    assert re.fullmatch(r"BG-LOC-\d{8}-000001", inc["ref"])
    assert inc["status"] == "UNREVIEWED" and inc["severity"] == "HIGH"
    assert [t["event"] for t in inc["timeline"]][:4] == ["PERSON_DETECTED", "SHELF_INTERACTION",
                                                         "UNRESOLVED_SHELF_INTERACTION", "EXIT_APPROACH"]
    assert inc["merged_events"] == 1 and inc["media_status"] == "READY"
    assert 12.0 <= inc["clip_duration_seconds"] <= MAX_CLIP_S
    # Encrypted at rest: neither a JPEG nor an MP4 header on disk…
    raw = [p.read_bytes() for p in stack.dir.rglob("*.enc")]
    assert raw and all(not b.startswith(b"\xff\xd8") and b"ftyp" not in b[:64] for b in raw)
    # …but the API side decrypts to a real JPEG and a playable ≤15 s MP4.
    snap, ctype = stack.svc.media_bytes(rows[0].id, "snapshot", None, "OWNER")
    assert ctype == "image/jpeg" and snap[:2] == b"\xff\xd8"
    clip, _ = stack.svc.media_bytes(rows[0].id, "clip", None, "OWNER")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(clip)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f.name],
                               capture_output=True, text=True).stdout.strip())
    Path(f.name).unlink()
    assert 12.0 <= dur <= 15.05


def test_two_people_two_incidents_but_after_hours_groups_by_camera(stack):
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT", track="a"))
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT", track="b"))
    stack.svc.on_ai_event(evt("AFTER_HOURS_PERSON", track="c"))
    stack.svc.on_ai_event(evt("AFTER_HOURS_PERSON", track="d"))
    types = sorted(i.incident_type for i in incidents(stack))
    assert types == ["AFTER_HOURS_INTRUSION", "POSSIBLE_UNPAID_EXIT", "POSSIBLE_UNPAID_EXIT"]
    assert next(i for i in incidents(stack) if i.incident_type == "AFTER_HOURS_INTRUSION").severity == "CRITICAL"


def test_simultaneous_incidents_on_two_cameras_both_get_clips(stack):
    t0 = time.monotonic()
    for c in ("cam1", "cam2"):
        fill(stack.buffers.get(c), t0 - 6, 6)
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT", cam="cam1"))
    stack.svc.on_ai_event(evt("RESTRICTED_ZONE_ENTRY", cam="cam2"))
    for c in ("cam1", "cam2"):
        fill(stack.buffers.get(c), time.monotonic(), 11.5)
    drain(stack)
    assert [i.media_status for i in incidents(stack)] == ["READY", "READY"]


def test_context_events_alone_create_nothing(stack):
    for e in ("PERSON_DETECTED", "ZONE_ENTRY", "SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION",
              "EXIT_APPROACH", "POSSIBLE_CONCEALMENT"):
        stack.svc.on_ai_event(evt(e))
    assert incidents(stack) == []


def test_concealment_lifts_open_incident_one_step(stack):
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT", conf="LOW"))
    stack.svc.on_ai_event(evt("POSSIBLE_CONCEALMENT", conf="LOW"))
    assert incidents(stack)[0].confidence == "MEDIUM"


def test_lost_buffer_keeps_incident_without_video(stack):
    fill(stack.buffers.get("cam1"), time.monotonic() - 6, 6)
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))
    stack.media.captures.clear()                           # as if Guard restarted mid-clip
    drain(stack)
    inc = incidents(stack)[0]
    assert inc.media_status == "UNAVAILABLE" and inc.ref.startswith("BG-")


def test_running_jobs_recover_after_crash(stack):
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))
    with stack.db.session() as s:
        for j in s.query(MediaJob).all():
            j.status = "RUNNING"
    assert stack.media.recover() == 2


def test_camera_drops_after_incident_still_gets_shorter_clip(stack):
    buf = stack.buffers.get("cam1")
    fill(buf, time.monotonic() - 6, 6)
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))
    fill(buf, time.monotonic(), 2)                       # camera dies 2 s after the event
    cap = next(iter(stack.media.captures.values()))
    cap.opened_at -= cap.post_s + cap.GRACE_S + 1        # …and the post window has long passed
    drain(stack)
    inc = incidents(stack)[0]
    assert inc.media_status == "READY" and 5.0 <= inc.clip_duration_seconds < 10.0


def test_clip_never_exceeds_15_seconds():
    buf = RollingBuffer("c", seconds=10)
    t0 = 1000.0
    for i in range(300):
        buf.push(b"x", t0 + i * 0.1)
    cap = buf.start_capture("i", t0 + 25, pre_s=12, post_s=20)  # asks for 32 s
    assert cap.pre_s + cap.post_s <= MAX_CLIP_S
    for i in range(300, 600):
        buf.push(b"x", t0 + i * 0.1)
    fr = cap.clipped()
    assert fr[-1].ts - fr[0].ts <= MAX_CLIP_S


# ── review ───────────────────────────────────────────────────────────
def people(stack):
    o = stack.auth.create_user("Ada", "OWNER", "1234")
    m = stack.auth.create_user("Musa", "MANAGER", "2345")
    g = stack.auth.create_user("Chinedu", "SECURITY", "3456")
    return (stack.auth.sign_in(o["id"], "1234"), stack.auth.sign_in(m["id"], "2345"), stack.auth.sign_in(g["id"], "3456"))


def test_review_flow_and_permissions(stack):
    owner, manager, guard = people(stack)
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))
    stack.svc.on_ai_event(evt("POSSIBLE_FIRE", track=None))
    exit_id = next(i.id for i in incidents(stack) if i.incident_type == "POSSIBLE_UNPAID_EXIT")
    fire_id = next(i.id for i in incidents(stack) if i.incident_type == "POSSIBLE_FIRE")

    assert stack.svc.act(exit_id, "acknowledge", guard)["acknowledged_by"] == "Chinedu"
    with pytest.raises(Forbidden):
        stack.svc.act(fire_id, "confirm", guard)          # security: not on CRITICAL
    with pytest.raises(IncidentError):
        stack.svc.act(exit_id, "false_alert", manager)     # needs a reason
    d = stack.svc.act(exit_id, "false_alert", manager, note="Paid at till 2", reason="CUSTOMER_PAID")
    assert d["status"] == "FALSE_ALERT" and d["reviewed_by"] == "Musa" and d["false_alert_reason"] == "CUSTOMER_PAID"
    with pytest.raises(TransitionError):
        stack.svc.act(exit_id, "confirm", manager)         # must reopen first
    with pytest.raises(Forbidden):
        stack.svc.keep(exit_id, guard, True)
    with pytest.raises(Forbidden):
        stack.svc.delete(exit_id, manager)
    stack.svc.delete(exit_id, owner)
    with pytest.raises(IncidentError):
        stack.svc.get(exit_id)
    actions = [a.action for a in stack.db.recent_audit(50)]
    assert {"incident_acknowledged", "incident_false_alert", "incident_deleted"} <= set(actions)


def test_pin_lockout(stack):
    u = stack.auth.create_user("Ada", "OWNER", "1234")
    for _ in range(5):
        with pytest.raises(AuthError):
            stack.auth.sign_in(u["id"], "0000")
    with pytest.raises(AuthError, match="Too many"):
        stack.auth.sign_in(u["id"], "1234")                # right PIN, still locked
    with stack.db.session() as s:
        assert "1234" not in s.query(__import__("app.database.models", fromlist=["LocalUser"]).LocalUser).first().pin_hash


# ── retention & disk ─────────────────────────────────────────────────
def test_retention_expired_kept_and_confirmed(stack):
    owner, manager, _ = people(stack)
    for t in ("a", "b", "c"):
        fill(stack.buffers.get("cam1"), time.monotonic() - 6, 6)
        stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT", track=t))
        fill(stack.buffers.get("cam1"), time.monotonic(), 11.5)
    drain(stack)
    a, b, c = sorted(incidents(stack), key=lambda i: i.track_id)
    stack.svc.keep(b.id, owner, True)
    stack.svc.act(c.id, "confirm", manager)
    stack.retention.set_policy("POSSIBLE_UNPAID_EXIT", None, 30, keep_if_confirmed=True)
    res = stack.retention.cleanup(now=datetime.now(timezone.utc) + timedelta(days=31))
    assert res["expired"] == 1
    rows = {i.track_id: i for i in incidents(stack)}
    assert rows["a"].deleted_at is not None and rows["a"].media_status == "DELETED"
    assert rows["b"].deleted_at is None and rows["b"].clip_path          # Keep Evidence
    assert rows["c"].deleted_at is None                                   # confirmed + keep_if_confirmed
    assert not list((stack.dir).rglob(f"*{rows['a'].ref}*"))


def test_critical_low_disk_skips_non_critical_media(stack):
    stack.free["v"] = 1 * GB
    fill(stack.buffers.get("cam1"), time.monotonic() - 6, 6)
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))
    stack.svc.on_ai_event(evt("POSSIBLE_FIRE", track=None))
    by = {i.incident_type: i for i in incidents(stack)}
    assert by["POSSIBLE_UNPAID_EXIT"].media_status == "SKIPPED"          # metadata kept, no video
    assert by["POSSIBLE_FIRE"].media_status == "PENDING"                 # critical still captured
    assert stack.retention.status()["level"] == "CRITICAL"


# ── alarms ───────────────────────────────────────────────────────────
class FakeAdapter:
    calls: list = []
    fail = False

    async def available(self):
        return not self.fail

    async def activate(self, s):
        if self.fail:
            raise AlarmError("Relay not responding")
        FakeAdapter.calls.append(s)

    async def deactivate(self):
        pass


@pytest.mark.asyncio
async def test_alarm_cooldown_failure_and_fire_repeat(stack):
    with stack.db.session() as s:
        s.add(AlarmOutput(id="out1", name="Relay", kind="NETWORK_RELAY", config_json="{}"))
    stack.alarms._adapter = lambda o: FakeAdapter()
    FakeAdapter.calls, FakeAdapter.fail = [], False
    stack.svc.on_ai_event(evt("AFTER_HOURS_PERSON", track="x"))
    first = incidents(stack)[0].id
    assert await stack.alarms.on_incident(first) == "TRIGGERED" and FakeAdapter.calls == [10]
    stack.svc.on_ai_event(evt("AFTER_HOURS_PERSON", cam="cam2"))
    second = next(i.id for i in incidents(stack) if i.id != first)
    assert await stack.alarms.on_incident(second) == "COOLDOWN"          # recorded, siren quiet
    # Theft exits have no cooldown: back-to-back exits each sound the siren.
    FakeAdapter.calls = []
    for t in ("e1", "e2"):
        stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT", track=t))
    exits = [i.id for i in incidents(stack) if i.incident_type == "POSSIBLE_UNPAID_EXIT"]
    for eid in exits:
        assert await stack.alarms.on_incident(eid) == "TRIGGERED"
    assert len(FakeAdapter.calls) == len(exits) >= 2
    # A failing output: incident still exists, failure is recorded.
    FakeAdapter.fail = True
    stack.svc.on_ai_event(evt("POSSIBLE_SMOKE", track=None))
    smoke = next(i.id for i in incidents(stack) if i.incident_type == "POSSIBLE_SMOKE")
    assert await stack.alarms.on_incident(smoke) == "FAILED"
    assert next(o for o in stack.alarms.outputs() if o["id"] == "out1")["health"] == "ERROR"
    assert "ALARM_OUTPUT_FAILURE" in [a.action for a in stack.db.recent_audit(30)]
    # Fire repeats until acknowledged.
    FakeAdapter.fail = False
    stack.svc.on_ai_event(evt("POSSIBLE_FIRE", track=None, cam="cam2"))
    fire = next(i.id for i in incidents(stack) if i.incident_type == "POSSIBLE_FIRE")
    await stack.alarms.on_incident(fire)
    assert fire in stack.alarms._repeat_tasks
    owner, _, _ = people(stack)
    stack.svc.act(fire, "acknowledge", owner)
    assert fire not in stack.alarms._repeat_tasks


# ── camera health ────────────────────────────────────────────────────
def test_camera_offline_incidents(stack):
    svc = stack.svc
    svc.check_camera_health(0, {"cam1": "OFFLINE", "cam2": "ONLINE"})
    svc.check_camera_health(70, {"cam1": "OFFLINE", "cam2": "ONLINE"})
    off = [i for i in incidents(stack) if i.incident_type == "CAMERA_OFFLINE"]
    assert len(off) == 1 and off[0].severity == "LOW"
    svc.check_camera_health(80, {"cam1": "OFFLINE", "cam2": "ONLINE"})
    assert len([i for i in incidents(stack) if i.incident_type == "CAMERA_OFFLINE"]) == 1  # one until recovery
    svc.check_camera_health(320, {"cam1": "OFFLINE", "cam2": "ONLINE"})
    assert next(i for i in incidents(stack) if i.incident_type == "CAMERA_OFFLINE").severity == "HIGH"
    svc.check_camera_health(330, {"cam1": "ONLINE", "cam2": "ONLINE"})
    assert next(i for i in incidents(stack) if i.incident_type == "CAMERA_OFFLINE").ended_at is not None
    svc.check_camera_health(400, {"cam1": "OFFLINE", "cam2": "OFFLINE"})
    svc.check_camera_health(470, {"cam1": "OFFLINE", "cam2": "OFFLINE"})
    deg = [i for i in incidents(stack) if i.incident_type == "GUARD_PROTECTION_DEGRADED"]
    assert len(deg) == 1 and deg[0].severity == "CRITICAL"


def test_location_code(stack):
    with pytest.raises(IncidentError):
        stack.svc.set_location_code("LAGOS")
    stack.svc.set_location_code("lag")
    stack.svc.on_ai_event(evt("POSSIBLE_UNPAID_EXIT"))
    assert incidents(stack)[0].ref.startswith("BG-LAG-")
