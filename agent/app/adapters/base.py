"""The contract every CCTV integration implements (Phase 1 §7).

Guard never branches on a brand in its main code. Discovery, the API and the
stream manager only ever talk to a `CCTVAdapter`; Hikvision, Dahua, V380 and
generic RTSP differ only inside their own adapter. Adding a manufacturer is
a new file here plus one line in adapters/registry.py.

Adapters are stateless about credentials: they are handed a username and
password for the call and must not keep, log, or return them. Stream URIs
they return are credential-FREE — the stream manager injects credentials at
the moment it opens FFmpeg, and nowhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum


class DeviceType(StrEnum):
    CAMERA = "CAMERA"
    DVR = "DVR"
    NVR = "NVR"
    UNKNOWN = "UNKNOWN"


class Compatibility(StrEnum):
    UNKNOWN = "UNKNOWN"
    COMPATIBLE = "COMPATIBLE"
    LIMITED = "LIMITED"
    INCOMPATIBLE = "INCOMPATIBLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    OFFLINE = "OFFLINE"


class AuthResult(StrEnum):
    OK = "OK"
    BAD_CREDENTIALS = "BAD_CREDENTIALS"
    LOCKED = "LOCKED"  # the device says too many attempts — we stop, never retry
    UNREACHABLE = "UNREACHABLE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class DeviceInfo:
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    device_type: DeviceType = DeviceType.UNKNOWN
    channel_count: int | None = None
    onvif: bool = False
    rtsp: bool = False
    audio: bool = False
    alarm_output: bool = False
    ptz: bool = False


@dataclass
class StreamProfile:
    """One encoder profile on one channel. `uri` carries NO credentials."""

    token: str
    name: str
    uri: str
    codec: str | None = None  # H264 | H265 | MJPEG | …
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    is_main: bool = False


@dataclass
class Channel:
    number: int
    name: str
    online: bool = True
    profiles: list[StreamProfile] = field(default_factory=list)


@dataclass
class Credentials:
    username: str
    password: str = field(repr=False)  # never in a repr, so never in a log line


class CCTVAdapter(ABC):
    """One integration route to a device. See module docstring for the rules."""

    #: Stable key stored in devices.adapter_type.
    key: str = "base"
    #: Lower runs first when several adapters claim a device (Phase 1 §9 order).
    priority: int = 100

    @abstractmethod
    async def probe(self, host: str, port: int | None = None) -> bool:
        """Cheap, unauthenticated: does this device look like ours to handle?"""

    @abstractmethod
    async def authenticate(self, host: str, port: int | None, creds: Credentials) -> AuthResult:
        """ONE attempt with the credentials given. Never loops, never guesses."""

    @abstractmethod
    async def get_device_info(self, host: str, port: int | None, creds: Credentials) -> DeviceInfo: ...

    @abstractmethod
    async def list_channels(self, host: str, port: int | None, creds: Credentials) -> list[Channel]: ...

    async def get_stream_profiles(
        self, host: str, port: int | None, creds: Credentials, channel: int
    ) -> list[StreamProfile]:
        for ch in await self.list_channels(host, port, creds):
            if ch.number == channel:
                return ch.profiles
        return []
