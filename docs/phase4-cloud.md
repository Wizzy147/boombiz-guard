# Phase 4 — Cloud, remote alerts, sync, multi-store

Built sprint by sprint (user decision 2026-09-13). Local Guard stays the primary protection: no cloud
call is ever on the path of detection, incident capture or the local alarm.

| Sprint | Scope | State |
|---|---|---|
| 4A | Device cloud & authentication: activation codes, device secret → access tokens, heartbeats, remote health, offline detection, revoke | **Built** (this doc) |
| 4B | Incident sync & media: priority sync queue, idempotent incident API, snapshot + ≤15 s clip upload to a private bucket, retention | **Built** |
| 4C | Notifications: recipients, rules, WhatsApp (Meta Cloud API) + email, dedupe, retries, signed incident links, acknowledge | **Built** (see "4C" below) |
| 4D | Multi-store, RBAC, audit UI, health alerts, subscription state (admin-set; Paystack later), hardening | **Built** (see "4D" below) |

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

## 4C — WhatsApp + email alerts, secure links, remote acknowledge

Decisions (2026-09-14): Claude drafts the WhatsApp templates, the owner submits them in WhatsApp Manager
(`docs/guard-whatsapp-templates.md`); **3 WhatsApp recipients per business** (email unlimited); guards without an
account get a limited 30-minute link page.

**Recipients** (Guard console → Recipients; owner/manager, a branch-pinned manager only their branch): name, role,
WhatsApp number (Nigerian local numbers normalised), email, branch or all, alert types, minimum severity, quiet
hours. Role presets: security = theft/intrusion types, no camera-health. "Send a test" (max 10/hour/business).

**Rules** (`lib/guard/alertRules.ts`, unit-tested): fire → everyone, any hour; critical ignores quiet hours; LOW is
email-only (WhatsApp costs money); late arrivals after an outage — critical always, HIGH ≤ 60 min, LOW ≤ 30 min,
else dashboard only; fair use 30 WhatsApp/business/hour except fire.

**Sending** (`lib/guard/notify.ts`): an incident that syncs (or rises in severity, e.g. camera offline LOW → HIGH)
alerts at once; Guard offline (cron) and restored (heartbeat) go to "Camera & Guard problems" recipients.
One `GuardNotification` per subject + recipient + channel (unique key → no duplicates); content rendered once, so
a retry is identical. Retries 30 s / 2 min / 5 min (4 attempts) — the 10-min guard-health cron runs them, and only
queries Postgres when the DynamoDB `NOTIFY_PENDING` flag says something is waiting. A fire/critical WhatsApp that
fails for good is emailed instead (§89). Messages carry a "View incident" link rather than an attached image: the
snapshot usually lands after the alert should already be out.

**Secure link** `guard.getboombiz.com/i/<token>`: 192-bit token, hash only, 30 min, revocable, one incident:
snapshot, clip, what/where/when, Acknowledge. No sign-in, nothing else reachable; rate-limited; opening is audited.
Expired → "This secure incident link has expired. Log in to Boombiz Guard to view the incident."

**Remote acknowledge** (console or link): cloud row moves (optimistic concurrency on `version`), a predefined
`INCIDENT_ACKNOWLEDGE` command is queued in DynamoDB, delivered on the PC's next heartbeat (≤ 2 min), applied
locally (UNREVIEWED → ACKNOWLEDGED "(remote)", stops a repeating fire siren), confirmed on the following beat.
A local review always wins; a stale re-send never undoes a remote acknowledgement. Unknown command types are
acknowledged and ignored — never executed (§58). The incident page lists who was alerted, by what, and the result.

**Main channels vs backup (owner decision 2026-09-14):** the main alerts are the free ones — phone buzz (Guard
web push) and the shop's alarms (CCTV siren/buzzer on a camera or recorder alarm output, the PC beep, any external
relay alarm; theft, swap and restricted-area alarms are ON by default and fire every enabled output). WhatsApp and
email are backup: **WhatsApp capped at 150 messages per business per calendar month** (Lagos; fire never blocked,
still counts; over the cap the row is SKIPPED with "used up — still went to phones, the shop alarm and email"), and
**grouped**: the first WhatsApp to a person about a branch goes at once; further non-fire, non-critical alerts to
them for that branch within 10 minutes are held (GROUPED) and the guard-health cron sends one summary via the ALERT
template ("3 more alerts (…) at Owerri Branch", camera list, time range, top severity, link to the latest). A summary
counts as one message. New recipients get WhatsApp on by default only for the Owner. Not built: cameras whose
speaker is only reachable through a separate vendor "audio alarm" API (needs real hardware to verify).

**Not in 4C:** escalation chains (§93, Phase 4.1), remote confirm/false-alert (4D RBAC), delivery receipts from
Meta's webhook (status stays SENT, not DELIVERED), attached snapshot images.

**To go live (4C):** `prisma db push` (GuardRecipient, GuardNotification, GuardIncidentLink); deploy; submit the 4
templates and set `BOOMBIZ_GUARD_WA_TEMPLATE_*` in Amplify when approved (email works before that); agent 0.4.2.

## 4D — roles, branches, subscription, installer passes, audit

Decisions (2026-09-14): Guard role per person set by the owner; 7-day grace; 30-day trial from the first Guard PC;
installers are Boombiz staff with a 24-hour pass from the internal console.

