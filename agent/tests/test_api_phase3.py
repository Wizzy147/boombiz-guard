"""Phase 3 over real HTTP routes. Regression: the generic
POST /incidents/{id}/{action} route was registered before /keep and
/media-ticket and swallowed both — Keep Evidence and clip playback were
broken over HTTP while the service-level tests stayed green."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database.models import Camera, Device
from app.main import create_app
from app.security.vault import _InsecureDevCipher


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), cipher=_InsecureDevCipher())
    with app.state.db.session() as s:
        s.add(Device(id="dev1", ip_address="10.0.0.2"))
        s.add(Camera(id="cam1", device_id="dev1", name="Main Exit", guard_enabled=True))
    token = (tmp_path / "setup-token").read_text().strip()
    c = TestClient(app, base_url="http://127.0.0.1:7480")  # no lifespan: no AI/streams needed
    c.headers.update({"X-Guard-Token": token, "Host": "127.0.0.1:7480"})
    owner = c.post("/api/v1/users", json={"name": "Ada", "role": "OWNER", "pin": "1234"}).json()
    sess = c.post("/api/v1/auth/signin", json={"user_id": owner["id"], "pin": "1234"}).json()["session"]
    c.headers["X-Guard-Session"] = sess
    app.state.incidents.on_ai_event({"camera_id": "cam1", "track_id": "t1", "event_type": "POSSIBLE_UNPAID_EXIT",
                                     "confidence": "MEDIUM", "metadata": {},
                                     "occurred_at": datetime.now(timezone.utc).isoformat()})
    iid = c.get("/api/v1/incidents").json()["incidents"][0]["id"]
    return c, iid


def test_keep_evidence_route_is_not_swallowed(client):
    c, iid = client
    r = c.post(f"/api/v1/incidents/{iid}/keep", json={"keep": True})
    assert r.status_code == 200 and r.json()["keep_evidence"] is True


def test_media_ticket_route_is_not_swallowed(client):
    c, iid = client
    r = c.post(f"/api/v1/incidents/{iid}/media-ticket?kind=clip")
    assert r.status_code == 200 and r.json()["ticket"]


def test_review_actions_still_route(client):
    c, iid = client
    assert c.post(f"/api/v1/incidents/{iid}/acknowledge").json()["status"] == "ACKNOWLEDGED"
    r = c.post(f"/api/v1/incidents/{iid}/false-alert", json={"reason": "CUSTOMER_PAID"})
    assert r.status_code == 200 and r.json()["status"] == "FALSE_ALERT"
    assert c.post(f"/api/v1/incidents/{iid}/not-an-action").status_code == 404


def test_times_carry_a_timezone(client):
    """Regression: SQLite drops tzinfo, so occurred_at went out as naive UTC
    and the browser showed incident times an hour early in Lagos."""
    from datetime import datetime

    c, iid = client
    c.post(f"/api/v1/incidents/{iid}/acknowledge")
    d = c.get(f"/api/v1/incidents/{iid}").json()
    for key in ("occurred_at", "acknowledged_at"):
        assert datetime.fromisoformat(d[key]).tzinfo is not None, key
    for row in c.get("/api/v1/incidents").json()["incidents"]:
        assert datetime.fromisoformat(row["occurred_at"]).tzinfo is not None


def test_export_is_a_zip(client):
    c, iid = client
    r = c.get(f"/api/v1/incidents/{iid}/export")
    assert r.status_code == 200 and r.content[:2] == b"PK"
