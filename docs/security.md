# Security — Phase 1

Each Phase 1 §27 requirement, with where it lives and how it's checked.

| Requirement | Implementation | Checked by |
|---|---|---|
| No CCTV credentials in logs | `security/redact.py` pattern-masks `rtsp://user:pass@`, `Authorization:`, `password=`, WS-Security `<Password>`/`<Nonce>`; the filter sits on every **handler** (`logging_setup.py`) so third-party loggers are covered too. FFmpeg stderr is redacted before it's kept. | `tests/test_security.py`, e2e "Passwords absent from agent logs" |
| Encrypted credential storage | Windows DPAPI (`CryptProtectData`, machine scope, app entropy) via ctypes; blobs in `device_secrets`, never in `devices`/`cameras`. Refuses to run unencrypted off Windows unless `GUARD_ALLOW_INSECURE_VAULT=1`. | DPAPI round-trip test; e2e scans the raw DB bytes for the password |
| No brute force / default passwords | One credentialed attempt per protocol; the only second protocol is ONVIF→brand API (Hikvision's separate ONVIF users). Generic RTSP probes paths **without** credentials first, then sends them once. A 401 stops everything. An AUTH_ERROR stream stops instead of retrying. | e2e "Wrong password costs exactly ONE login attempt" (the simulated recorder counts) |
| Polite scanning | WS-Discovery + TCP connect on CCTV ports only (554/8554/10554/80/8000/8800), own /24 only, 0.6 s timeouts. No banners beyond identification, no exploits. | code review (`discovery/scanner.py`) |
| No public RTSP exposure / port forwarding | The agent opens outbound connections only; nothing instructs router changes. The local API binds 127.0.0.1. | config default; `LocalGuard` re-checks the client address |
| Localhost-only setup API | Bind `127.0.0.1:7480`; client address must be loopback. | e2e |
| Authorization for configuration changes | Every `/api/v1` route needs `X-Guard-Token` (random 256-bit, in the data dir, handed to the UI in the URL fragment). | e2e "refuses a request with no token" |
| CSRF protection | Custom header requirement (no CORS is granted, so a cross-site page can't send it) + Origin allow-list. | e2e "refuses a cross-site Origin" |
| DNS rebinding | Host header must be 127.0.0.1/localhost:port. | e2e "refuses a foreign Host header" |
| Browser never sees credentials | Stored URIs are credential-free; previews are MJPEG from the agent; access by 2-minute single-camera tickets. Camera API responses carry no RTSP address. | e2e "Camera API returns no password and no RTSP address" |
| Sanitized errors | `GuardError` messages are written for installers; unexpected exceptions are logged (redacted), not returned. | — |
| Audit | `audit_logs`: discovery_run, camera_added/removed, credentials_updated, device_auth_failed, camera_tested, camera_guard_enabled/disabled, configuration_changed, agent_started, stream events. Never a secret. | `/api/v1/system/audit` |

## Open items before production

1. **FFmpeg command line holds the credentialed URL** — visible to other local
   users via the process list. Move to a pipe or a per-run temp file readable
   only by the service account.
2. ~~**Setup token file ACL**~~ — done in the installer (`installer/post-install.ps1`):
   `%PROGRAMDATA%\Boombiz Guard` is SYSTEM + Administrators only. Signed-in
   users can READ `setup-token` alone, because the tray app runs as the cashier;
   that token still lets any local user drive the localhost API.
3. **Signed installer and signed updates** (PRD §45) — the installer exists but
   is unsigned (pilots); `installer/build.ps1` signs when `GUARD_SIGN_CERT` is set.
   Signed updates not built.
4. **Dependency scanning in CI** — `pip-audit` + `npm audit` in the pipeline.
5. **Least-privilege service account** — run the WinSW service as
   `NT SERVICE\BoombizGuard` rather than LocalSystem.
