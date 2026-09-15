"""Guard Test — the merchant proves the installation works (plug-and-play).

Instead of wondering whether setup worked, the merchant walks the camera:

    1. Walk in front of the camera     → a person is detected
    2. Walk to the product area        → Guard sees them in the products zone
    3. Walk to the exit                → Guard sees them at the exit

then Guard checks the rest of the chain by itself:

    incident created · snapshot captured · clip saved · local alarm
    · cloud connected · test alert sent to the owner's phone

Walking steps are read from the AI's own event feed (the same events that
build real incidents), so a pass here means the real pipeline saw it. A step
the merchant can't do right now can be skipped; a skipped step never counts
as passed.

The test incident is type GUARD_TEST, severity LOW: it never sounds the
store alarm, never pops up, and is never synced to the cloud as a real alert.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..database.models import AlarmOutput, Incident, Zone

log = logging.getLogger(__name__)

WALK_STEPS = ("person", "products", "exit")
CHECKS = ("incident", "snapshot", "clip", "local_alert", "cloud", "phone")
MEDIA_WAIT_S = 30

PERSON_EVENTS = {"PERSON_DETECTED", "ZONE_ENTRY", "ZONE_EXIT", "EXIT_APPROACH", "SHELF_INTERACTION",
                 "UNRESOLVED_SHELF_INTERACTION", "POSSIBLE_UNPAID_EXIT", "RESTRICTED_ZONE_ENTRY", "AFTER_HOURS_PERSON"}
PRODUCT_EVENTS = {"SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION"}
EXIT_EVENTS = {"EXIT_APPROACH", "POSSIBLE_UNPAID_EXIT"}


def _utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def walk_progress(events: list[dict], camera_id: str, since: datetime, zone_types: dict[str, str],
                  live_tracks: int = 0) -> dict[str, bool]:
    """Pure: which walking steps the AI has seen on this camera since the test began."""
    seen = {"person": live_tracks > 0, "products": False, "exit": False}
    for e in events:
        if e.get("camera_id") != camera_id:
            continue
        at = _utc(e.get("occurred_at"))
        if at is None or at < since:
            continue
        et = e.get("event_type")
        ztype = zone_types.get(e.get("zone_id") or "")
        if et in PERSON_EVENTS:
            seen["person"] = True
        if et in PRODUCT_EVENTS or (et == "ZONE_ENTRY" and ztype == "SHELF"):
            seen["products"] = True
        if et in EXIT_EVENTS or (et == "ZONE_ENTRY" and ztype == "EXIT"):
            seen["exit"] = True
    return seen


class GuardTest:
    def __init__(self, db, ai, ai_events, incidents, alarms, cloud, licence) -> None:  # noqa: ANN001
        self.db = db
        self.ai = ai
        self.ai_events = ai_events
        self.incidents = incidents
        self.alarms = alarms
        self.cloud = cloud
        self.licence = licence
        self.session: dict | None = None
        self._task: asyncio.Task | None = None

    def _zone_types(self, camera_id: str) -> dict[str, str]:
        with self.db.session() as s:
            return {z.id: z.zone_type for z in s.scalars(select(Zone).where(Zone.camera_id == camera_id))}

    def start(self, camera_id: str) -> dict:
        if camera_id not in self.ai.workers:
            raise ValueError("Guard AI isn't watching this camera yet. Give it a few seconds and try again.")
        self.session = {
            "camera_id": camera_id, "started_at": datetime.now(timezone.utc).isoformat(),
            "skipped": [], "checks": {k: {"state": "waiting", "note": None} for k in CHECKS},
            "checks_running": False, "incident_id": None,
        }
        self.db.audit("guard_test_started", camera_id)
        return self.status()

    def skip(self, step: str) -> dict:
        if not self.session or step not in WALK_STEPS:
            raise ValueError("Start the test first.")
        if step not in self.session["skipped"]:
            self.session["skipped"].append(step)
        return self.status()

    def status(self) -> dict:
        s = self.session
        if not s:
            return {"running": False}
        cam = s["camera_id"]
        w = self.ai.workers.get(cam)
        live = sum(1 for t in (w.snapshot.get("tracks", []) if w else []) if t.get("state") != "ENDED")
        walk = walk_progress(list(self.ai_events.feed), cam, _utc(s["started_at"]), self._zone_types(cam), live)
        steps = {k: ("ok" if walk[k] else "skipped" if k in s["skipped"] else "waiting") for k in WALK_STEPS}
        checks = s["checks"]
        done = all(c["state"] in ("ok", "failed", "skipped") for c in checks.values())
        # Passed = every walking step actually seen AND every check that can
        # pass did (a shop without internet can't send the phone alert yet).
        passed = done and all(v == "ok" for v in steps.values()) and all(
            c["state"] in ("ok", "skipped") for c in checks.values()) and checks["incident"]["state"] == "ok"
        return {"running": True, "camera_id": cam, "steps": steps, "checks": checks,
                "checks_running": s["checks_running"], "checks_done": done, "passed": passed}

    # ── the automatic half ───────────────────────────────────────────
    def run_checks(self) -> dict:
        if not self.session:
            raise ValueError("Start the test first.")
        if not self.session["checks_running"]:
            self.session["checks_running"] = True
            for c in self.session["checks"].values():
                c.update(state="waiting", note=None)
            self._task = asyncio.get_running_loop().create_task(self._checks())
        return self.status()

    def _set(self, key: str, state: str, note: str | None = None) -> None:
        if self.session:
            self.session["checks"][key] = {"state": state, "note": note}

    async def _checks(self) -> None:
        s = self.session
        cam = s["camera_id"]
        try:
            try:
                iid = self.incidents.test_incident(cam)
                s["incident_id"] = iid
                self._set("incident", "ok")
            except Exception as e:  # noqa: BLE001
                log.exception("test incident failed")
                self._set("incident", "failed", str(e) or "Guard couldn't record an incident.")
                iid = None
            await asyncio.gather(self._media(iid), self._local_alert(), self._cloud_and_phone())
        finally:
            s["checks_running"] = False
            self.db.audit("guard_test_finished", cam, passed=self.status().get("passed"))

    async def _media(self, iid: str | None) -> None:
        if not iid:
            self._set("snapshot", "failed", "No incident to take a picture for.")
            self._set("clip", "failed", "No incident to record.")
            return
        for _ in range(MEDIA_WAIT_S * 2):
            with self.db.session() as s:
                inc = s.get(Incident, iid)
                snap, clip, dur, status = inc.snapshot_path, inc.clip_path, inc.clip_duration_seconds, inc.media_status
            if snap and self.session["checks"]["snapshot"]["state"] != "ok":
                self._set("snapshot", "ok")
            if clip:
                self._set("clip", "ok", f"{round(dur)}-second clip saved" if dur else None)
                return
            if status in ("UNAVAILABLE", "SKIPPED"):
                break
            await asyncio.sleep(0.5)
        if self.session["checks"]["snapshot"]["state"] != "ok":
            self._set("snapshot", "failed", "No picture was saved. Check the computer has free disk space.")
        self._set("clip", "failed", "No clip was saved. Check the computer has free disk space.")

    async def _local_alert(self) -> None:
        with self.db.session() as s:
            out = s.scalar(select(AlarmOutput).where(AlarmOutput.kind == "PC_SOUND", AlarmOutput.enabled.is_(True)))
            oid = out.id if out else None
        if not oid:
            self._set("local_alert", "failed", "The computer's alarm sound is switched off in Alarms.")
            return
        try:
            r = await self.alarms.test(oid, 1)
            self._set("local_alert", "ok" if r.get("ok") else "failed",
                      None if r.get("ok") else "The computer's speaker didn't respond.")
        except Exception as e:  # noqa: BLE001
            self._set("local_alert", "failed", str(e) or "The computer's speaker didn't respond.")

    async def _cloud_and_phone(self) -> None:
        state = await self.cloud.refresh()
        if not state.get("paired"):
            self._set("cloud", "failed", "This computer isn't linked to your Boombiz account.")
            self._set("phone", "skipped", "Link this computer first.")
            return
        if state.get("online") is False:
            self._set("cloud", "failed", "No internet. Guard still protects the shop; phone alerts wait for the connection.")
            self._set("phone", "skipped", "Needs the internet.")
            return
        self._set("cloud", "ok")
        try:
            r = await self.cloud.send_test_alert()
        except Exception as e:  # noqa: BLE001
            self._set("phone", "failed", str(e) or "The test alert couldn't be sent.")
            return
        if r.get("ok"):
            who = ", ".join(r.get("recipients") or []) or "your phone"
            self._set("phone", "ok", f"Sent to {who}")
        else:
            self._set("phone", "failed", "Nobody is set up to receive alerts yet. Add yourself under Recipients in Boombiz Guard.")