**Roles** (`lib/guard/access.ts`, unit-tested): OWNER (Boombiz owner — everything, every branch) · MANAGER
(incidents, clips, acknowledge, recipients, Guard PCs, audit, plan) · SECURITY (incidents, clips, acknowledge,
health) · VIEWER (incidents + snapshots only). Owner sets role + branches per staff member on **People**
(`GuardMember`); without one a Boombiz MANAGER is a Guard manager at their location and STAFF a viewer at theirs.
Every signed-in Guard API route resolves the viewer through `guardUser()` and filters by business + branch list
server-side; a static test fails if a route skips it (tenant isolation §65). Recipients' phones/emails are owner/
manager only; clips are hidden from viewers; revoking a Guard PC and managing people stay owner-only.

**Subscription** (`GuardSubscription`, `lib/guard/subscription.ts`): TRIAL (30 days from the first Guard PC
activation/pairing) → ACTIVE (paid-until recorded by a super-admin in the internal console; months stack) → GRACE
(7 days, banner + one owner email) → EXPIRED (one owner email). EXPIRED pauses WhatsApp/email, phone buzz except fire,
the remote dashboard (incidents, health) and cloud clip uploads (402; the PC keeps files and retries hourly without
using up retries). The shop PC's detection, clips and alarms never depend on it. Viewing the console never starts
the trial. Owners see **Plan** (§112–113 wording).

**Installer passes** (`GuardInstallerPass`, internal console → Boombiz Guard): a manager/super-admin gives an agent,
BDO or manager 24 hours on one business; the installer creates activation codes and checks Guard PCs, nothing else;
owner (People) or admins extend/revoke; actions audited as "Name (Boombiz)".

**Audit** tab (owner/manager): every Guard action in plain words — devices, incidents, links, recipients, roles,
installer passes, payments, warnings — with who did it; branch-limited managers see their branches' PCs and their own
actions.

**Not in 4D:** online payment (Paystack) for the subscription, escalation chains, agent auto-update (§100–101),
per-request replay nonces (access tokens are 30-min and every write is idempotent instead).

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

## Keeping Neon asleep — Guard's hot path is DynamoDB

Guard PCs heartbeat every 2 min, re-check their link every 60 s and refresh their token every ~30 min. From
Postgres that traffic would stop Neon's compute from ever suspending (always-on bill). So the Boombiz app
serves it from DynamoDB table `boombiz-{env}-guard-device` (`lib/guard/deviceStore.ts`):

| Item | Used by |
|---|---|
| `device#<id>` / DEVICE — copy of the Postgres device row | device/auth, /v1/device, heartbeat, incident routes |
| `secret#<hash>` / SECRET → device id | device/auth (secret lookup) |
| `device#<id>` / LATEST — last heartbeat, offlineNotifiedAt; GSI `heartbeat-index` | dashboard health, offline cron |
| `device#<id>` / `H#<iso>` — health sample ≤1 per 5 min, TTL 14 days | 24 h health history |

- Postgres stays the source of truth. Every device change (pair/start, claim, activate, rename/move) re-mirrors;
  a missing or not-yet-linked copy falls back to Postgres once and re-mirrors itself (covers older PCs).
- **Revoke writes DynamoDB first**; if that fails the revoke fails — a revoked PC can never keep signing in.
- The guard-health cron queries DynamoDB every 10 min and touches Postgres only when a PC actually went quiet
  (to push + audit); media/code/unclaimed cleanup runs once a day (03:00 Lagos, or `?housekeeping=1`).
- Postgres is now touched only by real events (incidents, pairing, someone opening the dashboard).
- Routine token refreshes are no longer audited (they would be Postgres writes twice an hour per PC);
  activation, pairing, revoke, offline/restored still are.
- Cost: ~$0.20 per PC per month in DynamoDB (on-demand; ~130k write units incl. the index).
- **Link check folded into the heartbeat (agent 0.4.1):** the heartbeat response carries `paired`,
  `business_name`, `location_name`. A linked PC calls `GET /v1/device` at most every 15 min; an unlinked one
  (pairing code waiting) still every 60 s. Revocation still shows at once — the next heartbeat or token
  refresh fails sign-in. Cuts each PC's cloud requests by about two-thirds (~66k → ~24k a month).

## Production status

- ✅ DynamoDB table **boombiz-prod-guard-device** created 2026-09-14 (on-demand, `heartbeat-index` GSI, TTL on
  `expiresAt`). Verified end to end with the app's deviceStore: heartbeat + sampling, latest read, history,
  quiet-device query, one-time offline claim, restored flag, cleanup.

- ✅ Bucket **boombiz-prod-guard-media** created 2026-09-13 (eu-west-1, account 065634457453): all public access
  blocked, SSE AES256 + bucket key, BucketOwnerEnforced, abort-incomplete-multipart after 1 day, tagged
  product=boombiz-guard. Verified with the app's own lib/s3 as `boombiz-app`: checksum-signed PUT ok, tampered body
  refused (400), HEAD size+SHA-256 match, signed GET returns the bytes, anonymous GET 403, delete ok.
- ✅ Schema pushed to production Neon 2026-09-13 (4A + 4B: GuardDevice columns, GuardActivationCode,
  GuardDeviceHealth, GuardAuditLog, GuardIncident, GuardIncidentMedia). Diff was Guard-only; drift check now empty.
- ✅ `BOOMBIZ_GUARD_TOKEN_SECRET` set in Amplify 2026-09-13 (64-char random, app level; merged 85 → 86 keys,
  none lost or changed). Takes effect on the next build.
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
