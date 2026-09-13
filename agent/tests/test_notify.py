"""Pop-up feed + cloud outbox, against a fake cloud on loopback."""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn
from fastapi import FastAPI, Request

from app.cloud.client import CloudLink
from app.database.db import Database
from app.database.models import AuditLog, Camera, CloudOutbox, Device, Incident
from app.notify.feed import feed
from app.security.vault import _InsecureDevCipher

CIPHER = _InsecureDevCipher()

# ── fake Boombiz cloud ───────────────────────────────────────────────
CLOUD = {"alerts": [], "paired": False, "fail": False}
fake = FastAPI()


@fake.post("/api/guard/v1/pair/start")
async def pair_start(req: Request):
    return {"device_id": "gd1", "device_token": "gd_" + "x" * 40, "pairing_code": "ABCD-EFGH",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}


@fake.get("/api/guard/v1/device")
async def device(req: Request):
    return {"paired": CLOUD["paired"], "business_name": "Lab Shop" if CLOUD["paired"] else None}


@fake.post("/api/guard/v1/alerts")
async def alerts(req: Request):
    if CLOUD["fail"]:
        from fastapi import HTTPException

        raise HTTPException(503)
    body = await req.json()
    if any(a["key"] == body["key"] for a in CLOUD["alerts"]):
        from fastapi.responses import JSONResponse

        return JSONResponse({"duplicate": True}, status_code=409)
    CLOUD["alerts"].append(body)
    return {"accepted": True, "pushed": 1 if body["push"] else 0}


@pytest.fixture(scope="module")
def cloud_url():
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=7497, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.0)
    yield "http://127.0.0.1:7497"
    server.should_exit = True


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "g.db")
    with d.session() as s:
        s.add(Device(id="dev1", ip_address="10.0.0.2"))
        s.add(Camera(id="cam1", device_id="dev1", name="Main Exit"))
    return d


def add_incident(db, sev, itype="POSSIBLE_UNPAID_EXIT", ago_min=0):
    with db.session() as s:
        i = Incident(ref=f"BG-LOC-20260913-{time.monotonic_ns() % 999999:06d}", camera_id="cam1",
                     incident_type=itype, severity=sev, title=itype.replace("_", " ").capitalize(),
                     occurred_at=datetime.now(timezone.utc) - timedelta(minutes=ago_min))
        s.add(i)
        s.flush()
        return i.id


def test_feed_pops_high_critical_and_alarm_failures_only(db):
    start = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    add_incident(db, "LOW")
    add_incident(db, "HIGH")
    add_incident(db, "CRITICAL", "POSSIBLE_FIRE")
    db.audit("ALARM_OUTPUT_FAILURE", "alarm1", output="Door relay", error="x")
    items = feed(db, start)["items"]
    assert sorted(i["severity"] for i in items) == ["CRITICAL", "HIGH", "HIGH"]
    assert any(i["kind"] == "alarm_failure" and "Door relay" in i["title"] for i in items)
    assert next(i for i in items if i["incident_type"] == "POSSIBLE_FIRE")["fire"] is True
    assert all(i["camera"] in ("Main Exit", None) for i in items)


def test_camera_offline_pops_when_it_becomes_high(db):
    start = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    iid = add_incident(db, "LOW", "CAMERA_OFFLINE")
    assert feed(db, start)["items"] == []
    with db.session() as s:
        s.get(Incident, iid).severity = "HIGH"
    items = feed(db, start)["items"]
    assert len(items) == 1 and items[0]["key"].endswith(":HIGH") and items[0]["group"] == "health"


def test_outbox_queues_offline_then_delivers_once(db, cloud_url):
    link = CloudLink(db, CIPHER, cloud_url)
    asyncio.run(link.start_pairing("LAG Guard PC", "0.3"))
    assert link.state["pairing_code"] == "ABCD-EFGH"
    raw = db.session  # token is stored encrypted, never plain
    with raw() as s:
        from app.database.models import Setting

        assert "gd_" not in s.get(Setting, "cloud_device_token").value
    start = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    add_incident(db, "HIGH")
    for item in feed(db, start)["items"]:
        link.enqueue_alert(item)
        link.enqueue_alert(item)  # idempotent
    assert link.queued() == 1

    CLOUD.update(alerts=[], fail=True)                     # cloud down / no internet
    r = asyncio.run(link.flush())
    assert r["sent"] == 0 and link.queued() == 1
    CLOUD["fail"] = False                                   # back online (skip the backoff wait)
    r = asyncio.run(link.flush(now=datetime.now(timezone.utc) + timedelta(minutes=10)))
    assert r["sent"] == 1 and link.queued() == 0 and len(CLOUD["alerts"]) == 1
    assert CLOUD["alerts"][0]["push"] is True


def test_stale_high_is_dashboard_only_but_critical_still_buzzes(db, cloud_url):
    link = CloudLink(db, CIPHER, cloud_url)
    asyncio.run(link.start_pairing("LAG Guard PC", "0.3"))
    CLOUD.update(alerts=[], fail=False)
    start = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    add_incident(db, "HIGH", ago_min=8 * 60)                           # happened during an 8 h outage
    add_incident(db, "CRITICAL", "POSSIBLE_FIRE", ago_min=8 * 60)
    for item in feed(db, start)["items"]:
        link.enqueue_alert(item)
    asyncio.run(link.flush())
    push = {a["incident_type"]: a["push"] for a in CLOUD["alerts"]}
    assert push == {"POSSIBLE_UNPAID_EXIT": False, "POSSIBLE_FIRE": True}


def test_unpaired_pc_keeps_alerts_queued(db, cloud_url):
    link = CloudLink(db, CIPHER, cloud_url)
    start = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    add_incident(db, "HIGH")
    for item in feed(db, start)["items"]:
        link.enqueue_alert(item)
    r = asyncio.run(link.flush())                                        # no token at all
    assert r["sent"] == 0 and link.queued() == 1
