@echo off
rem Run the Guard agent against the simulated lab (lab\sim.py must be running).
rem Keeps all agent data in lab\run\agent-data and never sweeps the real LAN.
setlocal
set GUARD_DEV=1
set GUARD_DATA_DIR=%~dp0run\agent-data
set GUARD_SCAN_LAN=0
set GUARD_SCAN_EXTRA_HOSTS=127.0.0.2,127.0.0.3,127.0.0.4,127.0.0.5
set GUARD_DISCOVERY_UNICAST=127.0.0.3:3702
set GUARD_DISCOVERY_TIMEOUT=2
rem No cloud in the lab, so no licence: pin the camera limit the e2e expects.
set GUARD_MAX_CAMERAS=2
cd /d %~dp0..\agent
.venv\Scripts\python -m app.main
