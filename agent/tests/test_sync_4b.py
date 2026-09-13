"""Phase 4B — incident sync queue against a fake cloud + fake bucket on loopback."""

import asyncio
import base64
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.cloud.client import CloudLink
from app.cloud.sync import SyncQueue
from app.database.db import Database
from app.database.models import Camera, Device, Incident, SyncJob
from app.media.encryption import MediaCrypto
from app.media.worker import MediaWorker
from app.security.vault import _InsecureDevCipher

CIPHER = _InsecureDevCipher()
SECRET = "gd_" + "s" * 43
PORT = 7499
BASE = f"http://127.0.0.1:{PORT}"

C: dict = {}


def reset(**kw):
    C.clear()
    C.update(incidents={}, order=[], objects={}, fail=False, save_then_fail_once=False, calls=0)
    C.update(kw)


fake = FastAPI()


@fake.post("/api/guard/v1/device/auth")
async def device_auth(req: Request):
    return {"access_token": "ga_tok", "expires_in": 1800, "paired": True}


def _ok(req: Request) -> bool:
    return req.headers.get("authorization") == "Bearer ga_tok"


@fake.post("/api/guard/v1/incidents")
async def upsert(req: Request):
    if not _ok(req):
        return JSONResponse({}, status_code=401)
    if C["fail"]:
        return JSONResponse({"error": "down"}, status_code=503)
    b = await req.json()
    C["calls"] += 1
    lid = b["local_incident_id"]
    row = C["incidents"].get(lid)
    if row is None:
        row = {"id": f"ci_{len(C['incidents']) + 1}", "payloads": []}
        C["incidents"][lid] = row
        C["order"].append(b["incident_type"])
    row["payloads"].append(b)
    if C["save_then_fail_once"]:  # saved, but the answer never reached the PC
        C["save_then_fail_once"] = False
        return JSONResponse({"error": "timeout"}, status_code=504)
    return {"incident_id": row["id"], "version": len(row["payloads"])}


@fake.post("/api/guard/v1/incidents/{cid}/uploads")
async def uploads(cid: str, req: Request):
    b = await req.json()
    C["order"].append(f"{b['media_type']}:{cid}")
    b64 = base64.b64encode(bytes.fromhex(b["sha256"])).decode()
    return {"upload_url": f"{BASE}/bucket/{cid}/{b['media_type']}", "method": "PUT",
            "headers": {"Content-Type": b["content_type"], "x-amz-checksum-sha256": b64}, "expires_in": 600}


@fake.put("/bucket/{cid}/{kind}")
async def put(cid: str, kind: str, req: Request):
    body = await req.body()
    if req.headers.get("x-amz-checksum-sha256") != base64.b64encode(hashlib.sha256(body).digest()).decode():
        return JSONResponse({}, status_code=400)
    C["objects"][(cid, kind)] = body
    return {}


@fake.post("/api/guard/v1/incidents/{cid}/uploads/complete")
async def complete(cid: str, req: Request):
    b = await req.json()
    return {"ok": True} if (cid, b["media_type"]) in C["objects"] else JSONResponse({"error": "missing"}, status_code=422)


@pytest.fixture(scope="module", autouse=True)
def cloud():
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.0)
    yield
    server.should_exit = True


@pytest.fixture()
def env(tmp_path):
    reset()
    db = Database(tmp_path / "g.db")
    with db.session() as s:
        s.add(Device(id="dev1", ip_address="10.0.0.2"))
        s.add(Camera(id="cam1", device_id="dev1", name="Main Exit"))
    link = CloudLink(db, CIPHER, BASE)
    link._store_secret("gd_dev1", SECRET)
    crypto = MediaCrypto(tmp_path / "media.key", CIPHER)
    media = MediaWorker(db, crypto, tmp_path / "incidents", lambda _c: [])
    sync = SyncQueue(db, link, media)
    sync.scan()  # sets the cursor: nothing before this is ever sent
    time.sleep(0.01)
    return db, sync, media, crypto


_seq = iter(range(1, 10_000))


def incident(env, itype="POSSIBLE_UNPAID_EXIT", sev="HIGH", snapshot=b"", clip=b""):
    db, _, media, crypto = env
    ref = f"BG-OWR-20260913-{next(_seq):06d}"
    with db.session() as s:
        i = Incident(ref=ref, camera_id="cam1", incident_type=itype, severity=sev, title=itype.title(),
                     occurred_at=datetime.now(timezone.utc), media_status="READY" if snapshot else "SKIPPED")
        if snapshot:
            crypto.write(media.dir / f"x/{ref}/snapshot.jpg.enc", snapshot, ref.encode())
            i.snapshot_path = f"x/{ref}/snapshot.jpg.enc"
        if clip:
            crypto.write(media.dir / f"x/{ref}/clip.mp4.enc", clip, ref.encode())
            i.clip_path, i.clip_duration_seconds = f"x/{ref}/clip.mp4.enc", 15.0
        s.add(i)
        s.flush()
        return i.id


