"""Plug-and-play Auto Setup: licence limits, computer capacity, camera
recommendations, Guard Test progress, and the setup API end to end."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database.db import Database
from app.database.models import Camera, Device, Setting, Zone
from app.licence import Licence
from app.main import create_app
from app.security.vault import _InsecureDevCipher
from app.setup.benchmark import capacity_from, verdict_for
from app.setup.guard_test import walk_progress
from app.setup.recommend import classify, recommend


# ── licence ──────────────────────────────────────────────────────────
def _db(tmp_path):
    return Database(tmp_path / "g.db")


def test_new_install_is_demo(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_MAX_CAMERAS", raising=False)
    lic = Licence(_db(tmp_path))
    assert lic.current()["status"] == "DEMO"
    assert lic.limit() == 0 and not lic.protects()


def test_install_already_protecting_keeps_two(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_MAX_CAMERAS", raising=False)
    db = _db(tmp_path)
    with db.session() as s:
        d = Device(ip_address="10.0.0.5")
        s.add(d)
        s.flush()
        s.add(Camera(device_id=d.id, channel_number=1, name="Shop", guard_enabled=True))
    lic = Licence(db)
    assert lic.current()["status"] == "GRANDFATHERED"
    assert lic.limit() == 2


def test_cloud_answers_change_the_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_MAX_CAMERAS", raising=False)
    lic = Licence(_db(tmp_path))
    lowered = []
    lic.on_lower = lowered.append
    assert lic.update({"status": "ACTIVE", "tier": "BUSINESS", "name": "Guard Business", "ai_cameras": 4})
    assert lic.limit() == 4
    assert not lic.update({"status": "ACTIVE", "tier": "BUSINESS", "name": "Guard Business", "ai_cameras": 4})
    lic.update({"status": "REVOKED", "tier": None, "name": None, "ai_cameras": 0})
    assert lic.limit() == 0 and lowered == [0]


def test_missing_or_bad_answer_never_lowers(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_MAX_CAMERAS", raising=False)
    lic = Licence(_db(tmp_path))
    lic.update({"status": "ACTIVE", "tier": "STARTER", "name": "Guard Starter", "ai_cameras": 2})
    for bad in (None, {}, {"status": "WEIRD"}, {"status": "ACTIVE", "ai_cameras": "lots"}, "ACTIVE"):
        assert not lic.update(bad)
    assert lic.limit() == 2


# ── signed licences (monthly plan) ───────────────────────────────────
@pytest.fixture
def signer(monkeypatch):
    """A throwaway Ed25519 key the agent trusts for this test."""
    import base64 as b64
    import json as js
    import time as tm

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setenv("GUARD_LICENCE_PUBKEY", b64.b64encode(raw).decode())
    monkeypatch.delenv("GUARD_MAX_CAMERAS", raising=False)

    def make(status="ACTIVE", cams=4, days=30, device="dev1", inst="inst_a", k=key):
        payload = js.dumps({"v": 1, "d": device, "i": inst, "s": status, "c": cams,
                            "u": int(tm.time() + days * 86400) if days is not None else None, "t": int(tm.time())}).encode()
        enc = lambda b: b64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
        return {"status": status, "ai_cameras": cams, "token": f"{enc(payload)}.{enc(k.sign(payload))}"}

    return make


def _signed(db, ident=("dev1", "inst_a")):
    return Licence(db, identity=lambda: ident)


def test_signed_licence_survives_restart_offline(tmp_path, signer):
    db = _db(tmp_path)
    assert _signed(db).update(signer(cams=8))
    assert _signed(db).limit() == 8  # rebooted with no internet: still protecting


def test_unsigned_licence_is_not_trusted_after_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_MAX_CAMERAS", raising=False)
    db = _db(tmp_path)
    lic = Licence(db)
    lic.update({"status": "ACTIVE", "ai_cameras": 4, "valid_until": None})
    assert lic.limit() == 4  # this run only
    assert Licence(db).limit() == 0


def test_expired_plan_stops_protection_and_lowers(tmp_path, signer):
    lic = _signed(_db(tmp_path))
    lowered = []
    lic.on_lower = lowered.append
    lic.update(signer(days=30))
    assert lic.limit() == 4
    lic.update(signer(days=-1))  # renewal never came: valid_until passed
    assert lic.limit() == 0 and lowered == [0]
    assert lic.current()["status"] == "EXPIRED"


def test_plan_runs_out_while_offline(tmp_path, signer, monkeypatch):
    import app.licence as L

    lic = _signed(_db(tmp_path))
    lowered = []
    lic.on_lower = lowered.append
    lic.update(signer(days=1))
    assert lic.check() == 4
    real = L.time.time
    monkeypatch.setattr(L.time, "time", lambda: real() + 2 * 86400)
    assert lic.check() == 0 and lowered == [0]


def test_token_for_another_pc_or_key_is_refused(tmp_path, signer):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    lic = _signed(_db(tmp_path))
    assert not lic.update(signer(device="someone-else"))
    assert not lic.update(signer(inst="inst_other"))
    assert not lic.update(signer(k=Ed25519PrivateKey.generate()))
    assert lic.limit() == 0


def test_edited_database_is_caught(tmp_path, signer):
    db = _db(tmp_path)
    _signed(db).update(signer(cams=4))
    with db.session() as s:  # the shop "upgrades" itself in guard.db
        row = s.get(Setting, "licence_json")
        d = json.loads(row.value)
        d["ai_cameras"], d["valid_until"] = 16, 4102444800
        row.value = json.dumps(d)
    assert _signed(db).limit() == 4  # the signed payload wins over the edited fields
    with db.session() as s:
        row = s.get(Setting, "licence_json")
        d = json.loads(row.value)
        d["token"] = d["token"][:-4] + "AAAA"
        row.value = json.dumps(d)
    assert _signed(db).limit() == 0


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARD_MAX_CAMERAS", "3")
    assert Licence(_db(tmp_path)).limit() == 3


# ── capacity ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("ms,cores,ram,expect", [
    (20, 8, 16, 5),      # 500 / (20·5) = 5
    (10, 8, 16, 10),
    (10, 8, 5.2, 6),     # memory-bound: (5.2-3)/0.35 = 6
    (2, 16, 64, 16),     # never more than 16
    (10, 2, 16, 1),      # 2 cores: 1 camera whatever the model speed
    (10, 8, 4, 1),       # 4 GB: 1 camera
    (150, 8, 16, 0),     # too slow
    (None, 8, 16, 0),
])
def test_capacity(ms, cores, ram, expect):
    assert capacity_from(ms, cores, ram) == expect


def test_verdicts_are_plain_language():
    assert verdict_for(0, True)[0] == "unsuitable"
    assert verdict_for(1, True)[0] == "limited"
    assert "4 cameras" in verdict_for(4, True)[1]
    assert verdict_for(3, False)[0] == "unknown"


# ── recommendations ──────────────────────────────────────────────────
def cam(i, name, online=True, compat="UNKNOWN"):
    return {"id": f"c{i}", "name": name, "channel_number": i, "online": online, "compatibility": compat,
            "has_sub_stream": True, "has_main_stream": True}


def test_classify_names():
    assert classify("Entrance")[1] == "EXIT"
    assert classify("Back Door")[1] == "EXIT"
    assert classify("Main Shop")[1] == "PRODUCTS"
    assert classify("Aisle 1")[1] == "PRODUCTS"
    assert classify("Stockroom")[1] == "STOCK"
    assert classify("Camera 01")[1] == "UNKNOWN"
    assert classify("Toilet")[0] < 0


def test_two_slots_cover_products_and_exit():
    cams = [cam(1, "Entrance"), cam(2, "Main Shop"), cam(3, "Cashier"), cam(4, "Stockroom"),
            cam(5, "Aisle 1"), cam(6, "Aisle 2"), cam(7, "Back Door"), cam(8, "Outside")]
    picked = {c.name for c in recommend(cams, {}, 2) if c.recommended}
    assert picked == {"Main Shop", "Entrance"}


def test_four_slots_and_offline_never_picked():
    cams = [cam(1, "Entrance"), cam(2, "Main Shop", online=False), cam(3, "Aisle 1"), cam(4, "Stockroom"),
            cam(5, "Back Door"), cam(6, "Toilet"), cam(7, "Shelf 3", compat="INCOMPATIBLE")]
    rec = recommend(cams, {}, 4)
    picked = {c.name for c in rec if c.recommended}
    assert "Main Shop" not in picked and "Shelf 3" not in picked and "Toilet" not in picked
    assert len(picked) == 4
    assert rec[-1].usable is False  # unusable ones sort last


def test_people_seen_break_generic_names():
    cams = [cam(1, "Camera 01"), cam(2, "Camera 02"), cam(3, "Camera 03")]
    picked = [c.id for c in recommend(cams, {"c3": 3}, 1) if c.recommended]
    assert picked == ["c3"]


def test_zero_slots_recommend_nothing():
    assert not any(c.recommended for c in recommend([cam(1, "Entrance")], {}, 0))


# ── Guard Test progress ──────────────────────────────────────────────
def test_walk_progress_needs_events_after_start():
    start = datetime.now(timezone.utc)
    before = (start - timedelta(seconds=30)).isoformat()
    after = (start + timedelta(seconds=5)).isoformat()
    zones = {"zS": "SHELF", "zE": "EXIT"}
    ev = [
        {"camera_id": "c1", "event_type": "ZONE_ENTRY", "zone_id": "zE", "occurred_at": before},  # too early
        {"camera_id": "c2", "event_type": "PERSON_DETECTED", "occurred_at": after},              # other camera
    ]
    assert walk_progress(ev, "c1", start, zones) == {"person": False, "products": False, "exit": False}
    ev += [{"camera_id": "c1", "event_type": "PERSON_DETECTED", "occurred_at": after},
           {"camera_id": "c1", "event_type": "ZONE_ENTRY", "zone_id": "zS", "occurred_at": after}]
    assert walk_progress(ev, "c1", start, zones) == {"person": True, "products": True, "exit": False}
    ev.append({"camera_id": "c1", "event_type": "EXIT_APPROACH", "occurred_at": after})
    assert walk_progress(ev, "c1", start, zones)["exit"] is True


def test_live_track_counts_as_person():
    assert walk_progress([], "c1", datetime.now(timezone.utc), {}, live_tracks=1)["person"] is True


# ── API ──────────────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_MAX_CAMERAS", raising=False)
    app = create_app(Settings(data_dir=tmp_path), cipher=_InsecureDevCipher())
    token = (tmp_path / "setup-token").read_text().strip()
    c = TestClient(app, base_url="http://127.0.0.1:7480")
    c.headers.update({"X-Guard-Token": token, "Origin": "http://127.0.0.1:7480"})
    with app.state.db.session() as s:
        d = Device(ip_address="10.0.0.9", manufacturer="Hikvision", device_type="DVR")
        s.add(d)
        s.flush()
        for i, n in enumerate(["Entrance", "Main Shop", "Stockroom"], 1):
            s.add(Camera(device_id=d.id, channel_number=i, name=n, online=True, stream_sub=f"rtsp://x/{i}"))
    return c, app


def test_found_speaks_merchant(client):
    c, _ = client
    devs = c.get("/api/v1/setup/found").json()["devices"]
    assert devs[0]["title"] == "Hikvision Recorder" and devs[0]["channels"] == 3
    assert "ip_address" not in devs[0] and "port" not in devs[0]


def test_demo_mode_cannot_protect(client):
    c, app = client
    rec = c.get("/api/v1/setup/recommendation").json()
    assert rec["licence"]["status"] == "DEMO"
    ids = [x["id"] for x in rec["cameras"] if x["recommended"]]
    assert ids  # still recommends, so the merchant sees what they'd get
    r = c.post("/api/v1/setup/apply", json={"camera_ids": ids[:1]})
    assert r.status_code == 400 and "Activate your Guard plan" in r.json()["detail"]
    r = c.post(f"/api/v1/cameras/{ids[0]}/enable")
    assert r.status_code == 409 and "isn't active" in r.json()["detail"]


def test_licensed_apply_and_areas(client):
    c, app = client
    app.state.licence.update({"status": "ACTIVE", "tier": "STARTER", "name": "Guard Starter", "ai_cameras": 2})
    rec = c.get("/api/v1/setup/recommendation").json()
    ids = [x["id"] for x in rec["cameras"] if x["recommended"]]
    assert len(ids) == 2
    assert c.post("/api/v1/setup/apply", json={"camera_ids": ids + ["extra"]}).status_code == 400
    r = c.post("/api/v1/setup/apply", json={"camera_ids": ids})
    assert r.status_code == 200 and len(r.json()["cameras"]) == 2
    rect = {"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.7}
    r = c.post("/api/v1/setup/areas", json={"camera_id": ids[0], "products": rect, "exit": {"x1": 0.7, "y1": 0.1, "x2": 0.95, "y2": 0.9}})
    assert r.status_code == 200
    types = sorted(z["zone_type"] for z in r.json()["zones"])
    assert types == ["EXIT", "SHELF"]
    # Running it again replaces the setup zones instead of piling them up.
    c.post("/api/v1/setup/areas", json={"camera_id": ids[0], "products": rect, "exit": None})
    with app.state.db.session() as s:
        zones = [z.zone_type for z in s.query(Zone).filter(Zone.camera_id == ids[0])]
    assert zones == ["SHELF"]


def test_licence_downgrade_stops_extra_cameras(client):
    c, app = client
    app.state.licence.update({"status": "ACTIVE", "tier": "BUSINESS", "name": "Guard Business", "ai_cameras": 4})
    ids = [x["id"] for x in c.get("/api/v1/setup/recommendation").json()["cameras"]]
    c.post("/api/v1/setup/apply", json={"camera_ids": ids})
    import asyncio
    asyncio.run(app.state.devices.enforce_limit(1))
    with app.state.db.session() as s:
        on = [x.name for x in s.query(Camera).filter(Camera.guard_enabled.is_(True))]
    assert on == ["Entrance"]


def test_tiny_area_is_refused_plainly(client):
    c, app = client
    app.state.licence.update({"status": "ACTIVE", "tier": "STARTER", "name": "Guard Starter", "ai_cameras": 2})
    cid = c.get("/api/v1/setup/recommendation").json()["cameras"][0]["id"]
    r = c.post("/api/v1/setup/areas", json={"camera_id": cid, "products": {"x1": 0.1, "y1": 0.1, "x2": 0.11, "y2": 0.11}})
    assert r.status_code == 400 and "too small" in r.json()["detail"]


def test_summary_has_no_addresses(client):
    _, app = client
    s = app.state.setup.summary()
    assert s["cameras_found"] == 3 and s["devices"][0]["brand"] == "Hikvision"
    assert "10.0.0.9" not in json.dumps(s)


def test_setup_complete_is_remembered(client):
    c, app = client
    assert c.get("/api/v1/setup/state").json()["setup_complete_at"] is None
    c.post("/api/v1/setup/complete", json={"test_passed": True})
    with app.state.db.session() as s:
        assert s.get(Setting, "setup_complete_at") is not None
