# Supported devices

Guard decides compatibility by testing, never by brand (PRD §2.1). This page
records what has actually been **verified**, and against what.

## Verification status

| Integration | Lab (simulated) | Real hardware |
|---|---|---|
| ONVIF discovery (WS-Discovery) | ✅ | ⬜ not yet tested |
| ONVIF sign-in (WS-Security digest), profiles, stream URIs | ✅ | ⬜ |
| Hikvision ISAPI: device info, DVR/NVR channel list, offline channels | ✅ | ⬜ |
| Hikvision RTSP `/Streaming/Channels/<ch>01|02` main + sub | ✅ | ⬜ |
| Dahua CGI + RTSP `/cam/realmonitor` | ⚠️ written, not simulatable | ⬜ |
| Generic RTSP path probing (main + sub) | ✅ | ⬜ |
| Manual RTSP address | ✅ | ⬜ |
| V380 decision path (cloud-only → NOT compatible) | ✅ | ⬜ |
| V380 with local RTSP enabled | ⬜ | ⬜ |
| 4G / solar cameras via their phone app (V380 Pro, CamHi, UBox) — Android watcher only, see android-watcher.md | ✅ unit-tested | ⬜ |
| XMEye recorders | ⬜ no adapter yet | ⬜ |
| Android Wi-Fi camera presets: EZVIZ, Imou, Tapo, Reolink, CamHi, Yoosee, iCSee (paths only) | ✅ unit-tested | ⬜ |
| Android shelf → exit theft detection | ✅ synthetic frames | ⬜ |
| Camera speaker siren (ONVIF audio back-channel, G.711) | ✅ SDP/digest/G.711 unit-tested | ⬜ |
| Automatic reconnect after a feed drops | ✅ | ⬜ |

**Nothing on this page may be marketed as supported until the "Real hardware"
column is filled in** (PRD §67: Tier 2 must be validated against real firmware).

## Hardware test matrix (fill in from the lab kit, Phase 1 §28–29)

| Device | Firmware | Discovery | Login | Channels | Main | Sub | Reconnect | Result |
|---|---|---|---|---|---|---|---|---|
| Hikvision IPC | | | | | | | | |
| Hikvision DVR | | | | | | | | |
| Hikvision NVR | | | | | | | | |
| Dahua IPC | | | | | | | | |
| Dahua XVR/NVR | | | | | | | | |
| V380 Pro (A) | | | | | | | | |
| V380 Pro (B) | | | | | | | | |
| XMEye DVR | | | | | | | | |

A row that ends "Unsupported" is a correct outcome (Phase 1 §29).

## Known device quirks handled

- Hikvision keeps a **separate ONVIF user list**: an admin login rejected over
  ONVIF is retried once against ISAPI — never with a different password.
- Cheap cameras lock after 3–5 bad logins. Guard sends the installer's password
  at most once per protocol and stops on the first rejection.
- Recorders report stale IPs in ONVIF replies; Guard keeps the address it
  actually reached and only swaps the host, never the port.
- Recorder clocks are usually wrong; ONVIF digests are timed to the device clock.
