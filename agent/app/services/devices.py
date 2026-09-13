"""Discovery results → devices → authenticated channels → cameras.

The rules this file is responsible for keeping:

  · ONE attempt per protocol with the credentials the installer typed. The
    only time the same credentials go to a second protocol is ONVIF → the
    brand's own API, because Hikvision keeps a separate ONVIF user list and
    an admin login that fails ONVIF is normal there. No other retries, no
    guessed usernames, no default passwords (Phase 1 §11, §27).
  · Credentials go into the vault the moment they are proven, and nowhere
    else. Everything returned to the API is credential-free.
  · The Guard Basic limit (2 analysed cameras) is enforced here, not in the UI.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select

from ..adapters import registry
from ..adapters.base import AuthResult, Channel, Compatibility, Credentials
from ..adapters.v380 import V380Adapter
from ..config import Settings
from ..database.db import Database
from ..database.models import Camera, Device, iso_utc
from ..discovery.scanner import Candidate, Scanner
from ..security.redact import redact
from ..security.vault import CredentialVault
from ..streams import compat, tester
from ..streams.urls import split_credentials, with_credentials
from .streams import StreamManager

log = logging.getLogger(__name__)

AUTH_FAILED_MSG = (
    "Authentication failed. Confirm the username and password with the business owner, "
    "or use the manufacturer's official password recovery."
)
LOCKED_MSG = (
    "The CCTV system has locked this account after too many attempts. "
    "Wait for it to unlock (usually 30 minutes), then try once more."
)
UNREACHABLE_MSG = "Guard could not reach this device. Check that it is powered on and connected to the shop network."


class GuardError(Exception):
    """A plain-language problem to show the installer."""


class LimitError(GuardError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DeviceService:
    def __init__(self, db: Database, vault: CredentialVault, streams: StreamManager, settings: Settings) -> None:
        self.db = db
        self.vault = vault
        self.streams = streams
        self.settings = settings
        self.scanner: Scanner | None = None
        self.scan_error: str | None = None

    # ── discovery ───────────────────────────────────────────────────
    async def run_discovery(self) -> list[str]:
        self.scanner = Scanner(timeout=self.settings.discovery_timeout_s)
        self.scan_error = None
        try:
            found = await self.scanner.scan()
        except Exception as e:  # surface, don't crash the agent
            self.scan_error = "The network scan stopped unexpectedly. Try again."
            log.exception("discovery failed: %s", redact(e))
            return []
        ids = [self.upsert_candidate(c) for c in found]
        self.db.audit("discovery_run", None, found=len(ids))
        return ids

    def discovery_status(self) -> dict:
        if not self.scanner:
            return {"stage": "idle", "checked": 0, "total": 0, "found": 0, "error": None}
        return {**self.scanner.progress, "error": self.scan_error}

    def upsert_candidate(self, c: Candidate) -> str:
        with self.db.session() as s:
            dev = None
            # Dedupe (Phase 1 §8): ONVIF UUID, then MAC, then IP.
            if c.onvif and c.onvif.uuid:
                dev = s.scalar(select(Device).where(Device.onvif_uuid == c.onvif.uuid))
            if dev is None and c.mac:
                dev = s.scalar(select(Device).where(Device.mac_address == c.mac))
            if dev is None:
                dev = s.scalar(select(Device).where(Device.ip_address == c.ip))
            if dev is None:
                dev = Device(ip_address=c.ip, source="DISCOVERED")
                s.add(dev)
            dev.ip_address = c.ip
            dev.mac_address = c.mac or dev.mac_address
            dev.rtsp_port = c.rtsp_port or dev.rtsp_port
            dev.port = c.http_port or dev.port
            dev.last_seen_at = _now()
            if c.onvif:
                dev.onvif_uuid = c.onvif.uuid or dev.onvif_uuid
                dev.onvif_endpoint = c.onvif.endpoint or dev.onvif_endpoint
                dev.name = dev.name or c.onvif.scope("name")
                dev.model = dev.model or c.onvif.scope("hardware")
            dev.manufacturer = dev.manufacturer or c.brand or c.mac_vendor or (c.onvif and c.onvif.manufacturer_hint)
            dev.adapter_type = dev.adapter_type or c.adapter_hint
            caps = json.loads(dev.capabilities or "{}")
            caps["onvif"] = caps.get("onvif") or bool(c.onvif)
            caps["rtsp"] = caps.get("rtsp") or bool(c.rtsp_port)
            dev.capabilities = json.dumps(caps)
            if dev.compatibility_status in ("UNKNOWN", "OFFLINE") and self.vault.get_credentials(dev.id) is None:
                dev.compatibility_status = Compatibility.AUTH_REQUIRED.value
            s.flush()
            return dev.id

    # ── manual add (Phase 1 §23) ────────────────────────────────────
    async def add_manual(
        self, *, name: str | None, ip: str, port: int | None, username: str, password: str,
        connection_type: str = "auto", rtsp_url: str | None = None,
    ) -> dict:
        clean_uri = None
        creds = Credentials(username, password)
        if rtsp_url:
            if not rtsp_url.lower().startswith("rtsp://"):
                raise GuardError("An RTSP address starts with rtsp://")
            clean_uri, embedded = split_credentials(rtsp_url)
            creds = embedded if (embedded and not username) else creds
            ip = ip or (urlparse(clean_uri).hostname or "")
        if not ip:
            raise GuardError("Enter the device's IP address.")
        with self.db.session() as s:
            # A device the scan already found at this address is the same
            # device — reuse it rather than listing it twice.
            dev = s.scalar(select(Device).where(Device.ip_address == ip))
            if dev is None:
                dev = Device(ip_address=ip)
                s.add(dev)
            dev.port = port or dev.port
            dev.name = name or dev.name
            dev.source = "MANUAL"
            dev.connection_type = connection_type or "auto"
            dev.manual_stream_uri = clean_uri
            dev.adapter_type = "rtsp" if clean_uri else (None if connection_type in (None, "auto") else connection_type)
            if clean_uri:
                dev.rtsp_port = urlparse(clean_uri).port or 554
            dev.compatibility_status = Compatibility.AUTH_REQUIRED.value
            s.flush()
            dev_id = dev.id
        self.db.audit("camera_added", dev_id, source="manual", connection_type=connection_type)
        return await self.authenticate(dev_id, creds.username, creds.password)

    # ── authentication (Phase 1 §11) ────────────────────────────────
    async def authenticate(self, device_id: str, username: str, password: str) -> dict:
        if not username:
            raise GuardError("Enter the CCTV username.")
        creds = Credentials(username, password)
        with self.db.session() as s:
            dev = s.get(Device, device_id)
            if not dev:
                raise GuardError("That device is no longer in the list. Scan again.")
            host, port = dev.ip_address, dev.port
            adapters = registry.build(
                onvif_endpoint=dev.onvif_endpoint, rtsp_port=dev.rtsp_port,
                manual_uri=dev.manual_stream_uri, prefer=dev.connection_type if dev.connection_type != "auto" else dev.adapter_type,
            )
            forced = dev.connection_type not in (None, "auto")

        chosen = None
        outcome: AuthResult | None = None
        v380_reason: str | None = None
        onvif_rejected = False
        for adapter in adapters:
            if forced and adapter.key != dev.connection_type:
                continue
            if adapter.key != "onvif" and adapter.key != "rtsp" and not forced:
                if not await adapter.probe(host, port):
                    continue
            if adapter.key == "onvif" and not dev.onvif_endpoint and not await adapter.probe(host, port):
                continue
            if onvif_rejected and adapter.key not in ("hikvision", "dahua"):
                break  # the ONVIF→brand API hand-off is the only second attempt
            result = await adapter.authenticate(host, port, creds)
            log.info("auth device=%s adapter=%s result=%s", device_id, adapter.key, result.value)
            if isinstance(adapter, V380Adapter):
                v380_reason = adapter.last_reason
            if result == AuthResult.OK:
                chosen = adapter
                break
            if result == AuthResult.LOCKED:
                outcome = result
                break
            if result == AuthResult.BAD_CREDENTIALS:
                outcome = result
                if adapter.key == "onvif" and not onvif_rejected:
                    onvif_rejected = True
                    continue
                break
            outcome = outcome or result

        if chosen is None:
            msg = {
                AuthResult.BAD_CREDENTIALS: AUTH_FAILED_MSG,
                AuthResult.LOCKED: LOCKED_MSG,
            }.get(outcome, v380_reason or UNREACHABLE_MSG)
            with self.db.session() as s:
                dev = s.get(Device, device_id)
                dev.auth_error = msg
                if v380_reason:
                    dev.compatibility_status = Compatibility.INCOMPATIBLE.value
                    dev.compatibility_reason = v380_reason
                elif outcome in (AuthResult.BAD_CREDENTIALS, AuthResult.LOCKED):
                    dev.compatibility_status = Compatibility.AUTH_REQUIRED.value
            self.db.audit("device_auth_failed", device_id, result=(outcome.value if outcome else "NONE"))
            return {"ok": False, "error": msg, "device": self.device_dict(device_id)}

        info = await chosen.get_device_info(host, port, creds)
        channels = await chosen.list_channels(host, port, creds)
        self.vault.save_credentials(device_id, username, password)
        with self.db.session() as s:
            dev = s.get(Device, device_id)
            dev.auth_error = None
            dev.adapter_type = chosen.key
            dev.manufacturer = info.manufacturer or dev.manufacturer
            dev.model = info.model or dev.model
            dev.firmware = info.firmware or dev.firmware
            dev.serial_number = info.serial_number or dev.serial_number
            dev.device_type = info.device_type.value
            dev.capabilities = json.dumps({
                "onvif": info.onvif or chosen.key == "onvif", "rtsp": info.rtsp, "audio": info.audio,
                "alarm_output": info.alarm_output, "ptz": info.ptz, "channel_count": info.channel_count,
            })
            dev.compatibility_status = Compatibility.UNKNOWN.value if channels else Compatibility.INCOMPATIBLE.value
            dev.compatibility_reason = None if channels else "Signed in, but no local video stream was found."
            dev.last_seen_at = _now()
            self._sync_cameras(s, dev, channels)
        self.db.audit("credentials_updated", device_id, adapter=chosen.key)
        for cam in self.cameras(device_id):
            await self.streams.restart_guard(cam["id"])
        return {"ok": True, "device": self.device_dict(device_id)}

    def _sync_cameras(self, s, dev: Device, channels: list[Channel]) -> None:  # noqa: ANN001
        existing = {c.channel_number: c for c in dev.cameras}
        for ch in channels:
            cam = existing.pop(ch.number, None)
            if cam is None:
                cam = Camera(device_id=dev.id, channel_number=ch.number)
                s.add(cam)
                dev.cameras.append(cam)
            main = next((p for p in ch.profiles if p.is_main), ch.profiles[0] if ch.profiles else None)
            subs = [p for p in ch.profiles if p is not main]
            # Guard wants the smallest usable stream (PRD §17): prefer the
            # lowest-resolution non-main profile.
            sub = min(subs, key=lambda p: (p.width or 10**6) * (p.height or 10**6)) if subs else None
            cam.name = cam.name if (cam.name and cam.name != ch.name and cam.guard_enabled) else ch.name
            cam.stream_main = main.uri if main else None
            cam.stream_sub = sub.uri if sub else None
            cam.codec_main = main.codec if main else None
            cam.codec_sub = sub.codec if sub else None
            ref = sub or main
            if ref:
                cam.width, cam.height, cam.fps = ref.width, ref.height, ref.fps
            cam.online = ch.online
        for gone in existing.values():
            gone.online = False  # keep history; a channel can come back

    # ── credentials → URL (StreamManager's resolver) ────────────────
    def stream_url(self, camera_id: str) -> str | None:
        with self.db.session() as s:
            cam = s.get(Camera, camera_id)
            if not cam:
                return None
            uri = cam.stream_sub or cam.stream_main
            dev_id = cam.device_id
        if not uri:
            return None
        creds = self.vault.get_credentials(dev_id)
        return with_credentials(uri, Credentials(*creds) if creds else None)

    # ── compatibility test (Phase 1 §13–14) ─────────────────────────
    async def test_camera(self, camera_id: str, seconds: int | None = None) -> dict:
        with self.db.session() as s:
            cam = s.get(Camera, camera_id)
            if not cam:
                raise GuardError("That camera is no longer in the list.")
            dev = cam.device
            caps = json.loads(dev.capabilities or "{}")
            has_sub = cam.stream_sub is not None
            dev_id = dev.id
        creds = self.vault.get_credentials(dev_id)
        url = self.stream_url(camera_id)
        result = await tester.test_stream(url, seconds) if (url and creds) else None
        report = compat.classify(
            authenticated=creds is not None, test=result, has_substream=has_sub,
            onvif=bool(caps.get("onvif")), alarm_output=bool(caps.get("alarm_output")), audio=bool(caps.get("audio")),
        )
        payload = {"report": report.to_dict(), "test": result.to_dict() if result else None}
        with self.db.session() as s:
            cam = s.get(Camera, camera_id)
            cam.compatibility_status = report.status.value
            cam.last_test = json.dumps(payload)
            cam.tested_at = _now()
            if result and result.connected:
                cam.width, cam.height = result.width or cam.width, result.height or cam.height
                cam.fps = result.nominal_fps or cam.fps
                if cam.stream_sub:
                    cam.codec_sub = result.codec or cam.codec_sub
                else:
                    cam.codec_main = result.codec or cam.codec_main
                cam.online = True
            dev = cam.device
            order = [Compatibility.COMPATIBLE.value, Compatibility.LIMITED.value]
            statuses = [c.compatibility_status for c in dev.cameras]
            best = next((st for st in order if st in statuses), None)
            dev.compatibility_status = best or report.status.value
        self.db.audit("camera_tested", camera_id, status=report.status.value)
        return {"camera": self.camera_dict(camera_id), **payload}

    # ── Guard selection (PRD §11–12) ────────────────────────────────
    async def set_guard(self, camera_id: str, enabled: bool) -> dict:
        with self.db.session() as s:
            cam = s.get(Camera, camera_id)
            if not cam:
                raise GuardError("That camera is no longer in the list.")
            if enabled and not cam.guard_enabled:
                active = s.scalars(select(Camera).where(Camera.guard_enabled.is_(True))).all()
                if len(active) >= self.settings.max_guard_cameras:
                    names = ", ".join(c.name or "a camera" for c in active)
                    raise LimitError(
                        f"Guard Basic analyses up to {self.settings.max_guard_cameras} cameras. "
                        f"Turn one off first ({names})."
                    )
                if cam.compatibility_status == Compatibility.INCOMPATIBLE.value:
                    raise GuardError("This camera failed the compatibility test, so Guard can't use it.")
                if not (cam.stream_sub or cam.stream_main):
                    raise GuardError("This camera has no video stream Guard can open.")
            cam.guard_enabled = enabled
        self.db.audit("camera_guard_enabled" if enabled else "camera_guard_disabled", camera_id)
        if enabled:
            await self.streams.start_guard(camera_id)
        else:
            await self.streams.stop_guard(camera_id)
        return self.camera_dict(camera_id)

    async def remove_device(self, device_id: str) -> None:
        for cam in self.cameras(device_id):
            await self.streams.stop_guard(cam["id"])
        self.vault.delete_credentials(device_id)
        with self.db.session() as s:
            dev = s.get(Device, device_id)
            if dev:
                s.delete(dev)
        self.db.audit("camera_removed", device_id)

    def guard_camera_ids(self) -> list[str]:
        with self.db.session() as s:
            return list(s.scalars(select(Camera.id).where(Camera.guard_enabled.is_(True))))

    # ── read models (never carry secrets) ───────────────────────────
    def device_dict(self, device_id: str) -> dict | None:
        with self.db.session() as s:
            d = s.get(Device, device_id)
            return self._dev(d) if d else None

    def devices(self) -> list[dict]:
        with self.db.session() as s:
            return [self._dev(d) for d in s.scalars(select(Device).order_by(Device.created_at))]

    def _dev(self, d: Device) -> dict:
        caps = json.loads(d.capabilities or "{}")
        return {
            "id": d.id, "ip_address": d.ip_address, "port": d.port, "name": d.name,
            "manufacturer": d.manufacturer, "model": d.model, "firmware": d.firmware,
            "serial_number": d.serial_number, "device_type": d.device_type, "adapter_type": d.adapter_type,
            "compatibility": d.compatibility_status, "compatibility_reason": d.compatibility_reason,
            "authentication_required": self.vault.get_credentials(d.id) is None,
            "auth_error": d.auth_error, "source": d.source, "mac_address": d.mac_address,
            "capabilities": caps, "channel_count": len(d.cameras),
            "last_seen_at": iso_utc(d.last_seen_at),
        }

    def cameras(self, device_id: str | None = None) -> list[dict]:
        with self.db.session() as s:
            q = select(Camera).order_by(Camera.device_id, Camera.channel_number)
            if device_id:
                q = q.where(Camera.device_id == device_id)
            return [self._cam(c) for c in s.scalars(q)]

    def camera_dict(self, camera_id: str) -> dict | None:
        with self.db.session() as s:
            c = s.get(Camera, camera_id)
            return self._cam(c) if c else None

    def _cam(self, c: Camera) -> dict:
        health = self.streams.health().get(c.id)
        return {
            "id": c.id, "device_id": c.device_id, "channel_number": c.channel_number, "name": c.name,
            "has_main_stream": c.stream_main is not None, "has_sub_stream": c.stream_sub is not None,
            "codec_main": c.codec_main, "codec_sub": c.codec_sub, "width": c.width, "height": c.height, "fps": c.fps,
            "guard_enabled": c.guard_enabled, "online": c.online, "compatibility": c.compatibility_status,
            "last_test": json.loads(c.last_test) if c.last_test else None,
            "tested_at": iso_utc(c.tested_at),
            "health": health,
        }

    def rename_camera(self, camera_id: str, name: str) -> dict:
        with self.db.session() as s:
            cam = s.get(Camera, camera_id)
            if not cam:
                raise GuardError("That camera is no longer in the list.")
            cam.name = name.strip()[:60] or cam.name
        self.db.audit("configuration_changed", camera_id, field="name")
        return self.camera_dict(camera_id)
