"""Phase 4A — activation, access tokens, heartbeat — against a fake cloud on loopback."""

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.cloud.client import CloudError, CloudLink
from app.cloud.heartbeat import Heartbeat
from app.database.db import Database
from app.database.models import Camera, Device, Incident, Setting
from app.security.vault import _InsecureDevCipher

CIPHER = _InsecureDevCipher()
SECRET = "gd_" + "s" * 43

# ── fake Boombiz cloud (Phase 4A routes) ─────────────────────────────
C: dict = {}


def reset(**kw):
    C.clear()
    C.update(auth_calls=0, tokens=set(), revoked=False, bound=None, heartbeats=[], alerts=[],
             activations=[], used_codes=set(), legacy=False, reject_next_heartbeat=False)
    C.update(kw)


fake = FastAPI()


@fake.post("/api/guard/v1/devices/activate")
async def activate(req: Request):
    b = await req.json()
    C["activations"].append(b)
    code = b["activation_code"].replace("-", "").replace(" ", "").upper()  # the real cloud normalises too
    if code != "GARD7K82HQ4A" or code in C["used_codes"]:
        return JSONResponse({"error": "That activation code didn't work."}, status_code=400)
    C["used_codes"].add(code)
    C["bound"] = b["installation_id"]
    return {"device_id": "gd_dev1", "device_secret": SECRET, "business_name": "Digital Pharmacy",
            "location_id": "loc_owerri", "location_name": "Owerri Branch", "heartbeat_seconds": 120}


@fake.post("/api/guard/v1/device/auth")
async def device_auth(req: Request):
    if C["legacy"]:
        raise HTTPException(404)
    C["auth_calls"] += 1
    body = await req.json()
    if req.headers.get("authorization") != f"Bearer {SECRET}" or C["revoked"]:
        return JSONResponse({"error": "Unknown or revoked device."}, status_code=401)
    if C["bound"] and body.get("installation_id") != C["bound"]:
        return JSONResponse({"error": "This credential belongs to another computer."}, status_code=401)
    tok = f"ga_tok{C['auth_calls']}"
    C["tokens"].add(tok)
    paired = C.get("paired", True)
    return {"access_token": tok, "expires_in": 1800, "paired": paired,
            "business_name": "Digital Pharmacy" if paired else None, "location_name": "Owerri Branch" if paired else None}


def _access_ok(req: Request) -> bool:
    h = req.headers.get("authorization", "")
    return h.removeprefix("Bearer ") in C["tokens"] and not C["revoked"]


@fake.post("/api/guard/v1/devices/{device_id}/heartbeat")
async def heartbeat(device_id: str, req: Request):
    if C["legacy"]:
        raise HTTPException(404)
    if C["reject_next_heartbeat"]:
        C["reject_next_heartbeat"] = False
        C["tokens"].clear()  # token rotated server-side
        return JSONResponse({"error": "Unknown device."}, status_code=401)
    if not _access_ok(req):
        return JSONResponse({"error": "Unknown device."}, status_code=401)
    C["heartbeats"].append({"device_id": device_id, "auth": req.headers["authorization"], **(await req.json())})
    return {"status": "ONLINE", "reasons": [], "paired": True, "business_name": "Digital Pharmacy",
            "location_name": "Owerri Branch", "next_heartbeat_seconds": 120, "commands": []}


@fake.get("/api/guard/v1/device")
async def device(req: Request):
    C["device_checks"] = C.get("device_checks", 0) + 1
    h = req.headers.get("authorization", "")
    if not (_access_ok(req) or (C["legacy"] and h == f"Bearer {SECRET}")):
        return JSONResponse({"error": "Unknown device."}, status_code=401)
    paired = C.get("paired", True)
    return {"paired": paired, "business_name": "Digital Pharmacy" if paired else None}


