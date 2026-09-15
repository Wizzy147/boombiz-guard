# Auto Setup — plug-and-play Guard (2.5.2)

Owner decision 2026-09-15: plug-and-play is a core product requirement, not an
installation convenience. A shop with supported CCTV should go from download to
protected in 10–15 minutes without a technician.

```
Download free → Start Automatic Setup → checks → "We found your CCTV" → CCTV login
  → (no licence)  "Guard Ready" → Activate → browser sign-in → package → pay ─┐
  → (licensed)    recommended cameras → products / exit areas → Guard Test → PROTECTED
                                                                        ↑───────┘
```

## Modes

| | Who | What |
|---|---|---|
| **Auto Setup** (default) | merchants, BDOs | discovery, protocol, stream and codec choice, capacity benchmark, recommendations, guided areas, Guard Test |
| **Advanced setup** | Boombiz technicians, certified installers | the Phase 1 screens: manual IP / stream address, every channel, stream details. Settings → Setup → Advanced setup |

Auto Setup never shows RTSP, ONVIF, H.264, ports, stream URLs, substreams or IP
addresses. When it gives up it says **Technical assistance required** and offers
a Boombiz visit and Advanced setup; the reason is reported (see KPIs).

## Plan and licence (app/licence.py)

Pricing (owner decision 2026-09-15, web `lib/guard/tiers.ts`): **₦35,000 setup
fee** for everyone — self-setup or BDO — including the **first month free**;
then **₦10,000/month for up to 4 AI cameras, +₦7,000/month per extra camera**.
The monthly plan covers local protection too: when it lapses past 7 grace days,
the PC drops to demo mode.

The cloud's answer arrives with every sign-in, link check and heartbeat, as a
**signed token** (Ed25519; web `lib/guard/licenceToken.ts`, key
`BOOMBIZ_GUARD_LICENCE_KEY`; the public half is built into `app/licence.py`).
The token names this device and installation and carries `valid_until` (end of
the paid/free month + grace), so the PC keeps protecting through an internet
outage and stops on its own if nobody renews. Editing guard.db or copying
another PC's token doesn't work.

| status | cameras protected |
|---|---|
| `ACTIVE` | the plan's camera count, until `valid_until` |
| `LEGACY` | 4 — shops a BDO set up before the monthly plan; same dates and renewal |
| `EXPIRED` | 0 — the plan lapsed; renewing switches it back on within a heartbeat |
| `GRANDFATHERED` | 2 — local only: this PC was protecting before licences and hasn't heard from the cloud yet |
| `DEMO` / `REVOKED` | 0 — Compatibility & Demo mode: scan, connect, test, recommend |

**Cloud services follow the same plan.** Without a live plan (expired past
grace, or setup never paid) the cloud refuses alert pushes, incident sync and
media uploads (402 — the PC keeps them queued and paused retries don't use up
the media retry limit), sends no phone, WhatsApp or email alerts (fire
included), disables incident links, and the owner's console shows only the
Plan page. Device sign-in, heartbeats and licence checks keep working so a
renewal reaches the PC within a heartbeat.

Only an explicit cloud answer changes the plan; offline, errors and old clouds
change nothing. A licence sent without a token (signing key not set) is trusted
for 72 hours of the current run only. Every minute (and after each answer) the
agent re-checks: when the limit goes down, the cameras over it stop
(`DeviceService.enforce_limit`). `GUARD_MAX_CAMERAS` overrides everything (lab,
support).

## Computer capacity (app/setup/benchmark.py)

Guard runs its own person model on real CCTV frames (a synthetic frame before
any camera is connected) and sizes:

- **by processor**: half the computer (the POS comes first), ~5 analyses per
  second per camera → `500 / (median inference ms × 5)`
- **by memory**: 3 GB kept free, ~350 MB per camera
- 2-core or <5 GB machines: at most 1 camera. Never more than 16.

If the licence covers more cameras than the PC can handle, setup says so and
recommends the highest-risk cameras up to the PC's capacity.

## Recommendations (app/setup/recommend.py)

Signals: the camera's name on the recorder (entrance/exit/door, shop floor,
shelf/aisle, store room, cashier, outside, office…), how many people Guard saw
in its picture, and whether it works. With 2+ slots the pick covers one
products camera and one exit camera when the shop has both. Shown as
"Recommended"; the merchant can change it.

## Areas

"Where are your products?" and "Where do customers leave?" — drag a box on the
camera picture. Creates `SHELF` and `EXIT` zones named `… (Setup)`; running
again replaces only those, so zones drawn in Advanced setup stay.

## Guard Test (app/setup/guard_test.py)

Walking steps are read from the AI's own event feed since the test started:
person (any person event or a live track), product area (`SHELF_INTERACTION` or
entry into a SHELF zone), exit (`EXIT_APPROACH` or entry into an EXIT zone). A
skipped step never counts as passed.

Then, automatically: a `GUARD_TEST` incident (LOW, real media pipeline — no
alarm rule, no pop-up, never synced), snapshot, clip, the computer's alarm
sound (1 s), cloud link, and a test alert to the owner's phone
(`/api/guard/v1/setup/test-alert`; adds the owner as the first recipient if
nobody is set up, 5 per hour per PC).

## Browser sign-in (web repo lib/guard/link.ts)

`POST /api/guard/v1/link/start` gives the PC its device secret immediately
(linked to nobody) and a link `guard.getboombiz.com/link?c=…` carrying what
setup found (counts, brands, capacity — no addresses). The owner signs in or
creates an account there, taps **Link this computer**, picks a package and
pays (Paystack `gdl_…`). The signed webhook issues the licence and starts the
free cloud month; the PC hears it on its next check. The merchant's password
never reaches the Guard PC.

## KPIs

Every milestone is reported to `POST /api/guard/v1/setup/run`
(`GuardSetupRun`): started, discovered, connected, recommended, zones, tested,
protected, help (with a reason), advanced. The internal Guard page shows:
CCTV found automatically, connected automatically, Guard Test pass rate,
reached protected, **finished without a technician** (target 80%, then 90%),
median time to protected, packages sold online (with BDO code).

## Limits (honest)

- Recommendations are heuristics (names + people seen), not scene
  understanding. No automatic exit detection yet — the merchant draws it.
- The benchmark measures the AI model only; very weak GPUs/iGPUs and heavy POS
  software can still make a PC slower in practice than predicted. The adaptive
  policy (performance/adaptive_policy.py) sheds load if it is.
- Discovery is the same /24 CCTV-port sweep as Phase 1: a recorder on another
  subnet or VLAN needs Advanced setup.