def drain(sync, rounds=10):
    """Each round an hour later, so every backoff has passed."""
    for n in range(rounds):
        sync.scan()
        asyncio.run(sync.process(now=datetime(2100, 1, 1, tzinfo=timezone.utc) + timedelta(hours=n), limit=50))


def test_nothing_from_before_connecting_is_sent(tmp_path):
    reset()
    db = Database(tmp_path / "g.db")
    with db.session() as s:
        s.add(Device(id="dev1", ip_address="10.0.0.2"))
        s.add(Camera(id="cam1", device_id="dev1", name="Main Exit"))
        s.add(Incident(ref="BG-OLD-1", camera_id="cam1", incident_type="POSSIBLE_UNPAID_EXIT", severity="HIGH",
                       occurred_at=datetime.now(timezone.utc)))
    link = CloudLink(db, CIPHER, BASE)
    link._store_secret("gd_dev1", SECRET)
    sync = SyncQueue(db, link, MediaWorker(db, MediaCrypto(tmp_path / "k", CIPHER), tmp_path / "i", lambda _c: []))
    drain(sync)
    assert C["incidents"] == {}


def test_outage_then_priority_order_metadata_before_media(env):
    _, sync, _, _ = env
    C["fail"] = True  # store internet down
    incident(env, "RESTRICTED_AREA_INCIDENT", "HIGH")
    incident(env, "POSSIBLE_UNPAID_EXIT", "HIGH", snapshot=b"JPEG-exit", clip=b"MP4-exit")
    incident(env, "POSSIBLE_FIRE", "CRITICAL", snapshot=b"JPEG-fire", clip=b"MP4-fire")
    sync.scan()
    asyncio.run(sync.process())
    # 3 metadata + snapshot and clip for each of the two HIGH/CRITICAL incidents with media, all waiting locally.
    assert C["incidents"] == {} and sync.pending() == 7
    C["fail"] = False  # back online
    drain(sync)
    order = C["order"]
    assert order[:1] == ["POSSIBLE_FIRE"]
    assert order.index("POSSIBLE_UNPAID_EXIT") < order.index("RESTRICTED_AREA_INCIDENT")
    fire_id = C["incidents"][next(k for k, v in C["incidents"].items() if v["payloads"][0]["incident_type"] == "POSSIBLE_FIRE")]["id"]
    # Fire media ahead of the ordinary incidents' metadata backlog; snapshot before clip.
    assert order.index(f"SNAPSHOT:{fire_id}") < order.index(f"CLIP:{fire_id}")
    # Uploaded bytes are the decrypted media, verified by checksum.
    assert C["objects"][(fire_id, "SNAPSHOT")] == b"JPEG-fire"
    assert C["objects"][(fire_id, "CLIP")] == b"MP4-fire"
    assert sync.pending() == 0


def test_retry_after_timeout_creates_one_cloud_incident(env):
    _, sync, _, _ = env
    C["save_then_fail_once"] = True
    incident(env)
    drain(sync)
    assert len(C["incidents"]) == 1
    row = next(iter(C["incidents"].values()))
    assert len(row["payloads"]) == 2  # sent twice, one incident


def test_low_gets_snapshot_not_clip_and_info_is_skipped(env):
    _, sync, _, _ = env
    incident(env, "CAMERA_OFFLINE", "LOW", snapshot=b"JPEG", clip=b"MP4")
    incident(env, "SOMETHING", "INFO")
    drain(sync)
    assert len(C["incidents"]) == 1
    cid = next(iter(C["incidents"].values()))["id"]
    assert (cid, "SNAPSHOT") in C["objects"] and (cid, "CLIP") not in C["objects"]


def test_local_change_is_resent_and_clip_follows_severity_raise(env):
    db, sync, media, crypto = env
    iid = incident(env, "CAMERA_OFFLINE", "LOW")
    drain(sync)
    with db.session() as s:
        i = s.get(Incident, iid)
        i.severity, i.status, i.reviewed_by = "HIGH", "CONFIRMED", "Emeka"
    drain(sync)
    row = next(iter(C["incidents"].values()))
    assert row["payloads"][-1]["severity"] == "HIGH" and row["payloads"][-1]["status"] == "CONFIRMED"
    assert len(C["incidents"]) == 1


