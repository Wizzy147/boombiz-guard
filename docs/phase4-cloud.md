# Phase 4 — Cloud, remote alerts, sync, multi-store

Built sprint by sprint (user decision 2026-09-13). Local Guard stays the primary protection: no cloud
call is ever on the path of detection, incident capture or the local alarm.

| Sprint | Scope | State |
|---|---|---|
| 4A | Device cloud & authentication: activation codes, device secret → access tokens, heartbeats, remote health, offline detection, revoke | **Built** (this doc) |
| 4B | Incident sync & media: priority sync queue, idempotent incident API, snapshot + ≤15 s clip upload to a private bucket, retention | **Built** |
| 4C | Notifications: recipients, rules, WhatsApp (Meta Cloud API) + email, dedupe, retries, signed incident links, acknowledge | — |
| 4D | Multi-store, RBAC, audit UI, health alerts, subscription state (admin-set; Paystack later), hardening | — |

Decisions: keep phone pairing AND add installer codes · incident media goes to a **new private bucket**
(4B) · subscription is state-only for now, activated by a super-admin (4D).

## 4A — how a Guard PC connects

```
Installer / owner (signed in)                 Guard PC                         Boombiz cloud
guard.getboombiz.com → Locations
  → Activate a Guard computer (branch)  ──►  POST /api/guard/activation-codes → GARD-XXXX-XXXX (30 min, one use, hash stored)
                                              Settings → Boombiz cloud → type code
                                              POST /api/guard/v1/devices/activate
                                                {code, installation_id, fingerprint_hash, …}
                                              ◄── device_id + device_secret (once; DPAPI-encrypted here)
                                              POST /api/guard/v1/device/auth  (Bearer secret + installation_id)
                                              ◄── access_token (30 min, memory only)
                                              every 2 min: POST /api/guard/v1/devices/{id}/heartbeat
```

- **Machine binding**: `installation_id` is generated once by the agent (settings table). The cloud refuses the
  secret from any other installation. Fingerprint = sha256(installation id + Windows MachineGuid).
- **Re-activating the same PC** rotates its secret on the same device row (tokenVersion bump kills old tokens).
- **Phone pairing still works** (older PCs, no installer on site). Those devices have no branch and show as
  "Not reporting" until their agent is updated; after the update they heartbeat like any other.
- **Old cloud, new agent**: if `device/auth` returns 404 the agent sends the secret directly, as before.

## Heartbeat (what leaves the shop)

Agent version, camera **names** + online/offline (guard-enabled cameras only), CPU/RAM/disk, average AI FPS,
whether AI is running, whether an alarm output is failing, outbox size, last incident time.
Never: video, snapshots, camera IPs, stream URLs, usernames, passwords (a test asserts this).

## Health states (`lib/guard/health.ts` in the Boombiz repo)

| State | Rule |
|---|---|
| UNKNOWN | never heartbeated |
| OFFLINE | no heartbeat for 6 min |
| DEGRADED | heartbeating, but a camera is down, no cameras, AI stopped, alarm output failing, disk < 5 GB, CPU ≥ 90 %, or > 500 items waiting |
| ONLINE | otherwise |

Dashboards compute this on every read. The `guard-health` cron (every 10 min) sends ONE "Guard offline"
push after 12 min of silence; the first heartbeat back sends "restored". A branch shows its worst PC.

## Revoke (stolen / replaced PC)

Owner → Locations → Revoke this computer. Its secret and every access token stop working immediately; the
agent shows "no longer connected — activate again". Activate the replacement with a new code.

## Tests

- Agent: `tests/test_cloud_4a.py` — activation, one-use code, secret only ever sent to device/auth, token
  reuse, heartbeat content (no addresses), re-auth after token rotation, revoked device, secret copied to
  another PC refused, fallback to an old cloud.
- Cloud: `node scripts/test-guard-cloud.cjs` — code format/normalising, token mint/verify/expiry/tamper,
  health rules.

## 4B — incident sync & media

```
incident (local)  ──scan every 5 s──►  sync_queue (SQLite, survives restarts and outages)
                                        INCIDENT_UPSERT   fire 10 · after-hours 20 · unpaid exit 30 ·
                                                          restricted 35 · health 40 · other 70
                                        SNAPSHOT_UPLOAD   50 (fire 15)   after the cloud id exists
                                        CLIP_UPLOAD       60 (fire 25)   HIGH / CRITICAL only
                  ──process every 5 s──►
POST /api/guard/v1/incidents                        idempotent on (device, local incident id)
POST /api/guard/v1/incidents/{id}/uploads           → presigned PUT, 10 min, SHA-256 signed in
PUT  <private bucket>                               decrypted in memory, straight to storage
POST /api/guard/v1/incidents/{id}/uploads/complete  cloud HEADs the object: size (+checksum) must match
```

