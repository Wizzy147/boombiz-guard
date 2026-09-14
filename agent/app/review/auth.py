"""Offline PIN sign-in for the people who review incidents (Phase 3 §35).

PINs are 4–6 digits, stored as PBKDF2-SHA256 (200k iterations, per-user
salt) — never in plain text, never logged. Five wrong PINs lock that person
for 5 minutes (a 4-digit PIN must not be guessable at the cashier). Sessions are
random 256-bit tokens held in memory for 12 hours: a restarted agent simply
asks people to sign in again.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..database.db import Database
from ..database.models import LocalUser
from .permissions import PEOPLE_ROLES

ITERATIONS = 200_000
MAX_FAILS = 5
LOCK_MINUTES = 5
SESSION_HOURS = 12
PIN_RE = re.compile(r"^\d{4,6}$")


class AuthError(Exception):
    pass


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, ITERATIONS)
    return f"pbkdf2-sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


@dataclass
class Session:
    token: str
    user_id: str
    name: str
    role: str
    expires: float


class LocalAuth:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._sessions: dict[str, Session] = {}

    # ── users ────────────────────────────────────────────────────────
    def has_owner(self) -> bool:
        with self.db.session() as s:
            return s.scalar(select(LocalUser).where(LocalUser.role == "OWNER", LocalUser.active.is_(True))) is not None

    def users(self) -> list[dict]:
        with self.db.session() as s:
            return [{"id": u.id, "name": u.name, "role": u.role, "active": u.active}
                    for u in s.scalars(select(LocalUser).order_by(LocalUser.created_at))]

    def create_user(self, name: str, role: str, pin: str) -> dict:
        name = name.strip()
        if not name:
            raise AuthError("Enter the person's name.")
        if role not in PEOPLE_ROLES:
            raise AuthError("Choose Owner, Manager or Security.")
        if not PIN_RE.match(pin or ""):
            raise AuthError("A PIN is 4 to 6 digits.")
        with self.db.session() as s:
            u = LocalUser(name=name[:60], role=role, pin_hash=hash_pin(pin))
            s.add(u)
            s.flush()
            out = {"id": u.id, "name": u.name, "role": u.role, "active": True}
        self.db.audit("user_added", out["id"], role=role)
        return out

    def set_active(self, user_id: str, active: bool) -> None:
        with self.db.session() as s:
            u = s.get(LocalUser, user_id)
            if not u:
                raise AuthError("That person no longer exists.")
            u.active = active
        for t, sess in list(self._sessions.items()):
            if sess.user_id == user_id and not active:
                self._sessions.pop(t, None)
        self.db.audit("user_disabled" if not active else "user_enabled", user_id)

    # ── sign-in ──────────────────────────────────────────────────────
    def sign_in(self, user_id: str, pin: str) -> Session:
        """Every outcome is COMMITTED before an error is raised: the session
        context rolls back on exceptions, which silently discarded the failed
        attempt counter and meant the lockout never engaged."""
        now = datetime.now(timezone.utc)
        error: str | None = None
        sess: Session | None = None
        with self.db.session() as s:
            u = s.get(LocalUser, user_id)
            if not u or not u.active:
                error = "That PIN didn't work."
            else:
                locked = u.locked_until.replace(tzinfo=timezone.utc) if u.locked_until and u.locked_until.tzinfo is None else u.locked_until
                if locked and locked > now:
                    mins = max(1, int((locked - now).total_seconds() // 60) + 1)
                    error = f"Too many wrong PINs. Try again in {mins} minute{'s' if mins > 1 else ''}."
                elif not verify_pin(pin or "", u.pin_hash):
                    u.failed_attempts += 1
                    if u.failed_attempts >= MAX_FAILS:
                        u.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                        u.failed_attempts = 0
                    error = "That PIN didn't work."
                else:
                    u.failed_attempts, u.locked_until = 0, None
                    sess = Session(secrets.token_urlsafe(32), u.id, u.name, u.role, time.time() + SESSION_HOURS * 3600)
        if error:
            self.db.audit("pin_failed", user_id)
            raise AuthError(error)
        assert sess is not None
        self._sessions[sess.token] = sess
        self.db.audit("user_signed_in", sess.user_id, role=sess.role)
        return sess

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        s = self._sessions.get(token)
        if s and s.expires > time.time():
            return s
        self._sessions.pop(token or "", None)
        return None

    def sign_out(self, token: str) -> None:
        self._sessions.pop(token, None)
