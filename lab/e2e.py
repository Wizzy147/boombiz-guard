"""End-to-end Phase 1 check against the simulated lab.

    (terminal 1) agent\\.venv\\Scripts\\python lab\\sim.py
    (terminal 2) lab\\run-agent.cmd
    (terminal 3) agent\\.venv\\Scripts\\python lab\\e2e.py

Walks the Phase 1 Definition of Done through the real local API and prints a
PASS/FAIL line per requirement. Exit code 1 if anything failed.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

LAB = Path(__file__).resolve().parent
DATA = LAB / "run" / "agent-data"
AGENT = "http://127.0.0.1:7480"
CONTROL = "http://127.0.0.1:7499"
PASSWORD = "Lab#2026"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}", flush=True)
    return bool(ok)


async def main() -> int:
    token = (DATA / "setup-token").read_text().strip()
    H = {"X-Guard-Token": token}
    async with httpx.AsyncClient(base_url=AGENT, timeout=90) as c, httpx.AsyncClient(base_url=CONTROL, timeout=10) as lab:
        await lab.post("/reset")

        # ── API security ────────────────────────────────────────────
        r = await c.get("/api/v1/devices")
        check(r.status_code == 401, "API refuses a request with no token", str(r.status_code))
        r = await c.get("/api/v1/devices", headers={**H, "Host": "evil.example:7480"})
        check(r.status_code == 403, "API refuses a foreign Host header (DNS rebinding)", str(r.status_code))
        r = await c.post("/api/v1/discovery/start", headers={**H, "Origin": "http://evil.example"})
        check(r.status_code == 403, "API refuses a cross-site Origin (CSRF)", str(r.status_code))

        # ── discovery ───────────────────────────────────────────────
        t0 = time.time()
        await c.post("/api/v1/discovery/start", headers=H)
        while True:
            s = (await c.get("/api/v1/discovery/status", headers=H)).json()
            if s["status"]["stage"] == "done":
                break
            await asyncio.sleep(0.5)
        devs = {d["ip_address"]: d for d in s["devices"]}
        check(set(devs) >= {"127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"},
              "Scan finds all four lab devices", f"{sorted(devs)} in {time.time() - t0:.1f}s")
        check(devs.get("127.0.0.2", {}).get("manufacturer") == "Hikvision", "Hikvision recorder identified before login")
        check(devs.get("127.0.0.3", {}).get("adapter_type") == "onvif", "ONVIF camera found by WS-Discovery")
        check(all(d["authentication_required"] for d in devs.values()), "Every device waits for credentials")

        hik, onv, v380, gen = (devs[ip]["id"] for ip in ("127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"))

        # ── authentication: one attempt, never a guess ──────────────
        r = (await c.post(f"/api/v1/devices/{hik}/authenticate", headers=H, json={"username": "admin", "password": "wrong"})).json()
        stats = (await lab.get("/stats")).json()
        check(r["ok"] is False and "Authentication failed" in r["error"], "Wrong password → plain-language failure", r.get("error", "")[:60])
        check(stats["failed_logins"]["hikvision"] == 1, "Wrong password costs exactly ONE login attempt",
              f"recorder saw {stats['failed_logins']['hikvision']}")

        r = (await c.post(f"/api/v1/devices/{hik}/authenticate", headers=H, json={"username": "admin", "password": PASSWORD})).json()
        check(r["ok"], "Correct password connects to the Hikvision DVR")
        d = r["device"]
        check(d["device_type"] == "DVR" and d["model"] == "DS-7208HQHI-K1", "DVR model and type read over ISAPI", f"{d['device_type']} {d['model']}")
        chans = (await c.get(f"/api/v1/devices/{hik}/channels", headers=H)).json()["channels"]
        names = {ch["name"]: ch for ch in chans}
        check(len(chans) == 3, "All 3 DVR channels enumerated", ", ".join(names))
        check(names.get("Rear Door", {}).get("online") is False, "Offline channel reported as offline")
        check(all(names[n]["has_sub_stream"] and names[n]["has_main_stream"] for n in ("Entrance", "Product Shelves")),
              "Main + substream detected per channel")

        r = (await c.post(f"/api/v1/devices/{onv}/authenticate", headers=H, json={"username": "admin", "password": PASSWORD})).json()
        onv_ch = (await c.get(f"/api/v1/devices/{onv}/channels", headers=H)).json()["channels"]
        check(r["ok"] and len(onv_ch) == 1 and onv_ch[0]["has_sub_stream"], "ONVIF camera: profiles + stream URIs over WS-Security",
              f"{onv_ch[0]['width']}x{onv_ch[0]['height']} sub" if onv_ch else "")

        r = (await c.post(f"/api/v1/devices/{v380}/authenticate", headers=H, json={"username": "admin", "password": PASSWORD})).json()
        check(not r["ok"] and r["device"]["compatibility"] == "INCOMPATIBLE", "V380 cloud-only camera honestly marked NOT compatible",
              (r["device"].get("compatibility_reason") or "")[:70])

        r = (await c.post(f"/api/v1/devices/{gen}/authenticate", headers=H, json={"username": "admin", "password": PASSWORD})).json()
        gen_ch = (await c.get(f"/api/v1/devices/{gen}/channels", headers=H)).json()["channels"]
        check(r["ok"] and gen_ch and gen_ch[0]["has_sub_stream"], "Generic RTSP camera found by path probing (main + sub)")

        # ── manual add ──────────────────────────────────────────────
        r = (await c.post("/api/v1/devices/manual", headers=H, json={
            "name": "Manual cam", "ip_address": "", "username": "admin", "password": PASSWORD,
            "connection_type": "rtsp", "rtsp_url": "rtsp://127.0.0.5:554/stream1"})).json()
        check(r["ok"], "Manual add with an RTSP address works")

        # ── selection limit ─────────────────────────────────────────
        ent, shelves = names["Entrance"]["id"], names["Product Shelves"]["id"]
        a = await c.post(f"/api/v1/cameras/{ent}/enable", headers=H)
        b = await c.post(f"/api/v1/cameras/{shelves}/enable", headers=H)
        third = await c.post(f"/api/v1/cameras/{onv_ch[0]['id']}/enable", headers=H)
        check(a.status_code == 200 and b.status_code == 200, "Entrance + Product Shelves selected for Guard")
        check(third.status_code == 409, "A third Guard camera is refused (Basic limit = 2)", third.json().get("detail", "")[:70])

        # ── compatibility test (real 20 s stream test, both in parallel) ──
        tests = await asyncio.gather(*(c.post(f"/api/v1/cameras/{cid}/test-stream", headers=H, json={}) for cid in (ent, shelves)))
        for cid, tr in zip((ent, shelves), tests):
            body = tr.json()
            rep, t = body["report"], body["test"] or {}
            check(rep["status"] == "COMPATIBLE" or rep["status"] == "LIMITED",
                  f"Compatibility test: {body['camera']['name']}",
                  f"{rep['status']} · {t.get('codec')} {t.get('width')}x{t.get('height')} · {t.get('measured_fps')} fps / {t.get('seconds_run')} s")

        # ── live preview without exposing credentials ───────────────
        ticket = (await c.post(f"/api/v1/cameras/{ent}/preview-ticket", headers=H)).json()["ticket"]
        snap = await c.get(f"/api/v1/cameras/{ent}/snapshot.jpg", params={"ticket": ticket})
        check(snap.status_code == 200 and snap.content[:2] == b"\xff\xd8", "Preview frame served as JPEG via a ticket", f"{len(snap.content)} bytes")
        bad = await c.get(f"/api/v1/cameras/{ent}/snapshot.jpg", params={"ticket": "nope"})
        check(bad.status_code == 401, "Preview refuses a bad ticket")
        cam_json = (await c.get(f"/api/v1/cameras/{ent}", headers=H)).text
        check(PASSWORD not in cam_json and "rtsp://" not in cam_json, "Camera API returns no password and no RTSP address")

        # ── reconnect without restarting Guard ──────────────────────
        await asyncio.sleep(3)
        await lab.post("/drop/hik-ch1-sub", params={"seconds": 20})
        saw_offline = saw_back = False
        deadline = time.time() + 90
        while time.time() < deadline:
            h = (await c.get("/api/v1/system/health", headers=H)).json()
            st = h["streams"].get(ent, {})
            evs = [e["event"] for e in h["events"] if e["camera_id"] == ent]
            saw_offline = saw_offline or "CAMERA_OFFLINE" in evs
            if saw_offline and "CAMERA_RECONNECTED" in evs and st.get("status") == "ONLINE":
                saw_back = True
                break
            await asyncio.sleep(2)
        check(saw_offline, "Camera dropout detected (CAMERA_OFFLINE)")
        check(saw_back, "Stream reconnects by itself (CAMERA_RECONNECTED)", f"reconnects={st.get('reconnect_count')}")

        # ── no secrets at rest in plain text ────────────────────────
        await asyncio.sleep(1)
        logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (DATA / "logs").glob("*.log*"))
        check(PASSWORD not in logs and "wrong" not in logs.replace("wrong password", ""), "Passwords absent from agent logs")
        db_bytes = b"".join(p.read_bytes() for p in DATA.glob("guard.db*"))
        check(PASSWORD.encode() not in db_bytes and b"Lab%232026" not in db_bytes, "Password absent from the database file (DPAPI blob only)")

    failed = [n for ok, n, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