def test_missing_file_fails_media_but_metadata_arrives(env):
    db, sync, media, _ = env
    iid = incident(env, snapshot=b"JPEG")
    with db.session() as s:
        (media.dir / s.get(Incident, iid).snapshot_path).unlink()
    drain(sync)
    assert len(C["incidents"]) == 1 and C["objects"] == {}
    with db.session() as s:
        job = s.query(SyncJob).filter_by(operation_type="SNAPSHOT_UPLOAD").one()
        assert job.status == "FAILED" and "no longer" in job.last_error
    assert sync.status()["failed"] == 1


def test_never_uploads_ciphertext_or_addresses(env):
    _, sync, _, _ = env
    incident(env, snapshot=b"PLAIN-JPEG")
    drain(sync)
    body = next(iter(C["objects"].values()))
    assert body == b"PLAIN-JPEG" and not body.startswith(b"BGM1")
    payload = next(iter(C["incidents"].values()))["payloads"][0]
    assert "10.0.0.2" not in str(payload) and payload["camera_name"] == "Main Exit"


# ── §98 bandwidth modes / §99 backlog cap ────────────────────────────

def real_jpeg(w=1920, h=1080) -> bytes:
    import cv2
    import numpy as np

    img = (np.random.default_rng(1).integers(0, 255, (h, w, 3))).astype("uint8")
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def test_metadata_only_holds_media_then_resumes(env):
    _, sync, _, _ = env
    sync.bandwidth.set("METADATA_ONLY")
    incident(env, snapshot=b"JPEG", clip=b"MP4")
    drain(sync)
    assert len(C["incidents"]) == 1 and C["objects"] == {}
    assert sync.pending() == 2  # snapshot + clip waiting, not failed
    sync.bandwidth.set("NORMAL")
    drain(sync)
    assert len(C["objects"]) == 2 and sync.pending() == 0


def test_metadata_only_expires_after_24h(env):
    _, sync, _, _ = env
    sync.bandwidth.set("METADATA_ONLY", now=datetime.now(timezone.utc) - timedelta(hours=25))
    assert sync.bandwidth.mode() == "NORMAL"


def test_low_bandwidth_uploads_smaller_snapshot_keeps_local_full(env):
    db, sync, media, _ = env
    sync.bandwidth.set("LOW")
    original = real_jpeg()
    iid = incident(env, snapshot=original)
    drain(sync)
    sent = next(iter(C["objects"].values()))
    assert sent[:2] == b"\xff\xd8" and len(sent) < len(original)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert media.read(inc.snapshot_path, inc.ref) == original  # local evidence untouched


def test_backlog_cap_skips_old_clips_first_and_protects_critical(env):
    db, sync, _, _ = env
    C["fail"] = True  # offline: everything piles up
    old_exit = incident(env, "POSSIBLE_UNPAID_EXIT", "HIGH", snapshot=b"S" * 1000, clip=b"C" * 5000)
    time.sleep(0.01)
    new_exit = incident(env, "POSSIBLE_UNPAID_EXIT", "HIGH", snapshot=b"S" * 1000, clip=b"C" * 5000)
    fire = incident(env, "POSSIBLE_FIRE", "CRITICAL", snapshot=b"S" * 1000, clip=b"C" * 5000)
    sync.link._put("cloud_media_cap_bytes", "14000")  # room for ~2 clips + snapshots
    sync.scan()
    with db.session() as s:
        st = {(j.resource_id, j.operation_type): j.status for j in s.query(SyncJob)}
    assert st[(old_exit, "CLIP_UPLOAD")] == "SKIPPED"          # oldest non-critical clip goes first
    assert st[(new_exit, "CLIP_UPLOAD")] == "PENDING"          # newest kept
    assert st[(fire, "CLIP_UPLOAD")] == "PENDING"              # critical never skipped
    assert st[(old_exit, "SNAPSHOT_UPLOAD")] == "PENDING"      # snapshots outlast clips
    assert st[(old_exit, "INCIDENT_UPSERT")] == "PENDING"      # details always go
    assert sync.status()["skipped"] == 1 and "skipped" in sync.status()["warning"]
    C["fail"] = False
    drain(sync)
    assert len(C["incidents"]) == 3


def test_cap_never_skips_kept_evidence(env):
    db, sync, _, _ = env
    C["fail"] = True
    kept = incident(env, "POSSIBLE_UNPAID_EXIT", "HIGH", clip=b"C" * 5000)
    with db.session() as s:
        s.get(Incident, kept).keep_evidence = True
    sync.link._put("cloud_media_cap_bytes", "100")
    sync.scan()
    with db.session() as s:
        assert s.query(SyncJob).filter_by(resource_id=kept, operation_type="CLIP_UPLOAD").one().status == "PENDING"
    assert "critical or kept" in sync.status()["warning"]


def test_shrink_clip_falls_back_to_original_when_ffmpeg_cant_read_it():
    from app.media.compress import shrink_clip

    assert shrink_clip(b"not an mp4") == b"not an mp4"
