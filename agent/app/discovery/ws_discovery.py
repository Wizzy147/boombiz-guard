"""ONVIF WS-Discovery (Phase 1 §8) — a standards Probe, nothing more.

Sends one Probe for NetworkVideoTransmitter / Device to the WS-Discovery
multicast group on every local IPv4 interface and collects ProbeMatches until
the timeout. This is what the ONVIF spec asks a client to do; it is not a
port scan and touches no device that doesn't answer it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import psutil

log = logging.getLogger(__name__)

MCAST_GROUP = "239.255.255.250"
MCAST_PORT = 3702

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
 <e:Header>
  <w:MessageID>uuid:{mid}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>{types}</d:Types></d:Probe></e:Body>
</e:Envelope>"""

# Recorders often answer only to Device, cameras only to NetworkVideoTransmitter.
PROBE_TYPES = ("dn:NetworkVideoTransmitter", "tds:Device")


@dataclass
class DiscoveredOnvif:
    ip: str
    xaddrs: list[str]
    uuid: str | None = None
    scopes: list[str] = field(default_factory=list)

    @property
    def endpoint(self) -> str | None:
        # Prefer an address on the same IP that answered (multi-homed NVRs list several).
        for x in self.xaddrs:
            if urlparse(x).hostname == self.ip:
                return x
        return self.xaddrs[0] if self.xaddrs else None

    def scope(self, key: str) -> str | None:
        """onvif://www.onvif.org/name/Foo → scope('name') == 'Foo'."""
        prefix = f"onvif://www.onvif.org/{key}/"
        for s in self.scopes:
            if s.lower().startswith(prefix):
                return unquote(s[len(prefix):]) or None
        return None

    @property
    def manufacturer_hint(self) -> str | None:
        return self.scope("mfr") or self.scope("manufacturer") or self.scope("hardware")


def _local_ipv4() -> list[str]:
    ips: list[str] = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                ips.append(a.address)
    return ips or ["0.0.0.0"]


def parse_probe_match(data: bytes, sender_ip: str) -> DiscoveredOnvif | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    xaddrs: list[str] = []
    scopes: list[str] = []
    dev_uuid = None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "XAddrs" and el.text:
            xaddrs += el.text.split()
        elif tag == "Scopes" and el.text:
            scopes += el.text.split()
        elif tag == "Address" and el.text and dev_uuid is None and "uuid" in el.text:
            dev_uuid = el.text.strip()
    if not xaddrs:
        return None
    return DiscoveredOnvif(ip=sender_ip, xaddrs=xaddrs, uuid=dev_uuid, scopes=scopes)


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, sink: dict[str, DiscoveredOnvif]) -> None:
        self.sink = sink

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        match = parse_probe_match(data, addr[0])
        if match:
            key = match.uuid or match.ip
            self.sink.setdefault(key, match)


async def probe(timeout: float = 4.0, target: tuple[str, int] | None = None) -> list[DiscoveredOnvif]:
    """Multicast (or, with `target`, unicast — used by the lab) WS-Discovery probe."""
    loop = asyncio.get_running_loop()
    found: dict[str, DiscoveredOnvif] = {}
    transports = []
    for ip in (["127.0.0.1"] if target else _local_ipv4()):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if ip != "0.0.0.0":
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sock.bind((ip, 0))
            sock.setblocking(False)
            transport, _ = await loop.create_datagram_endpoint(lambda: _Collector(found), sock=sock)
            transports.append(transport)
            for types in PROBE_TYPES:
                msg = PROBE.format(mid=uuid.uuid4(), types=types).encode()
                transport.sendto(msg, target or (MCAST_GROUP, MCAST_PORT))
        except OSError as e:
            log.info("WS-Discovery skipped interface %s: %s", ip, e)
    await asyncio.sleep(timeout)
    for t in transports:
        t.close()
    return list(found.values())