- **What syncs**: every LOW/HIGH/CRITICAL incident created after the PC was connected (never history; INFO
  never). Snapshots for all of them; clips for HIGH and CRITICAL only (bandwidth).
- **Changes re-sync**: severity rising, or a review on the shop PC, re-sends the incident to the same cloud row.
  In 4B the shop PC owns status/review and the cloud mirrors it; 4C adds remote review with `version`.
- **Retries**: offline → wait with backoff (5 s … 5 min); cloud not ready (404) → retry every 5 min; bad request
  → failed. Media gives up after 8 attempts; a file deleted locally fails its upload, the metadata still arrives.
- **Storage**: `guard/<business>/<location>/<yyyy>/<mm>/<dd>/<incident>/snapshot.jpg|clip.mp4` in the private
  Guard bucket (`S3_GUARD_MEDIA_BUCKET`, default `boombiz-prod-guard-media`). Viewers get 10-minute signed links
  after a tenant + branch check. Nothing public.
- **Retention**: cloud media removed 30 days after the incident by the guard-health cron, unless the owner/
  manager tapped **Keep evidence** in the cloud or it was kept on the shop PC. Metadata stays.
- **Cloud console**: Incidents tab (today's counts, filters, per-branch) and an incident page with snapshot,
  clip, timeline and review details.
- **Not yet**: remote acknowledge/confirm and alert links (4C).

## §98 Internet use (bandwidth modes) and §99 backlog cap

Guard PC → Settings → Boombiz cloud → **Internet use** (owner/installer):

| Mode | What uploads |
|---|---|
| Normal | details, snapshots, clips as recorded |
| Low bandwidth | details first; snapshot shrunk to ≤960 px / q70, clip re-encoded to ≤360p, 10 fps, CRF 32 — **cloud copy only**, local evidence stays full quality; falls back to the original if re-encoding fails |
| Details only (24 h) | incident details only; snapshots/clips wait (not failed) and resume when switched back — or automatically after 24 h |

**Backlog cap** (default 3 GB, 1–20 GB): measured over snapshot/clip files still waiting to upload. Over the cap,
uploads are **skipped** (status SKIPPED; the files stay on the PC) in this order: clips before snapshots, oldest
first. Never skipped: CRITICAL, fire/smoke, or kept evidence — if those alone exceed the cap they all still upload
and the PC says so. Incident details are never skipped. The PC shows a plain warning; the cloud Locations page
shows Low / Details-only mode from the heartbeat.

## Production status

- ✅ Bucket **boombiz-prod-guard-media** created 2026-09-13 (eu-west-1, account 065634457453): all public access
  blocked, SSE AES256 + bucket key, BucketOwnerEnforced, abort-incomplete-multipart after 1 day, tagged
  product=boombiz-guard. Verified with the app's own lib/s3 as `boombiz-app`: checksum-signed PUT ok, tampered body
  refused (400), HEAD size+SHA-256 match, signed GET returns the bytes, anonymous GET 403, delete ok.
- ✅ Schema pushed to production Neon 2026-09-13 (4A + 4B: GuardDevice columns, GuardActivationCode,
  GuardDeviceHealth, GuardAuditLog, GuardIncident, GuardIncidentMedia). Diff was Guard-only; drift check now empty.
- ⬜ Set `BOOMBIZ_GUARD_TOKEN_SECRET` in Amplify (merge — UpdateApp replaces the whole env map).
- ⬜ Deploy the Boombiz app.
- ⬜ `npx tsx scripts/provision-cron.ts` (boombiz-guard-health).
- ⬜ Install agent 0.4.1 on the Guard PC.

## To go live (4A) — see "Production status" above for what's done

1. ~~`npx prisma db push`~~ done 2026-09-13.
2. Set `BOOMBIZ_GUARD_TOKEN_SECRET` (long random) in Amplify. Without it tokens fall back to a key derived
   from NEXTAUTH_SECRET, which works but ties Guard tokens to that secret's rotation.
3. Deploy the Boombiz app.
4. `npx tsx scripts/provision-cron.ts` to create the `boombiz-guard-health` schedule.
5. Install agent 0.4.0 on the Guard PC.
