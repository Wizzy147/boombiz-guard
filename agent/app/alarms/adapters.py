"""Alarm output adapters (Phase 3 §26–27, §32).

Every adapter answers available / activate(seconds) / deactivate and never
raises past `AlarmError` with a plain message. Credentials for DVR/camera
outputs come from the CCTV vault at call time — never from config, never
logged.

  PC_SOUND       the Guard PC's own speaker (works on every Windows PC)
  HIKVISION_IO   DVR/NVR/camera alarm output via ISAPI (relay or siren)
  DAHUA_IO       DVR/NVR/camera alarm output via CGI
  NETWORK_RELAY  an HTTP relay module on the shop LAN — private addresses
                 only, so a mis-typed URL can never make Guard call the internet
  USB_RELAY      common LCUS-type USB serial relay (A0 01 01 A2 / A0 01 00 A1)

Hikvision/Dahua/USB are verified against the lab simulator only (no
hardware yet). PC_SOUND and NETWORK_RELAY are real everywhere.
"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
import threading
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx

from ..adapters.base import Credentials


class AlarmError(Exception):
    pass


class AlarmAdapter(ABC):
    kind = "base"

    @abstractmethod
    async def available(self) -> bool: ...

    @abstractmethod
    async def activate(self, duration_seconds: int) -> None: ...

    @abstractmethod
    async def deactivate(self) -> None: ...


class PCSoundAdapter(AlarmAdapter):
    """Alternating two-tone siren on the PC speaker for `duration` seconds."""

    kind = "PC_SOUND"

    def __init__(self) -> None:
        self._stop = threading.Event()

    async def available(self) -> bool:
        return sys.platform == "win32"

    def _siren(self, seconds: int) -> None:
        import winsound  # Windows only

        end = __import__("time").monotonic() + seconds
        while not self._stop.is_set() and __import__("time").monotonic() < end:
            for f in (1200, 800):
                if self._stop.is_set():
                    break
                winsound.Beep(f, 250)

    async def activate(self, duration_seconds: int) -> None:
        if not await self.available():
            raise AlarmError("This computer can't play the Guard siren.")
        self._stop.clear()
        threading.Thread(target=self._siren, args=(duration_seconds,), daemon=True).start()

    async def deactivate(self) -> None:
        self._stop.set()


class _HttpDeviceAdapter(AlarmAdapter):
    def __init__(self, host: str, port: int | None, output: int, creds: Credentials | None) -> None:
        self.base = f"http://{host}:{port or 80}"
        self.output = output
        self.creds = creds

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:  # noqa: ANN003
        auth = httpx.DigestAuth(self.creds.username, self.creds.password) if self.creds else None
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                return await c.request(method, self.base + path, auth=auth, **kw)
        except httpx.HTTPError:
            raise AlarmError("The recorder's alarm output didn't respond.") from None

    async def _pulse(self, duration_seconds: int) -> None:
        await self._set(True)

        async def off() -> None:
            await asyncio.sleep(duration_seconds)
            try:
                await self._set(False)
            except AlarmError:
                pass

        asyncio.create_task(off())

    async def activate(self, duration_seconds: int) -> None:
        await self._pulse(duration_seconds)

    async def deactivate(self) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class HikvisionIOAdapter(_HttpDeviceAdapter):
    kind = "HIKVISION_IO"

    async def available(self) -> bool:
        r = await self._req("GET", f"/ISAPI/System/IO/outputs/{self.output}")
        return r.status_code == 200

    async def _set(self, on: bool) -> None:
        body = f'<IOPortData><outputState>{"high" if on else "low"}</outputState></IOPortData>'
        r = await self._req("PUT", f"/ISAPI/System/IO/outputs/{self.output}/trigger", content=body,
                            headers={"Content-Type": "application/xml"})
        if r.status_code != 200:
            raise AlarmError("The recorder refused to switch its alarm output.")


class DahuaIOAdapter(_HttpDeviceAdapter):
    kind = "DAHUA_IO"

    async def available(self) -> bool:
        r = await self._req("GET", "/cgi-bin/configManager.cgi?action=getConfig&name=AlarmOut")
        return r.status_code == 200

    async def _set(self, on: bool) -> None:
        idx = max(0, self.output - 1)
        r = await self._req("GET", f"/cgi-bin/configManager.cgi?action=setConfig&AlarmOut[{idx}].Mode={1 if on else 0}")
        if r.status_code != 200:
            raise AlarmError("The recorder refused to switch its alarm output.")


def lan_only(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise AlarmError("A relay address looks like http://192.168.1.50/relay/on")
    try:
        ip = ipaddress.ip_address(p.hostname)
    except ValueError:
        raise AlarmError("Use the relay's IP address, not a name.") from None
    if not (ip.is_private or ip.is_loopback or ip.is_link_local):
        raise AlarmError("Guard only switches relays on the shop network.")
    return url


class NetworkRelayAdapter(AlarmAdapter):
    kind = "NETWORK_RELAY"

    def __init__(self, on_url: str, off_url: str, status_url: str | None = None) -> None:
        self.on_url, self.off_url = lan_only(on_url), lan_only(off_url)
        self.status_url = lan_only(status_url) if status_url else None

    async def _get(self, url: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=4.0) as c:
                return await c.get(url)
        except httpx.HTTPError:
            raise AlarmError("The network relay didn't respond.") from None

    async def available(self) -> bool:
        try:
            r = await self._get(self.status_url or self.off_url)
            return r.status_code < 500
        except AlarmError:
            return False

    async def activate(self, duration_seconds: int) -> None:
        r = await self._get(self.on_url)
        if r.status_code >= 400:
            raise AlarmError("The network relay refused the command.")

        async def off() -> None:
            await asyncio.sleep(duration_seconds)
            try:
                await self._get(self.off_url)
            except AlarmError:
                pass

        asyncio.create_task(off())

    async def deactivate(self) -> None:
        await self._get(self.off_url)


class USBRelayAdapter(AlarmAdapter):
    """LCUS-1 style serial relay. UNTESTED ON HARDWARE."""

    kind = "USB_RELAY"
    ON, OFF = bytes([0xA0, 0x01, 0x01, 0xA2]), bytes([0xA0, 0x01, 0x00, 0xA1])

    def __init__(self, port: str, baud: int = 9600) -> None:
        self.port, self.baud = port, baud

    def _write(self, data: bytes) -> None:
        try:
            import serial

            with serial.Serial(self.port, self.baud, timeout=1) as s:
                s.write(data)
        except Exception:
            raise AlarmError(f"The USB relay on {self.port} isn't responding. Check it's plugged in.") from None

    async def available(self) -> bool:
        try:
            import serial.tools.list_ports as lp

            return any(p.device == self.port for p in lp.comports())
        except Exception:
            return False

    async def activate(self, duration_seconds: int) -> None:
        await asyncio.to_thread(self._write, self.ON)

        async def off() -> None:
            await asyncio.sleep(duration_seconds)
            try:
                await asyncio.to_thread(self._write, self.OFF)
            except AlarmError:
                pass

        asyncio.create_task(off())

    async def deactivate(self) -> None:
        await asyncio.to_thread(self._write, self.OFF)
