# Boombiz Guard — WhatsApp message templates (to submit to Meta)

Submit these in **WhatsApp Manager → Message templates → Create template** on the Boombiz WhatsApp Business
Account (the number behind `BOOMBIZ_WA_PHONE_NUMBER_ID`). Category **Utility** for all four (they're alerts
about the customer's own account/service, not marketing — Utility is also the cheaper category).
Language: **English** — use the same code as `BOOMBIZ_WA_DEFAULT_LANG` (currently `en`).

When each is approved, set its name in Amplify (merge — never replace the env map), then redeploy:

| Env var | Template |
|---|---|
| `BOOMBIZ_GUARD_WA_TEMPLATE_ALERT` | `guard_incident_alert` |
| `BOOMBIZ_GUARD_WA_TEMPLATE_FIRE` | `guard_fire_alert` |
| `BOOMBIZ_GUARD_WA_TEMPLATE_OFFLINE` | `guard_device_offline` |
| `BOOMBIZ_GUARD_WA_TEMPLATE_RESTORED` | `guard_protection_restored` |

Until a template is set, that alert goes by email only and the incident page says "WhatsApp alerts start once
Meta approves the Guard message templates". Variable order below is exactly what the code sends — don't reorder.

---

## 1. `guard_incident_alert`  (Utility)

**Body**
```
Boombiz Guard alert

{{1}} at {{2}}.

Camera: {{3}}
Time: {{4}}
Severity: {{5}}

Open the link to see the snapshot and clip. The link works for 30 minutes.
```
Sample values for Meta: `{{1}}` Possible unpaid exit · `{{2}}` Owerri Branch · `{{3}}` Main Exit · `{{4}}` 1:28 PM · `{{5}}` HIGH

**Button** — Visit website, dynamic URL: `https://guard.getboombiz.com/i/{{1}}` · text **View incident**
(sample: `https://guard.getboombiz.com/i/Xk3v9Qp2LmN8rT5wY7zA1bC4dE6fG0hJ`)

## 2. `guard_fire_alert`  (Utility)

**Body**
```
URGENT — Boombiz Guard

Possible fire or smoke detected at {{1}} — {{2}}.

Check the location immediately.

Time: {{3}}
```
Samples: `{{1}}` Owerri Branch · `{{2}}` Stockroom · `{{3}}` 1:28 PM

**Button** — dynamic URL `https://guard.getboombiz.com/i/{{1}}` · text **View incident**

Wording rule (Phase 4 §27): always "possible" — Guard never says a fire is confirmed.

## 3. `guard_device_offline`  (Utility)

**Body**
```
Boombiz Guard: {{1}} at {{2}} has not checked in since {{3}}.

It may be switched off or without internet. If the computer is on, the shop's local alarm still works.
```
Samples: `{{1}}` OWR Guard PC · `{{2}}` Owerri Branch · `{{3}}` 1:28 PM

**Button** — static URL `https://guard.getboombiz.com/dashboard/locations` · text **Open Guard**

## 4. `guard_protection_restored`  (Utility)

**Body**
```
Boombiz Guard protection at {{1}} has been restored.

{{2}}

Guard is watching your cameras again. Open Guard to check everything is working.
```
Samples: `{{1}}` Aba Branch · `{{2}}` 2 of 2 cameras online.

**Button** — static URL `https://guard.getboombiz.com/dashboard/locations` · text **Open Guard**

---

Tips so Meta approves first time: keep the samples realistic (above), don't add promotional lines, and don't
start or end the body with a variable (the bodies above don't).
