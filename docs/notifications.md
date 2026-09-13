# Pop-up notifications — Windows and phones

What pops up (user decision 2026-09-13): **HIGH and CRITICAL incidents plus health problems** —
possible unpaid exit, restricted area, after-hours intrusion, smoke/fire, staff reports marked
High/Critical, a camera offline for more than 5 minutes, all Guard cameras down, and an alarm output
that stopped responding. LOW incidents stay in the dashboard only. One feed
(`GET /api/v1/notifications?after=<cursor>`, `app/notify/feed.py`) serves both destinations, keyed
by (incident, severity) so a camera that goes LOW → HIGH pops once, when it becomes HIGH.

## Windows — tray app (`agent/tray/guard_tray.py`)

The agent is a Windows Service and services can't show anything on the desktop, so a small tray app
runs in the cashier's own session.

- Polls the agent on 127.0.0.1 every 3 s (works with no internet).
- Native Windows notifications. They never take focus — the checkout keeps the keyboard.
- Fire/smoke: alarm-style notification that stays until dismissed, looping alarm sound, never muted.
- Clicking a notification opens that incident in Guard (`#t=…&view=incidents&incident=<id>`).
- Menu: Open Guard · Guard Mode · Mute for 1 hour · choose alert types · Quit.
- Starts at login: `agent\tray\install-startup.ps1` (the signed installer will do this later).
- Log for support: `%APPDATA%\Boombiz Guard\tray.log` (what was shown, what was muted, failures).
- Verified live: two staff reports (HIGH, CRITICAL) → both shown within one poll, no errors.

## Phones — Guard web app + web push

```
Guard PC ──(outbound HTTPS, when online)──► guard.getboombiz.com ──web push──► phones
   cloud_outbox (queued offline,             GuardDevice / GuardAlert /       Guard installed to
   retried with backoff)                     GuardPushSubscription            the home screen
```

1. **Pair** — Guard PC calls `POST /api/guard/v1/pair/start`, gets a device token (stored DPAPI-encrypted;
   the cloud keeps only its SHA-256) and a code like `ABCD-EFGH` (15 min). The **owner** signs in on
   `guard.getboombiz.com/dashboard` and enters the code.
2. **Deliver** — the agent copies feed items into its outbox every 5 s and posts them every 10 s.
   Offline → stays queued (5 s → 5 min backoff). Idempotent per (device, key): a resend after an outage
   answers 409 and never buzzes twice.
3. **Stale alerts (PRD §39)** — after an outage, HIGH alerts older than 60 min are stored for the phone
   page but don't buzz; CRITICAL always buzzes.
4. **Push** — `lib/guard/push.ts` sends to every phone of that business, skipping groups a person muted
   (fire can't be muted). Dead subscriptions (404/410, or 10 failures) are removed.
5. **Phone page** — `/dashboard` on the Guard host: turn alerts on, send a test, mute types, link a Guard
   PC (owner), recent alerts. iPhone: needs iOS 16.4+ and "Add to Home Screen" first; the page says so.

What reaches the cloud: incident ref, type, severity, camera **name**, time, title. No video, no
snapshot, no CCTV address or password. Snapshot and clip stay on the shop PC until Phase 4 upload.

## Before phone push works in production

1. `npx prisma db push` against production Neon — adds `GuardDevice`, `GuardPushSubscription`,
   `GuardAlert` (additive only).
2. Production VAPID keys (`npx web-push generate-vapid-keys`) → Amplify env vars
   `GUARD_VAPID_PUBLIC_KEY`, `GUARD_VAPID_PRIVATE_KEY`, `GUARD_VAPID_SUBJECT` **and** add them to the
   `amplify.yml` env allowlist (otherwise they're silently missing at request time).
3. Deploy, then on a phone: sign in at guard.getboombiz.com → (iPhone: Add to Home Screen) → Turn on
   alerts → Send a test. On the Guard PC: Settings → Link to phone alerts → enter the code on the phone.

Dev keys live only in the Boombiz repo's git-ignored `.env.local`.

## Not verified yet

- End-to-end phone push: needs steps 1–3 above. The agent side is covered by tests against a fake
  cloud (queue while offline, deliver once, stale-HIGH dashboard-only, unpaired stays queued) and the
  cloud code typechecks, but no real phone has received a Guard push.
- The agent's setup UI has no "Link to phone alerts" screen yet — pairing is available through
  `POST /api/v1/cloud/pair` only.
