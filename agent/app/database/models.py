"""Local SQLite schema (Phase 1 §4–5).

`devices` and `cameras` follow the phase doc column for column. Credentials
live in `device_secrets` as DPAPI blobs (security/vault.py) — never in
`devices` or `cameras`, and `cameras.stream_main/sub` hold credential-free
URIs only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("dev"))
    ip_address: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)
    # Extra identity for dedupe (Phase 1 §8): IP alone changes with DHCP.
    mac_address: Mapped[str | None] = mapped_column(String)
    onvif_uuid: Mapped[str | None] = mapped_column(String)
    onvif_endpoint: Mapped[str | None] = mapped_column(String)
    rtsp_port: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String)
    manufacturer: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    firmware: Mapped[str | None] = mapped_column(String)
    serial_number: Mapped[str | None] = mapped_column(String)
    device_type: Mapped[str] = mapped_column(String, default="UNKNOWN")
    adapter_type: Mapped[str | None] = mapped_column(String)
    compatibility_status: Mapped[str] = mapped_column(String, default="UNKNOWN")
    # JSON: {"onvif":bool,"rtsp":bool,"audio":bool,"alarm_output":bool,"ptz":bool}
    capabilities: Mapped[str | None] = mapped_column(Text)
    # How it got here: DISCOVERED | MANUAL
    source: Mapped[str] = mapped_column(String, default="DISCOVERED")
    # Manual add (Phase 1 §23): the installer's connection type and, for
    # advanced installers, an RTSP URL — stored WITHOUT credentials.
    connection_type: Mapped[str] = mapped_column(String, default="auto")
    manual_stream_uri: Mapped[str | None] = mapped_column(String)
    # Human reason when compatibility is decided before any stream test
    # (e.g. a V380 that only streams through its cloud app).
    compatibility_reason: Mapped[str | None] = mapped_column(String)
    # Set when the last auth attempt was refused, so the UI can say so — and so
    # nothing retries it on its own (Phase 1 §11).
    auth_error: Mapped[str | None] = mapped_column(String)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    cameras: Mapped[list["Camera"]] = relationship(back_populates="device", cascade="all, delete-orphan")


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("cam"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    channel_number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String)
    stream_main: Mapped[str | None] = mapped_column(String)
    stream_sub: Mapped[str | None] = mapped_column(String)
    codec_main: Mapped[str | None] = mapped_column(String)
    codec_sub: Mapped[str | None] = mapped_column(String)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    guard_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    compatibility_status: Mapped[str] = mapped_column(String, default="UNKNOWN")
    # JSON report from the last compatibility test (streams/compat.py).
    last_test: Mapped[str | None] = mapped_column(Text)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    device: Mapped[Device] = relationship(back_populates="cameras")


class DeviceSecret(Base):
    """DPAPI-encrypted {username,password} per device. See security/vault.py."""

    __tablename__ = "device_secrets"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    blob: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AuditLog(Base):
    """PRD §46 — security-relevant actions. Never holds a secret."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Zone(Base):
    """Phase 2 §10. polygon_json holds NORMALISED points (§11)."""

    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("zone"))
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # SHELF | EXIT | RESTRICTED | CASHIER | STOCKROOM | FIRE_RISK | IGNORE | PRIVACY
    zone_type: Mapped[str] = mapped_column(String, nullable=False)
    polygon_json: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sensitivity: Mapped[str] = mapped_column(String, default="MEDIUM")  # LOW | MEDIUM | HIGH
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Schedule(Base):
    """Phase 2 §16. One row per weekday (0 = Monday). Times are local HH:MM."""

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("sch"))
    location_id: Mapped[str] = mapped_column(String, nullable=False, default="loc_default")
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    opens_at: Mapped[str | None] = mapped_column(String)
    closes_at: Mapped[str | None] = mapped_column(String)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)


class AiEvent(Base):
    """Phase 2 §30. Never holds a frame or an image — metadata only."""

    __tablename__ = "ai_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("evt"))
    camera_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    track_id: Mapped[str | None] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    severity: Mapped[str | None] = mapped_column(String)    # INFO | LOW | HIGH | CRITICAL
    confidence: Mapped[str | None] = mapped_column(String)  # LOW | MEDIUM | HIGH
    zone_id: Mapped[str | None] = mapped_column(String)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    # §51 pilot feedback: ACCURATE | FALSE_EVENT | UNSURE
    feedback: Mapped[str | None] = mapped_column(String)
    feedback_note: Mapped[str | None] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CameraAiConfig(Base):
    """Phase 2 §50 — per-camera feature switches."""

    __tablename__ = "camera_ai_config"

    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True)
    person: Mapped[bool] = mapped_column(Boolean, default=True)
    shelf: Mapped[bool] = mapped_column(Boolean, default=True)
    exit: Mapped[bool] = mapped_column(Boolean, default=True)
    restricted: Mapped[bool] = mapped_column(Boolean, default=True)
    after_hours: Mapped[bool] = mapped_column(Boolean, default=True)
    fire: Mapped[bool] = mapped_column(Boolean, default=False)  # experimental heuristic → OFF
    # §33 experimental. ON by default (user decision 2026-09-13); always LOW
    # confidence and the first feature paused under CPU load.
    concealment: Mapped[bool] = mapped_column(Boolean, default=True)
    # Exit/security-critical cameras keep more FPS under load (§6).
    priority: Mapped[str] = mapped_column(String, default="NORMAL")  # PRIMARY | NORMAL


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
