# Guard on a spare Android phone or Android TV box

For shops with no computer (owner decision 2026-09-14): a spare Android phone or an Android TV box stays in the
shop, plugged in, on the shop Wi-Fi next to the recorder, and does the watching. The owner's own phone only
receives alerts — it leaves the shop at closing time, exactly when after-hours theft happens.

Code: `android/` (Kotlin, Gradle). Cloud side: **unchanged** — the phone uses the same `/api/guard/v1/*` routes,
activation codes, heartbeats, incident sync, media bucket, alert rules and WhatsApp templates as a Guard PC.

## What v1 does (core watcher)

| | Phone / TV box | Guard PC |
|---|---|---|
| Cameras | 2 (sub-stream, ~5 AI checks/s each) | more, by PC benchmark |
| Person after hours (CRITICAL) | yes — anyone in view ≥ 2 s, shop closed | yes |
| Restricted area (HIGH) | yes — box zones, feet inside | yes — polygons |
| Ignore zones | yes | yes (+ privacy masking) |
| Camera offline 30 s / covered / turned (HIGH) | yes | yes |
| Local alarm | device alarm sound (full volume, alarm stream) | PC beep + CCTV siren + relays |
| Snapshot to cloud | yes | yes |
| Clip to cloud | **no** (no rolling video buffer yet) | HIGH/CRITICAL |
| Shelf swap, unpaid exit, concealment, fire | **no** | yes |
| Remote acknowledge | yes (heartbeat command) | yes |
| Watcher unplugged | phone: CRITICAL + 30 s siren after hours, LOW notice when open (TV box: no battery, can't tell) | — |
| Settings PIN | yes | local sign-in |
| Phone pairing (ABCD-EFGH) | no — activation codes only | both |

Incident types, severities, titles, dedup windows and sync priorities are copied from `agent/app/incidents/classifier.py`
and `agent/app/cloud/sync.py`, so the cloud treats a phone exactly like a PC. Camera-damage alarms sound only after
closing (same rule as the PC).

## How it works

```
RTSP sub-stream ─► Media3 ExoPlayer (RTP over TCP, hardware MediaCodec) ─► ImageReader YUV ─► ARGB @ 5 fps
   ─► YOLOX-nano (ONNX Runtime, same model file + SHA-256 check as the PC) ─► ignore zones ─► ByteTrack-lite
   ─► restricted entry / after hours / tamper ─► dedup ─► incident + snapshot (SQLite, app-private storage)
   ─► alarm + notification ─► sync queue ─► cloud
```

- **Foreground service** (`specialUse`), partial wake lock + high-performance Wi-Fi lock, `START_STICKY`.
- **Survives restarts**: boot receiver, and a 15-minute watchdog alarm that restarts the service if a vendor
  "phone manager" (Tecno/Infinix/itel) killed it. The home screen walks the installer through battery exemption
  and auto-start.
- **Secrets**: device secret + recorder passwords sealed with AES-GCM, key in the Android Keystore.
- **Heartbeat**: never carries addresses, URLs, usernames or passwords (unit-tested). CPU is sent as null
  (Android hides whole-device CPU from apps); RAM and free storage are real.
- **Retention**: incidents and snapshots on the device are kept 30 days.

## If the phone is stolen

- **Unplugged alert** (`GUARD_DEVICE_UNPLUGGED`): charger pulled while the shop is closed → CRITICAL incident, 30 s
  siren, and an immediate sync (not the 5 s loop) with the newest camera frame as the snapshot — so the alert is
  usually out before the thief can switch the phone off. Shop open → LOW notice, no siren. Re-plugging ends the
  episode; a loose cable won't repeat within 10 min. Cloud: grouped with `after_hours` in `lib/guard/alertRules.ts`
  (Boombiz repo) so security recipients get it — **needs a Boombiz deploy**; until then it falls in "manual".
- **Settings PIN**: status screen open to all; cameras, areas, hours, incidents, alarm test, disconnect need the PIN.
  PBKDF2-hashed, 5 wrong tries → 5-minute lockout, re-locks on Home / after 5 min. Forgot PIN → a fresh GARD code
  from the console resets it (re-activation keeps the same device row).
- **Placement**: out of sight but inside a camera's view (e.g. locked in the recorder cabinet).
- **Afterwards**: owner revokes the phone in Guard → Locations; the DVR still has the video.
- Not covered: someone who knows the PIN, or who uninstalls/force-stops the app from Android settings — the
  cloud then reports Guard offline after ~12 min.

## Android TV

One APK. It shows on the TV launcher (leanback banner), needs no touchscreen, and every screen works with a remote.
Limits: zones need touch (draw them from a phone first); the alarm only plays through the TV speaker when the TV is on;
Samsung (Tizen) / LG (webOS) TVs can't run it — use an Android TV box. Minimum 4 GB RAM recommended.

## Build

```
cd android
cp ../agent/models/person/yolox_nano.onnx app/src/main/assets/   # not in git; the app checks its SHA-256
./gradlew testDebugUnitTest     # JVM tests for the detection logic, dedup, heartbeat privacy, URLs
./gradlew assembleRelease       # app/build/outputs/apk/release/app-release.apk (debug-signed for the pilot)
```

Needs JDK 17 + Android SDK 34 (`local.properties` → `sdk.dir`).

## Before real shops

- Test on a real Tecno/Infinix phone and a cheap Android TV box against a Hikvision and a Dahua DVR:
  frames arrive, AI FPS holds, the service survives 24 h with the screen off, the watchdog restarts it.
- Sign with a real release key (currently the debug key).
- The console still says "Guard PC" / "Activate a Guard computer" — the phone activates with the same code.
- Don't mention phones/TV boxes on the Guard site until this has run in a real shop.