@fake.post("/api/guard/v1/alerts")
async def alerts(req: Request):
    h = req.headers.get("authorization", "")
    if not (_access_ok(req) or (C["legacy"] and h == f"Bearer {SECRET}")):
        return JSONResponse({"error": "Unknown device."}, status_code=401)
    C["alerts"].append({"auth": h, **(await req.json())})
    return {"accepted": True, "pushed": 1}


@pytest.fixture(scope="module")
def cloud_url():
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=7498, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.0)
    yield "http://127.0.0.1:7498"
    server.should_exit = True


@pytest.fixture()
def db(tmp_path):
    reset()
    d = Database(tmp_path / "g.db")
    with d.session() as s:
        s.add(Device(id="dev1", ip_address="192.168.1.64"))
        s.add(Camera(id="cam1", device_id="dev1", name="Main Exit", guard_enabled=True,
                     stream_main="rtsp://192.168.1.64:554/Streaming/Channels/101"))
        s.add(Camera(id="cam2", device_id="dev1", name="Entrance", guard_enabled=True))
        s.add(Camera(id="cam3", device_id="dev1", name="Office", guard_enabled=False))
    return d


class FakeStreams:
    def health(self):
        return {"cam1": {"status": "ONLINE"}, "cam2": {"status": "OFFLINE"}}


class FakeAI:
    def status(self):
        return {"running": True, "cameras": [{"ai_fps": 7.0}, {"ai_fps": 8.0}]}


def activated(db, cloud_url) -> CloudLink:
    link = CloudLink(db, CIPHER, cloud_url)
    asyncio.run(link.activate("gard-7k82-hq4a", "OWR Guard PC", "0.4.0"))
    return link


def test_activation_stores_secret_encrypted_and_binds_installation(db, cloud_url):
    link = activated(db, cloud_url)
    assert link.state["paired"] and link.state["location_name"] == "Owerri Branch"
    with db.session() as s:
        assert SECRET not in s.get(Setting, "cloud_device_token").value  # encrypted at rest
        iid = s.get(Setting, "installation_id").value
    sent = C["activations"][0]
    assert sent["installation_id"] == iid and len(sent["fingerprint_hash"]) == 64
    # The same PC keeps its installation id across restarts.
    assert CloudLink(db, CIPHER, cloud_url).auth.installation_id() == iid


def test_bad_or_reused_code_is_explained(db, cloud_url):
    link = activated(db, cloud_url)
    with pytest.raises(CloudError, match="didn't work"):
        asyncio.run(link.activate("GARD-7K82-HQ4A", "OWR Guard PC", "0.4.0"))  # one-use
    with pytest.raises(CloudError, match="didn't work"):
        asyncio.run(CloudLink(db, CIPHER, cloud_url).activate("GARD-AAAA-BBBB", "x", "0.4.0"))


def test_secret_goes_only_to_device_auth_and_token_is_reused(db, cloud_url):
    link = activated(db, cloud_url)
    add = lambda k: link.enqueue_alert({"key": k, "severity": "HIGH", "occurred_at": datetime.now(timezone.utc).isoformat(),
                                        "incident_type": "POSSIBLE_UNPAID_EXIT", "title": "Possible unpaid exit"})
    add("a:HIGH")
    add("b:HIGH")
    asyncio.run(link.flush())
    asyncio.run(link.refresh())
    assert len(C["alerts"]) == 2
    assert all(a["auth"].startswith("Bearer ga_") for a in C["alerts"])  # never the secret
    assert C["auth_calls"] == 1  # cached across calls


