# Boombiz Guard

Turns the CCTV a shop already owns into an incident-detection system. A product
of Digital Orca Limited, in the Boombiz family.

**Current state: Phase 1 — CCTV foundation.** The agent can scan a shop LAN,
find cameras and DVR/NVRs, sign in, list channels, preview live video without
exposing the CCTV password, test compatibility, choose up to 2 Guard cameras,
and keep those streams connected through dropouts and restarts. No AI yet
(Phase 2).

```
boombiz-guard/
├── agent/        Python 3.13 Windows agent (FastAPI, SQLite, FFmpeg)
├── desktop-ui/   React + Vite setup UI, served by the agent on 127.0.0.1
├── installer/    (Phase 1 packaging — PyInstaller + WinSW, not built yet)
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
