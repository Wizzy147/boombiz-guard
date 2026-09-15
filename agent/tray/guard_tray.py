"""Boombiz Guard tray app — native Windows pop-ups (toasts).

    pythonw tray\\guard_tray.py        (starts with the user's Windows login)

Why a separate program: the agent runs as a Windows Service, and services
can't show anything on the desktop. This small app runs in the cashier's
own session, asks the agent (127.0.0.1 only) every 3 s for new pop-up-worthy
items, and shows them as Windows notifications.

  · HIGH / CRITICAL incidents, camera down > 5 min, all cameras down,
    alarm output failure — never LOW.
  · Fire/smoke: alarm-style toast that stays up with a looping sound until
    dismissed. Everything else: a normal toast (no sound burst on the POS).
  · Toasts never take focus — the checkout keeps the keyboard.
  · Click → opens that incident in Guard (browser, token in the URL fragment).
  · Menu: Open Guard · Guard Mode · Mute 1 hour · alert types · Quit.

Works with no internet: it only talks to the agent on this PC.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pystray
from PIL import Image, ImageDraw
from windows_toasts import (
    AudioSource,
    InteractableWindowsToaster,
    Toast,
    ToastAudio,
    ToastDuration,
    ToastScenario,
)

APP_ID = "Boombiz Guard"
PORT = int(os.environ.get("GUARD_PORT", "7480"))
BASE = f"http://127.0.0.1:{PORT}"
POLL_S = 3.0

GROUP_LABELS = {
    "unpaid_exit": "Possible theft (unpaid exit, product swap)",
    "restricted": "Restricted area",
    "after_hours": "After-hours intrusion",
    "fire": "Smoke / fire",
    "health": "Camera & alarm problems",
    "manual": "Staff reports",
}


def data_dir() -> Path:
    if os.environ.get("GUARD_DATA_DIR"):
        return Path(os.environ["GUARD_DATA_DIR"])
    return Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Boombiz Guard"


def prefs_path() -> Path:
    p = Path(os.environ.get("APPDATA", str(Path.home()))) / "Boombiz Guard" / "tray.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str) -> None:
    """%APPDATA%\\Boombiz Guard\\tray.log — what was shown and why not, for support."""
    try:
        p = prefs_path().with_name("tray.log")
        if p.exists() and p.stat().st_size > 512_000:
            p.replace(p.with_suffix(".log.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def load_prefs() -> dict:
    try:
        return json.loads(prefs_path().read_text(encoding="utf-8"))
    except Exception:
        return {"muted_groups": [], "muted_until": 0, "cursor": None}


def save_prefs(p: dict) -> None:
    try:
        prefs_path().write_text(json.dumps(p), encoding="utf-8")
    except Exception:
        pass


def icon_image(ok: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (11, 11, 11, 255))
    d = ImageDraw.Draw(img)
    fill = (255, 194, 26, 255) if ok else (140, 140, 140, 255)
    d.polygon([(32, 8), (54, 16), (54, 34), (32, 58), (10, 34), (10, 16)], fill=fill)
    d.ellipse((24, 24, 40, 40), fill=(11, 11, 11, 255))
    return img


class Tray:
    def __init__(self) -> None:
        self.prefs = load_prefs()
        self.token: str | None = None
        self.toaster = InteractableWindowsToaster(APP_ID)
        self.ok = False
        self.stop = threading.Event()
        self.shown: set[str] = set()
        self.icon = pystray.Icon("boombiz-guard", icon_image(False), "Boombiz Guard", self._menu())

    # ── agent access ─────────────────────────────────────────────────
    def _token(self) -> str | None:
        try:
            self.token = (data_dir() / "setup-token").read_text(encoding="utf-8").strip()
        except OSError:
            self.token = None
        return self.token

    def _get(self, path: str, **params) -> dict | None:  # noqa: ANN003
        tok = self.token or self._token()
        if not tok:
            return None
        try:
            r = httpx.get(BASE + path, params=params, timeout=4,
                          headers={"X-Guard-Token": tok, "Host": f"127.0.0.1:{PORT}"})
        except httpx.HTTPError:
            return None
        if r.status_code == 401:
            self.token = None  # token rotated (reinstall): re-read next time
            return None
        return r.json() if r.status_code == 200 else None

    def open_ui(self, incident_id: str | None = None, view: str | None = None) -> None:
        tok = self.token or self._token() or ""
        frag = f"t={tok}" + (f"&incident={incident_id}" if incident_id else "") + (f"&view={view}" if view else "")
        webbrowser.open(f"{BASE}/#{frag}")

    # ── menu ─────────────────────────────────────────────────────────
    def _muted(self) -> bool:
        return time.time() < self.prefs.get("muted_until", 0)

    def _toggle_group(self, group: str):  # noqa: ANN202
        def fn(icon, item):  # noqa: ANN001, ANN202
            g = set(self.prefs.get("muted_groups", []))
            g.symmetric_difference_update({group})
            self.prefs["muted_groups"] = sorted(g)
            save_prefs(self.prefs)
        return fn

    def _menu(self) -> pystray.Menu:
        groups = [pystray.MenuItem(label, self._toggle_group(g),
                                   checked=lambda item, g=g: g not in self.prefs.get("muted_groups", []))
                  for g, label in GROUP_LABELS.items()]
        return pystray.Menu(
            pystray.MenuItem("Open Guard", lambda i, it: self.open_ui(view="incidents"), default=True),
            pystray.MenuItem("Guard Mode", lambda i, it: self.open_ui(view="guardmode")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda it: "Unmute pop-ups" if self._muted() else "Mute pop-ups for 1 hour",
                             self._toggle_mute),
            pystray.MenuItem("Show pop-ups for", pystray.Menu(*groups)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _toggle_mute(self, icon, item) -> None:  # noqa: ANN001
        self.prefs["muted_until"] = 0 if self._muted() else time.time() + 3600
        save_prefs(self.prefs)

    def _quit(self, icon, item) -> None:  # noqa: ANN001
        self.stop.set()
        icon.stop()

    # ── toasts ───────────────────────────────────────────────────────
    def toast(self, item: dict) -> None:
        t = Toast()
        sev = item["severity"].capitalize()
        when = datetime.fromisoformat(item["occurred_at"]).astimezone().strftime("%I:%M %p").lstrip("0")
        t.text_fields = [f"Boombiz Guard · {sev}", item["title"],
                         " · ".join(x for x in (item.get("camera"), when) if x)]
        if item.get("fire"):
            # Stays up with a looping alarm until someone deals with it.
            t.scenario = ToastScenario.Alarm
            t.audio = ToastAudio(AudioSource.Alarm, looping=True)
        else:
            t.duration = ToastDuration.Long if item["severity"] == "CRITICAL" else ToastDuration.Short
        iid = item.get("incident_id")
        t.on_activated = lambda _args: self.open_ui(incident_id=iid, view="incidents")
        self.toaster.show_toast(t)

    def should_show(self, item: dict) -> bool:
        if item["key"] in self.shown:
            return False
        if item.get("fire"):
            return True  # fire is never muted
        if self._muted() or item["group"] in self.prefs.get("muted_groups", []):
            return False
        return True

    # ── loop ─────────────────────────────────────────────────────────
    def poll_loop(self) -> None:
        # First run: start from now — don't flood the cashier's computer with history.
        cursor = self.prefs.get("cursor") or datetime.now(timezone.utc).isoformat()
        last_status = 0.0
        while not self.stop.is_set():
            d = self._get("/api/v1/notifications", after=cursor)
            if d is not None:
                for item in d["items"]:
                    if self.should_show(item):
                        try:
                            self.toast(item)
                            log(f"shown {item['severity']} {item['title']} ref={item.get('ref')} fire={item.get('fire')}")
                        except Exception as e:
                            log(f"toast failed {item.get('ref')}: {e!r}")
                    else:
                        log(f"muted {item['severity']} {item['title']} ref={item.get('ref')}")
                    self.shown.add(item["key"])
                cursor = d.get("cursor") or cursor
                self.prefs["cursor"] = cursor
                save_prefs(self.prefs)
            if time.time() - last_status > 15:
                ov = self._get("/api/v1/incidents/overview")
                ok = bool(ov and ov["protection"]["running"] and ov["protection"]["cameras_online"] > 0)
                if ok != self.ok:
                    self.ok = ok
                    self.icon.icon = icon_image(ok)
                    self.icon.title = "Boombiz Guard · protection active" if ok else "Boombiz Guard · not protecting"
                last_status = time.time()
            self.stop.wait(POLL_S)

    def run(self) -> None:
        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.icon.run()


def single_instance() -> bool:
    """One tray per Windows session."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\BoombizGuardTray")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def open_setup() -> None:
    """`--open` (Start menu, end of install): open Guard in the browser once
    the agent answers. Right after install it may still be starting."""
    for _ in range(30):
        try:
            if httpx.get(f"{BASE}/api/ping", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    try:
        tok = (data_dir() / "setup-token").read_text(encoding="utf-8").strip()
    except OSError:
        tok = ""
    # view=start: Auto Setup on a computer that hasn't finished setup yet,
    # otherwise straight to Incidents (the setup screen decides).
    webbrowser.open(f"{BASE}/#t={tok}&view=start")


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit(0)
    first = single_instance()
    if "--open" in sys.argv:
        open_setup()
    if not first:
        sys.exit(0)
    Tray().run()