def test_heartbeat_reports_health_without_addresses(db, cloud_url):
    link = activated(db, cloud_url)
    with db.session() as s:
        s.add(Incident(ref="BG-OWR-20260913-000184", camera_id="cam1", incident_type="POSSIBLE_UNPAID_EXIT",
                       severity="HIGH", occurred_at=datetime(2026, 9, 13, 12, 28, tzinfo=timezone.utc)))
    from app.cloud.heartbeat import collect_health

    hb = Heartbeat(link, lambda: collect_health(db, FakeStreams(), FakeAI(), link, "0.4.0"))
    d = asyncio.run(hb.beat())
    assert d["status"] == "ONLINE" and link.state["health_status"] == "ONLINE"
    sent = C["heartbeats"][0]
    assert sent["device_id"] == "gd_dev1" and sent["auth"].startswith("Bearer ga_")
    assert sent["camera_total"] == 2 and sent["camera_online"] == 1  # Office isn't guard-enabled
    assert {c["name"]: c["online"] for c in sent["cameras"]} == {"Main Exit": True, "Entrance": False}
    assert sent["ai_fps"] == 7.5 and sent["ai_running"] is True and sent["alarm_available"] is None
    assert sent["last_incident_at"].startswith("2026-09-13T12:28")
    blob = str(sent)
    assert "192.168" not in blob and "rtsp" not in blob and SECRET not in blob


def test_heartbeat_reauthenticates_once_after_token_rotation(db, cloud_url):
    link = activated(db, cloud_url)
    hb = Heartbeat(link, lambda: {"agent_version": "0.4.0", "camera_online": 0, "camera_total": 0})
    asyncio.run(hb.beat())
    C["reject_next_heartbeat"] = True
    d = asyncio.run(hb.beat())
    assert d and d["status"] == "ONLINE"
    assert C["auth_calls"] == 2 and len(C["heartbeats"]) == 2


def test_revoked_device_stops_and_says_so(db, cloud_url):
    link = activated(db, cloud_url)
    C["revoked"] = True
    link.auth.invalidate()
    hb = Heartbeat(link, lambda: {"agent_version": "0.4.0"})
    assert asyncio.run(hb.beat()) is None
    assert link.state["paired"] is False and "revoked" in link.state["last_error"].lower()
    assert C["heartbeats"] == []


def test_secret_copied_to_another_pc_is_refused(db, cloud_url, tmp_path):
    activated(db, cloud_url)
    other = Database(tmp_path / "other.db")  # a different installation with the same secret
    link2 = CloudLink(other, CIPHER, cloud_url)
    link2._store_secret("gd_dev1", SECRET)
    asyncio.run(link2.refresh())
    assert link2.state["paired"] is False and "another computer" in link2.state["last_error"]


def test_linked_pc_skips_the_minute_link_check_while_heartbeats_answer_it(db, cloud_url):
    link = activated(db, cloud_url)
    hb = Heartbeat(link, lambda: {"agent_version": "0.4.1"})
    asyncio.run(hb.beat())
    assert link.state["paired"] and link.state["location_name"] == "Owerri Branch"
    for _ in range(10):  # ten minutes of the 60 s loop
        asyncio.run(link.refresh_if_due())
    assert C.get("device_checks", 0) == 0
    link._last_link_check -= 16 * 60  # nothing confirmed the link for 16 min → ask once
    asyncio.run(link.refresh_if_due())
    assert C["device_checks"] == 1


def test_unlinked_pc_still_checks_every_time(db, cloud_url):
    C["paired"] = False  # the owner hasn't typed the code on their phone yet
    link = CloudLink(db, CIPHER, cloud_url)
    link._store_secret("gd_dev1", SECRET)
    link.state.update(pairing_code="ABCD-EFGH", paired=False)  # waiting for the owner's phone
    asyncio.run(link.refresh_if_due())
    asyncio.run(link.refresh_if_due())
    assert C["device_checks"] == 2


def test_old_cloud_without_device_auth_falls_back_to_secret(db, cloud_url):
    reset(legacy=True)
    link = CloudLink(db, CIPHER, cloud_url)
    link._store_secret("gd_dev1", SECRET)  # phone-paired before 4A
    link.enqueue_alert({"key": "x:HIGH", "severity": "HIGH", "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "incident_type": "POSSIBLE_UNPAID_EXIT", "title": "Possible unpaid exit"})
    r = asyncio.run(link.flush())
    assert r["sent"] == 1 and link.auth.legacy is True
    assert asyncio.run(Heartbeat(link, lambda: {}).beat()) is None  # no heartbeat route: quiet no-op
