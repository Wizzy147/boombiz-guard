"""Setup funnel reports to the Boombiz cloud (GuardSetupRun).

The KPIs that say whether plug-and-play works: how often CCTV is found by
itself, connects first time, passes Guard Test, and reaches "protected"
without a technician — and how long that takes.

Best effort and outbound only: a report that fails is dropped, never retried
in a way that could slow setup down. Counts, brand names and yes/no outcomes
only — never an address, password or picture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import httpx

from ..database.models import Setting

log = logging.getLogger(__name__)

KEY = "setup_run_json"


class SetupTelemetry:
    def __init__(self, db, link, version: str) -> None:  # noqa: ANN001
        self.db = db
        self.link = link
        self.version = version
        self.run: dict = self._load() or {}

    def _load(self) -> dict | None:
        with self.db.session() as s:
            row = s.get(Setting, KEY)
            try:
                return json.loads(row.value) if row else None
            except ValueError:
                return None

    def _save(self) -> None:
        with self.db.session() as s:
            row = s.get(Setting, KEY)
            if row:
                row.value = json.dumps(self.run)
            else:
                s.add(Setting(key=KEY, value=json.dumps(self.run)))

    def start(self, mode: str = "AUTO") -> dict:
        self.run = {"run_id": f"run_{uuid.uuid4().hex}", "mode": mode,
                    "started_at": datetime.now(timezone.utc).isoformat(), "connect_attempts": 0}
        self._save()
        self.report("started")
        return self.run

    def ensure(self, mode: str = "AUTO") -> dict:
        return self.run if self.run.get("run_id") else self.start(mode)

    def note(self, **fields) -> None:  # noqa: ANN003
        self.ensure()
        self.run.update({k: v for k, v in fields.items() if v is not None})
        self._save()

    def bump(self, key: str) -> None:
        self.ensure()
        self.run[key] = int(self.run.get(key, 0)) + 1
        self._save()

    def seconds(self) -> int:
        try:
            start = datetime.fromisoformat(self.run["started_at"])
            return max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
        except (KeyError, ValueError):
            return 0

    def report(self, stage: str, **fields) -> None:  # noqa: ANN003
        """Fire-and-forget. Safe to call from a request handler."""
        self.note(**fields)
        body = {k: v for k, v in self.run.items()
                if k in ("run_id", "mode", "started_at", "devices_found", "cameras_found", "brands", "auto_discovered",
                         "connect_attempts", "connect_ok", "capacity", "needs_help", "help_reason", "test_passed",
                         "licensed")}
        body.update(stage=stage, installation_id=self.link.auth.installation_id(), agent_version=self.version,
                    duration_seconds=self.seconds())
        try:
            asyncio.get_running_loop().create_task(self._send(body))
        except RuntimeError:
            pass  # no loop (tests): skip

    async def _send(self, body: dict) -> None:
        headers = {}
        tok = self.link.auth.cached_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                await c.post(f"{self.link.base}/api/guard/v1/setup/run", json=body, headers=headers)
        except httpx.HTTPError:
            log.debug("setup report not sent (offline)")
