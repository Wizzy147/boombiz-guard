# Architecture — Phase 1

```
Setup UI (React, 127.0.0.1)
        │  X-Guard-Token + Host/Origin checks
        ▼
Local API (FastAPI)  ── api/routes.py, api/security.py
        │
        ├── DeviceService (services/devices.py)
        │     discovery → devices → one-attempt auth → channels → cameras
        │     compatibility test · 2-camera Guard limit · audit
        │
        ├── Scanner (discovery/)
        │     WS-Discovery probe · /24 TCP check on CCTV ports only · fingerprint
        │
        ├── Adapters (adapters/)  — registry order: ONVIF → Hikvision/Dahua → V380 → generic RTSP
        │
        ├── CredentialVault (security/vault.py) — Windows DPAPI, device_secrets table
        │
        └── StreamManager (services/streams.py)
              Guard workers: 1 FFmpeg per enabled camera, always on, backoff 2/5/10/30 s
              Preview workers: on demand, stopped 30 s after the last viewer
              RTSP → FFmpeg → MJPEG → browser (credentials never leave the agent)
```

## Key decisions

| Decision | Why |
|---|---|
| Own SOAP client instead of onvif-zeep | 6 operations needed; zeep's WSDL loading is slow, packages badly with PyInstaller, and breaks on OEM WSDLs. |
| WS-Security digest written in the **device's** clock | Shop DVRs rarely have the right time; digest auth fails on skew. GetSystemDateAndTime is read first. |
| RTSP Digest answered on the **same** TCP connection | MediaMTX and most camera firmware bind the nonce to the connection. The lab caught this. |
| FFmpeg is the transport, not OpenCV | Phase 1 §13. It also gives measured FPS, drops and decode errors for the stability test. |
| Stored stream URIs are credential-free | Credentials are joined to the URL only in `streams/urls.py`, just before FFmpeg starts. |
| Preview via single-camera tickets (2 min) | `<img>` can't send the token header, and the long-lived token must not go in a URL. |
| Hosts only ever swap the host, never the port | A device advertising a stale IP must not have its RTSP :554 rewritten to its web port. |

## Data (SQLite, `guard.db`)

`devices`, `cameras` (Phase 1 §4–5 columns), `device_secrets` (DPAPI blobs),
`audit_logs`, `settings`. Location: `%PROGRAMDATA%\Boombiz Guard\` in
production, `agent/data/` or `GUARD_DATA_DIR` in development.

## Health model (Phase 1 §19)

Per stream: `status` (ONLINE / DEGRADED / OFFLINE / AUTH_ERROR / STREAM_ERROR),
`last_frame_at`, `stream_fps`, `decode_fps`, `reconnect_count`, `last_error`.
Events: `CAMERA_ONLINE`, `CAMERA_OFFLINE`, `CAMERA_RECONNECTED`,
`CAMERA_AUTH_ERROR`. An auth error **stops** the worker rather than retrying a
rejected password against the recorder.

## Known Phase 1 limits

- The RTSP URL with credentials is on FFmpeg's command line. On a shared
  multi-user PC another local user could read it from the process list. Fix
  before production: pass the URL through a pipe/config file FFmpeg reads.
- The PC check is basic (RAM/CPU/disk/FFmpeg). The real benchmark (PRD §14) comes
  with Phase 2 when there is inference to measure.
- Dahua's RTSP convention (`?channel=&subtype=`) can't be simulated by MediaMTX
  (it drops query strings), so the Dahua adapter is unit-shaped only.
- Not yet packaged: PyInstaller build, WinSW Windows Service, tray icon, signed
  installer.
