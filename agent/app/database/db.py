"""Engine, sessions, and the vault's storage backend."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .models import AuditLog, Base, DeviceSecret


class Database:
    def __init__(self, path: Path | str) -> None:
        url = "sqlite:///:memory:" if str(path) == ":memory:" else f"sqlite:///{Path(path).as_posix()}"
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if str(path) == ":memory:":
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _rec):  # noqa: ANN001
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        Base.metadata.create_all(self.engine)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ── CredentialStore protocol (security/vault.py) ────────────────
    def put_secret(self, device_id: str, blob: str) -> None:
        with self.session() as s:
            row = s.get(DeviceSecret, device_id)
            if row:
                row.blob = blob
            else:
                s.add(DeviceSecret(device_id=device_id, blob=blob))

    def get_secret(self, device_id: str) -> str | None:
        with self.session() as s:
            row = s.get(DeviceSecret, device_id)
            return row.blob if row else None

    def delete_secret(self, device_id: str) -> None:
        with self.session() as s:
            row = s.get(DeviceSecret, device_id)
            if row:
                s.delete(row)

    # ── Audit ────────────────────────────────────────────────────────
    def audit(self, action: str, target: str | None = None, **detail: object) -> None:
        with self.session() as s:
            s.add(AuditLog(action=action, target=target, detail=json.dumps(detail, default=str) if detail else None))

    def recent_audit(self, limit: int = 100) -> list[AuditLog]:
        with self.session() as s:
            return list(s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)))
