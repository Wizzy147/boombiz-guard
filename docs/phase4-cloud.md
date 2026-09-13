# Phase 4 — Cloud, remote alerts, sync, multi-store

Built sprint by sprint (user decision 2026-09-13). Local Guard stays the primary protection: no cloud
call is ever on the path of detection, incident capture or the local alarm.

| Sprint | Scope | State |
|---|---|---|
| 4A | Device cloud & authentication: activation codes, device secret → access tokens, heartbeats, remote health, offline detection, revoke | **Built** (this doc) |
| 4B | Incident sync & media: priority sync queue, idempotent incident API, snapshot + ≤15 s clip upload to a private bucket, retention | Next |
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

## To go live (4A)

1. `npx prisma db push` against production Neon (additive: GuardDevice columns + GuardActivationCode,
   GuardDeviceHealth, GuardAuditLog).
2. Set `BOOMBIZ_GUARD_TOKEN_SECRET` (long random) in Amplify. Without it tokens fall back to a key derived
   from NEXTAUTH_SECRET, which works but ties Guard tokens to that secret's rotation.
3. Deploy the Boombiz app.
4. `npx tsx scripts/provision-cron.ts` to create the `boombiz-guard-health` schedule.
5. Install agent 0.4.0 on the Guard PC.
