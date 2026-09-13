# Phase 3 — Incidents, evidence capture & local alerts

```
AI event ─► classifier ─► correlation key camera : person : family ─► merge (inside window) or create
                                                                          │
   stream worker frames ─► RAM rolling buffer (10 s) ─► Capture (5 s before + 10 s after, ≤15 s)
                                                                          │
                                   media worker (below-normal priority) ◄─┘
                                     SNAPSHOT (+1.5 s)   CLIP (+10 s)
                                     encode in memory ─► AES-GCM ─► disk (.enc)
                                                                          │
                                         alarm rules ─► PC speaker / DVR relay / network relay / USB relay
                                                                          │
                                  review (PIN sign-in, roles) ─► retention / Keep Evidence / disk limits
```

Offline by construction: nothing in this phase uses the internet.

## Product rules and where they are enforced

| Rule | Enforcement |
|---|---|
| Guard is not a recorder | `buffer/rolling_buffer.py` — 10 s RAM ring, never written to disk |
| Clip ≤ 15 s | `RollingBuffer.start_capture` caps pre+post; `Capture.clipped` caps the frames; FFmpeg `-t 15`; an assert in the media worker |
| Pre-event context is the camera's, not the AI's | every AI event carries `frame_ts` (capture time of its frame); the capture anchors on it |
| No plaintext media on disk | clips encoded to a fragmented MP4 **in memory**, then AES-256-GCM (`media/encryption.py`, key wrapped by DPAPI); files bound to their incident by AAD |
| Media only through the API | one-time 60 s tickets per incident/kind; decrypted in memory; no paths returned |
| Incident never lost because media failed | 3 retries; lost buffer → "video unavailable", metadata kept; camera dropped mid-clip → shorter clip from what was captured |
| AI never decides theft | incidents start UNREVIEWED; only a signed-in person moves them on |
| POS first | clip encoding at BELOW_NORMAL priority, one thread, off the AI thread |

## Incidents

Events that open an incident (`incidents/classifier.py`): POSSIBLE_UNPAID_EXIT (HIGH, 60 s window, per
person), RESTRICTED_AREA_INCIDENT (HIGH, 30 s), AFTER_HOURS_INTRUSION (CRITICAL, 60 s, per camera),
POSSIBLE_FIRE (CRITICAL) / POSSIBLE_SMOKE (HIGH) (30 s, per camera), CAMERA_OFFLINE (LOW → HIGH after
5 min, one until it reconnects), GUARD_PROTECTION_DEGRADED (CRITICAL, all cameras down),
MANUAL_SECURITY_INCIDENT (staff report, can save the last 15 s).

POSSIBLE_CONCEALMENT does **not** open an incident on its own (earlier decision: it's always LOW); it joins
that person's unpaid-exit timeline and lifts its confidence one step. Everything else (PERSON_DETECTED,
SHELF_INTERACTION, EXIT_APPROACH …) becomes the incident's timeline.

IDs: `BG-<location>-<YYYYMMDD>-<seq>` (location code set by the installer, default LOC) plus an internal UUID.

## Review — PIN sign-in, roles

| Action | Owner | Manager | Security |
|---|---|---|---|
| View, play clip, acknowledge, escalate | ✓ | ✓ | ✓ |
| Confirm / false alert | ✓ | ✓ | not on CRITICAL |
| Keep evidence, export | ✓ | ✓ | ✕ |
| Delete, retention, people | ✓ | ✕ | ✕ |

PINs: 4–6 digits, PBKDF2-SHA256 200k, 5 wrong → 5-minute lock. False alerts require a reason
(customer paid, staff activity, item returned, camera angle, zone configured wrongly, AI mistake, other).
Every step is audited.

## Alarms

Adapters: PC speaker (works on every PC), Hikvision/Dahua alarm output, network relay (LAN addresses only),
USB serial relay (untested on hardware). Defaults: after-hours, smoke and fire ON; fire repeats every
cooldown until someone acknowledges; unpaid exit / restricted configurable (off); concealment silent.
A failed output is marked ERROR and audited as ALARM_OUTPUT_FAILURE — the incident is still recorded.

## Retention & disk

30 days default (LOW 14, INFO 7), Keep Evidence never expires, optional keep-if-confirmed. Expiry is a
soft delete: media removed, metadata kept. Storage ceiling min(10 GB, 5 % of the drive). Free space:
<10 GB warning, <5 GB accelerated cleanup, <2 GB only CRITICAL incidents keep media ("CRITICAL STORAGE
WARNING"). Cleanup every 6 h; SQLite WAL + busy_timeout + daily quick_check.

## Verified

- **73 unit tests**, including over-HTTP route tests.
- **`lab/dod3.py` 16/16** on real footage: one incident for the whole shelf→exit chain, snapshot, playable
  15.0 s clip, all encrypted, relay alarm on/off, security acknowledges, manager confirms, audit, Keep
  Evidence survives 31-day retention, unkept incident expires.
- **Live lab** through the running agent: manual incident with a 14.9 s clip from the live buffer,
  ticketed playback, bad ticket refused, Keep Evidence over HTTP, 24 files on disk all `.enc`.

## Bugs the testing caught (all fixed)

1. PIN lockout never engaged — failed-attempt counter rolled back with the error.
2. Catch-all review route swallowed `/keep` and `/media-ticket` over HTTP.
3. Clip pre-event anchored to AI processing time, not frame capture time.
4. A camera dropping right after an incident meant no clip at all.
5. Live lab: large (7–9 px) camera shifts with low correlation confidence were ignored, so a moving
   camera produced false HIGH unpaid-exit incidents. Large shifts now count regardless of confidence;
   a whole-scene-change check was added for knocked/turned cameras.

## Not done

- Windows toast notifications (the agent runs as a service; toasts need the future tray app). Guard Mode
  in the setup UI polls and can show a full-screen alert instead.
- Alarm hardware beyond the PC speaker and a simulated network relay is untested on real devices.
- No 8–12 h soak test yet.
