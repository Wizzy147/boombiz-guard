# Boombiz Guard

Turns the CCTV a shop already owns into an incident-detection system. A product
of Digital Orca Limited, in the Boombiz family.

**Current state: Phase 2 — local AI.** Phase 1 (CCTV foundation) scans a
shop LAN, signs in to cameras and DVR/NVRs, previews video without exposing
the CCTV password, and keeps up to 2 Guard streams connected. Phase 2 adds
on-device AI on those streams: YOLOX person detection, tracking, zones,
restricted/after-hours/exit rules, a shelf-interaction heuristic, correlation
into `POSSIBLE_UNPAID_EXIT`, experimental smoke/fire, and CPU-aware load
shedding. No cloud. See `docs/phase2-ai.md` — including its limits.

**2.5.2 — plug-and-play.** Guard is free to download and runs in
Compatibility & Demo mode until a package is bought online; **Auto Setup**
finds the CCTV, benchmarks the PC, recommends cameras, guides the merchant
through products/exit areas and a Guard Test, and reports setup KPIs. The
Phase 1 wizard is now **Advanced setup** for technicians. See
`docs/auto-setup.md`.

Person model: YOLOX (Apache-2.0). Download `yolox_nano.onnx` (and optionally
`yolox_tiny.onnx`) from the official Megvii YOLOX 0.1.1rc0 release into
`agent/models/person/`; the agent refuses any file whose SHA-256 doesn't
match `app/ai/model_manager.py`. Never swap in an Ultralytics model without
an enterprise licence (AGPL-3.0).

Pose model (concealment only): `rtmpose-t_simcc-body7…zip` from
download.openmmlab.com → `agent/models/pose/rtmpose-t-body7.onnx`.
**Licence review required before commercial launch** — see
`docs/phase2-ai.md`. Without it, concealment reports "unavailable" and all
other protection runs normally.

```bat
agent\.venv\Scripts\python lab\dod.py   :: Phase 2 Definition of Done on real footage
```

```
boombiz-guard/
├── agent/        Python 3.13 Windows agent (FastAPI, SQLite, FFmpeg)
├── desktop-ui/   React + Vite setup UI, served by the agent on 127.0.0.1
├── installer/    Windows installer (PyInstaller + Inno Setup) — see below
├── lab/          Simulated CCTV lab + end-to-end test
└── docs/         architecture, supported devices, security
```

## Run it

Prerequisites: Python 3.13, Node 20+, FFmpeg/ffprobe on PATH.

```bat
cd agent
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest

cd ..\desktop-ui
npm install
npm run build

cd ..\agent
.venv\Scripts\python -m app.main
```

The agent listens on `127.0.0.1:7480` only. Open the setup UI with the token it
created on first run:

```
http://127.0.0.1:7480/#t=<contents of %PROGRAMDATA%\Boombiz Guard\setup-token>
```

## Build the installer

```bat
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

Produces `dist\installer\BoombizGuardSetup-<VERSION>.exe` (version from
`agent\app\main.py`). Needs Inno Setup 6, PyInstaller in `agent\.venv`, the
model files in `agent\models`, and FFmpeg (`GUARD_FFMPEG_DIR`, default: the one
on PATH). The installer puts the agent + tray app in Program Files, the models
in `%ProgramData%\Boombiz Guard\models`, starts the agent at boot as SYSTEM
(scheduled task "Boombiz Guard Agent", restarts on failure) and the tray app at
every sign-in. Uninstall keeps `%ProgramData%\Boombiz Guard`.

It is **unsigned** for BDO pilots: Windows shows "Windows protected your PC" →
More info → Run anyway. Set `GUARD_SIGN_CERT` / `GUARD_SIGN_PASSWORD` to sign.

## Prove it without hardware — the simulated lab

```bat
agent\.venv\Scripts\python lab\sim.py          :: terminal 1 — fake shop network
lab\run-agent.cmd                             :: terminal 2 — agent against it
set PYTHONIOENCODING=utf-8
agent\.venv\Scripts\python lab\e2e.py          :: terminal 3 — 29 checks
```

The lab puts a "Hikvision DVR", an ONVIF camera, a cloud-only V380 and a generic
RTSP camera on 127.0.0.2–.5, speaking the real protocols (ISAPI Digest, ONVIF
WS-Security, WS-Discovery, RTSP Digest via MediaMTX). Login: `admin` / `Lab#2026`.
`lab/bin/mediamtx.exe` is not committed — download MediaMTX v1.21 for Windows
from github.com/bluenviron/mediamtx into `lab/bin/`.

**Lab results are not hardware results.** See `docs/supported-devices.md`.
