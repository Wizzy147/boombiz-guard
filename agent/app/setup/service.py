"""Auto Setup — the plug-and-play path (owner decision 2026-09-15).

    Start Automatic Setup
      → three checks at once: this computer (real AI benchmark), the local
        network + internet, and CCTV discovery
      → "We found your CCTV system" → the CCTV username/password, nothing else
      → recommended cameras for the licence (or, in demo mode, "Guard Ready"
        and Activate → browser sign-in → package → pay)
      → tap where products are, tap where customers leave
      → Guard Test → YOUR BUSINESS IS PROTECTED

The merchant never sees RTSP, ONVIF, codecs, ports or substreams here: the
device service picks the smallest usable stream, the adapters pick the
protocol. Everything technical stays in Advanced Setup (the Phase 1 screens).

Target: a supported CCTV system is protected in 10–15 minutes with no
technician. Each milestone is reported (setup/telemetry.py) so we can see
how close real Nigerian installs get.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select

from ..database.models import Camera, Setting, Zone
from ..discovery.scanner import local_subnets
from ..zones.geometry import PolygonError, validate_polygon
from .benchmark import BenchResult, grab_frames, run_benchmark
from .recommend import recommend

log = logging.getLogger(__name__)

SETUP_ZONE_SUFFIX = " (Setup)"
DONE_KEY = "setup_complete_at"
# Tiers the cloud sells, largest first. Only used to cap a demo-mode
# recommendation; prices always come from the cloud's /link page.
LARGEST_PACKAGE = 8


class SetupService:
    def __init__(self, *, db, devices, streams, ai, licence, cloud, telemetry, version: str) -> None:  # noqa: ANN001
        self.db = db
        self.devices = devices
        self.streams = streams
        self.ai = ai
        self.licence = licence
        self.cloud = cloud
        self.telemetry = telemetry
        self.version = version
        self.checks: dict = self._fresh_checks()
        self.bench: BenchResult | None = None
        self.people: dict[str, int] = {}
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    def _fresh_checks() -> dict:
        return {"computer": {"state": "idle"}, "network": {"state": "idle"}, "cctv": {"state": "idle"}}

    # ── overall state ────────────────────────────────────────────────
    def complete_at(self) -> str | None:
        with self.db.session() as s:
            row = s.get(Setting, DONE_KEY)
            return row.value if row else None

    def state(self) -> dict:
        cams = self.devices.cameras()
        return {
            "licence": self.licence.current(),
            "cloud": {k: self.cloud.state.get(k) for k in ("paired", "business_name", "location_name", "online",
                                                           "link_url", "link_code", "last_error")},
            "setup_complete_at": self.complete_at(),
            "capacity": self.bench.capacity if self.bench else None,
            "protected_cameras": sum(1 for c in cams if c["guard_enabled"]),
            "cameras": len(cams),
            "run": {k: self.telemetry.run.get(k) for k in ("run_id", "started_at")},
        }

    # ── step 1: the three automatic checks ───────────────────────────
    def start(self) -> dict:
        self.telemetry.start("AUTO")
        self.checks = self._fresh_checks()
        self.bench = None
        self.people = {}
        return self.start_checks()

    def start_checks(self) -> dict:
        if any(c["state"] == "running" for c in self.checks.values()):
            return self.checks_status()
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._computer()), loop.create_task(self._network()),
                       loop.create_task(self._cctv())]
        return self.checks_status()

    async def _detector(self):  # noqa: ANN202
        if self.ai.detector is None:
            await asyncio.to_thread(self.ai._load_model)
        return self.ai.detector

    async def _computer(self) -> None:
        self.checks["computer"] = {"state": "running"}
        try:
            online = [c["id"] for c in self.devices.cameras() if c["online"]][:8]
            frames = await grab_frames(self.streams, online, 6.0) if online else {}
            self.bench = await run_benchmark(await self._detector(), frames)
            self.people.update(self.bench.people)
            ok = self.bench.capacity > 0
            self.checks["computer"] = {"state": "ok" if ok else "failed", **self.bench.to_dict()}
            self.telemetry.note(capacity=self.bench.capacity)
        except Exception:
            log.exception("computer check failed")
            self.checks["computer"] = {"state": "failed", "message": "Guard couldn't test this computer. Restart it and try again."}

    async def _network(self) -> None:
        self.checks["network"] = {"state": "running"}
        lan = bool(local_subnets())
        internet = False
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self.cloud.base}/api/guard/v1/device")
            internet = r.status_code in (200, 401)
        except httpx.HTTPError:
            internet = False
        msg = ("Connected to the shop network and the internet." if lan and internet
               else "No internet. Guard can still protect the shop; phone alerts wait until it's back." if lan
               else "This computer isn't on a network. Plug it into the same router as the CCTV recorder.")
        self.checks["network"] = {"state": "ok" if lan else "failed", "lan": lan, "internet": internet, "message": msg}

    async def _cctv(self) -> None:
        self.checks["cctv"] = {"state": "running"}
        try:
            await self.devices.run_discovery()
        except Exception:
            log.exception("discovery failed")
        found = self.found()
        cams = sum(d["channels"] for d in found)
        self.checks["cctv"] = {"state": "ok" if found else "failed", "devices": len(found),
                               "message": None if found else "Guard didn't find any CCTV on this network."}
        self.telemetry.report("discovered", devices_found=len(found), cameras_found=cams,
                              auto_discovered=bool(found), brands=sorted({d["brand"] for d in found if d["brand"]}))

    def checks_status(self) -> dict:
        disc = self.devices.discovery_status()
        cctv = dict(self.checks["cctv"])
        if cctv.get("state") == "running":
            cctv["progress"] = round(100 * disc["checked"] / disc["total"]) if disc.get("total") else None
        return {"computer": self.checks["computer"], "network": self.checks["network"], "cctv": cctv,
                "done": all(c["state"] in ("ok", "failed") for c in self.checks.values())}

    # ── step 2: what was found, and connecting it ────────────────────
    def found(self) -> list[dict]:
        """Devices in merchant words. No IP address, port or protocol."""
        out = []
        for d in self.devices.devices():
            kind = d["device_type"]
            brand = d["manufacturer"]
            noun = "Recorder" if kind in ("DVR", "NVR") else "Camera" if kind == "CAMERA" else "CCTV system"
            out.append({
                "id": d["id"], "brand": brand, "kind": kind,
                "title": f"{brand} {noun}" if brand else ("CCTV recorder" if noun == "Recorder" else f"CCTV {noun.lower()}"
                                                          if noun != "CCTV system" else "CCTV system"),
                "channels": d["channel_count"], "needs_login": d["authentication_required"],
                "connected": not d["authentication_required"] and d["channel_count"] > 0,
                "incompatible": d["compatibility"] == "INCOMPATIBLE",
                "problem": d["compatibility_reason"] or d["auth_error"],
            })
        # Recorders first: one login gives every camera. Before sign-in the
        # type is unknown, so a known brand (usually the recorder) leads.
        return sorted(out, key=lambda x: (x["incompatible"], x["kind"] not in ("DVR", "NVR"), not x["brand"],
                                          not x["channels"], x["title"]))

    async def connect(self, device_id: str, username: str, password: str) -> dict:
        self.telemetry.bump("connect_attempts")
        r = await self.devices.authenticate(device_id, username, password)
        if r.get("ok"):
            cams = len(self.devices.cameras())
            self.telemetry.report("connected", connect_ok=True, cameras_found=cams)
        return {"ok": bool(r.get("ok")), "error": r.get("error"), "found": self.found()}

    # ── step 3: which cameras ────────────────────────────────────────
    async def survey(self) -> dict:
        """Look at every working camera once: how busy is it? Also re-measures
        this computer on real CCTV frames (more honest than the first guess)."""
        online = [c["id"] for c in self.devices.cameras() if c["online"]][:32]
        frames = await grab_frames(self.streams, online, 8.0) if online else {}
        det = await self._detector()
        if frames and det is not None:
            self.bench = await run_benchmark(det, frames, seconds=4.0)
            self.people.update(self.bench.people)
            self.checks["computer"] = {"state": "ok" if self.bench.capacity > 0 else "failed", **self.bench.to_dict()}
            self.telemetry.note(capacity=self.bench.capacity)
        return self.recommendation()

    def recommendation(self) -> dict:
        cams = self.devices.cameras()
        capacity = self.bench.capacity if self.bench else None
        lic = self.licence.current()
        usable = [c for c in cams if c["online"] and c["compatibility"] != "INCOMPATIBLE"]
        if lic["protects"]:
            slots = lic["limit"]
            if capacity is not None:
                slots = min(slots, capacity) if capacity > 0 else slots
        else:
            slots = min(len(usable), capacity if capacity is not None else LARGEST_PACKAGE, LARGEST_PACKAGE)
        ranked = recommend(cams, self.people, max(0, slots))
        return {
            "cameras": [r.to_dict() for r in ranked],
            "slots": slots, "capacity": capacity, "licence": lic,
            "over_capacity": bool(lic["protects"] and capacity is not None and 0 < capacity < lic["limit"]),
            "usable": len(usable),
        }

    async def apply(self, camera_ids: list[str]) -> dict:
        lic = self.licence.current()
        if not lic["protects"]:
            raise ValueError("Choose a Guard package first — then Guard can protect these cameras.")
        wanted = list(dict.fromkeys(camera_ids))
        if not wanted:
            raise ValueError("Choose at least one camera.")
        if len(wanted) > lic["limit"]:
            raise ValueError(f"Your package protects up to {lic['limit']} cameras. Choose {lic['limit']}.")
        for c in self.devices.cameras():
            if c["guard_enabled"] and c["id"] not in wanted:
                await self.devices.set_guard(c["id"], False)
        for cid in wanted:
            await self.devices.set_guard(cid, True)
        await self.ai.sync()
        self.telemetry.report("recommended", licensed=True)
        return {"cameras": [c for c in self.devices.cameras() if c["guard_enabled"]]}

    # ── step 4: where products are, where customers leave ────────────
    def areas(self, camera_id: str, products: dict | None, exit_: dict | None) -> dict:
        with self.db.session() as s:
            if not s.get(Camera, camera_id):
                raise ValueError("That camera is no longer in the list.")
        new = []
        for rect, ztype, name in ((products, "SHELF", "Products"), (exit_, "EXIT", "Exit")):
            if rect is None:
                continue
            x1, x2 = sorted((float(rect["x1"]), float(rect["x2"])))
            y1, y2 = sorted((float(rect["y1"]), float(rect["y2"])))
            try:
                poly = validate_polygon([{"x": x1, "y": y1}, {"x": x2, "y": y1}, {"x": x2, "y": y2}, {"x": x1, "y": y2}])
            except PolygonError as e:
                raise ValueError(str(e)) from None
            new.append((name + SETUP_ZONE_SUFFIX, ztype, poly))
        with self.db.session() as s:
            # Setup owns only the zones it made; zones drawn in Advanced Setup stay.
            s.execute(delete(Zone).where(Zone.camera_id == camera_id, Zone.name.endswith(SETUP_ZONE_SUFFIX)))
            for name, ztype, poly in new:
                s.add(Zone(camera_id=camera_id, name=name, zone_type=ztype, polygon_json=json.dumps(poly),
                           sensitivity="MEDIUM", enabled=True))
        self.db.audit("zone_created", camera_id, source="auto_setup", count=len(new))
        self.ai.reload_camera(camera_id)
        self.telemetry.report("zones")
        with self.db.session() as s:
            return {"zones": [{"id": z.id, "name": z.name, "zone_type": z.zone_type}
                              for z in s.scalars(select(Zone).where(Zone.camera_id == camera_id))]}

    # ── demo mode → the cloud ────────────────────────────────────────
    def summary(self) -> dict:
        """What the /link page shows before sign-in. Counts and brands only."""
        found = self.found()
        cams = self.devices.cameras()
        usable = [c for c in cams if c["online"] and c["compatibility"] != "INCOMPATIBLE"]
        s = {
            "cameras_found": len(cams), "compatible_cameras": len(usable),
            "capacity": self.bench.capacity if self.bench else 0,
            "devices": [{"brand": d["brand"], "kind": d["kind"] if d["kind"] in ("DVR", "NVR", "CAMERA") else "UNKNOWN",
                         "cameras": d["channels"]} for d in found][:16],
            "internet": bool(self.checks["network"].get("internet")),
        }
        if self.bench:
            s["computer"] = {"cores": self.bench.cpu_count, "ram_gb": self.bench.ram_total_gb}
        return s

    async def link(self, name: str) -> dict:
        return await self.cloud.start_link(name, self.version, self.summary())

    # ── the end ──────────────────────────────────────────────────────
    def complete(self, test_passed: bool | None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.session() as s:
            row = s.get(Setting, DONE_KEY)
            if row:
                row.value = now
            else:
                s.add(Setting(key=DONE_KEY, value=now))
        self.db.audit("setup_completed", None, test_passed=test_passed)
        self.telemetry.report("protected", test_passed=test_passed, licensed=self.licence.protects())
        return self.state()

    def help(self, reason: str) -> None:
        self.telemetry.report("help", needs_help=True, help_reason=reason[:60])
