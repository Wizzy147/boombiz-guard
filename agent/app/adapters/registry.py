"""Which adapters exist, and in which order they're tried (PRD §9):

    ONVIF → manufacturer adapter (Hikvision, Dahua) → V380 path → generic RTSP

Brand identification only reorders this list to save time; it never removes
ONVIF from the front, so a portable route is always preferred.
"""

from __future__ import annotations

from .base import CCTVAdapter
from .dahua import DahuaAdapter
from .hikvision import HikvisionAdapter
from .onvif import OnvifAdapter
from .rtsp_generic import GenericRtspAdapter
from .v380 import V380Adapter


def build(
    *,
    onvif_endpoint: str | None = None,
    rtsp_port: int | None = None,
    manual_uri: str | None = None,
    prefer: str | None = None,
) -> list[CCTVAdapter]:
    port = rtsp_port or 554
    adapters: list[CCTVAdapter] = [
        OnvifAdapter(onvif_endpoint),
        HikvisionAdapter(port),
        DahuaAdapter(port),
        V380Adapter(port),
        GenericRtspAdapter(port, manual_uri),
    ]
    if manual_uri:
        # An installer-typed URL is the most specific thing we've been told.
        return [a for a in adapters if a.key == "rtsp"]
    if prefer and prefer != "auto":
        adapters.sort(key=lambda a: (a.key != prefer, a.priority))
    else:
        adapters.sort(key=lambda a: a.priority)
    return adapters


def by_key(key: str, **kw) -> CCTVAdapter | None:  # noqa: ANN003
    for a in build(**kw):
        if a.key == key:
            return a
    return None
