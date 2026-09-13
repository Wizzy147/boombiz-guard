"""Scan Existing CCTV (PRD §6, Phase 1 §8–10).

Stage 1  ONVIF WS-Discovery (standards probe).
Stage 2  A polite TCP connect check of the local /24 on CCTV ports ONLY
         (RTSP 554/8554/10554, web 80/8000, V380 8800). No banner grabbing
         beyond what identification needs, no other ports, no credentials.
Stage 3  Fingerprint each candidate with the adapters' unauthenticated
         probe() — which brand API answers — and read the MAC from the ARP
         cache for dedupe and a vendor hint.

What this deliberately does NOT do (Phase 1 §9): exploit anything, try a
single password, or touch a service that isn't a CCTV one.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field

import psutil

from ..adapters.dahua import DahuaAdapter
from ..adapters.hikvision import HikvisionAdapter
from ..adapters.v380 import V380_P2P_PORT
from ..streams.rtsp_probe import rtsp_options
from . import ws_discovery

log = logging.getLogger(__name__)

CCTV_PORTS = (554, 8554, 10554, 80, 8000, V380_P2P_PORT)

# OUI prefixes for the two Tier-1 brands. A hint only — never the verdict.
OUI_VENDOR = {
    "C0:56:E3": "Hikvision", "44:19:B6": "Hikvision", "BC:AD:28": "Hikvision",
    "4C:BD:8F": "Hikvision", "28:57:BE": "Hikvision", "54:C4:15": "Hikvision",
    "3C:EF:8C": "Dahua", "90:02:A9": "Dahua", "E0:50:8B": "Dahua",
    "4C:11:BF": "Dahua", "38:AF:29": "Dahua", "A0:BD:1D": "Dahua",
}


@dataclass
class Candidate:
    ip: str
    open_ports: set[int] = field(default_factory=set)
    onvif: ws_discovery.DiscoveredOnvif | None = None
    mac: str | None = None
    mac_vendor: str | None = None
    brand: str | None = None  # from adapter probe
    adapter_hint: str | None = None
    rtsp_port: int | None = None
    http_port: int | None = None

    @property
    def is_cctv(self) -> bool:
        return bool(self.onvif or self.rtsp_port or self.brand or V380_P2P_PORT in self.open_ports)


async def _open(ip: str, port: int, timeout: float) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def local_subnets() -> list[ipaddress.IPv4Network]:
    nets = []
    for name, addrs in psutil.net_if_addrs().items():
        stats = psutil.net_if_stats().get(name)
        if stats and not stats.isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET or a.address.startswith(("127.", "169.254.")):
                continue
            # Only ever a /24 around our own address, however big the real
            # netmask is: a shop LAN is small, and a /16 sweep is not polite.
            nets.append(ipaddress.ip_network(f"{a.address}/24", strict=False))
    return list(dict.fromkeys(nets))


def arp_table() -> dict[str, str]:
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table = {}
    for ip, mac in re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", out):
        table[ip] = mac.upper().replace("-", ":")
    return table


class Scanner:
    def __init__(self, timeout: float = 4.0, connect_timeout: float = 0.6, concurrency: int = 128) -> None:
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.sem = asyncio.Semaphore(concurrency)
        self.progress: dict = {"stage": "idle", "checked": 0, "total": 0, "found": 0}
        # Lab / manual extras. GUARD_SCAN_EXTRA_HOSTS="127.0.0.2,127.0.0.3",
        # GUARD_DISCOVERY_UNICAST="127.0.0.1:3702".
        self.extra_hosts = [h for h in os.environ.get("GUARD_SCAN_EXTRA_HOSTS", "").split(",") if h.strip()]
        uni = os.environ.get("GUARD_DISCOVERY_UNICAST")
        self.unicast = (uni.split(":")[0], int(uni.split(":")[1])) if uni else None
        # GUARD_SCAN_LAN=0 skips the /24 sweep (the simulated lab uses it so a
        # test run never touches the developer's real network).
        self.sweep_lan = os.environ.get("GUARD_SCAN_LAN", "1") != "0"
        self.ports_override = {
            int(p) for p in os.environ.get("GUARD_SCAN_PORTS", "").split(",") if p.strip().isdigit()
        }

    async def _check_host(self, ip: str, cands: dict[str, Candidate]) -> None:
        ports = tuple(self.ports_override) or CCTV_PORTS
        async with self.sem:
            results = await asyncio.gather(*(_open(ip, p, self.connect_timeout) for p in ports))
        self.progress["checked"] += 1
        open_ports = {p for p, ok in zip(ports, results) if ok}
        if open_ports:
            cands.setdefault(ip, Candidate(ip)).open_ports |= open_ports

    async def scan(self) -> list[Candidate]:
        cands: dict[str, Candidate] = {}

        self.progress.update(stage="onvif", checked=0, total=0, found=0)
        for m in await ws_discovery.probe(self.timeout, self.unicast):
            cands.setdefault(m.ip, Candidate(m.ip)).onvif = m

        self.progress["stage"] = "network"
        hosts = {str(h) for net in local_subnets() for h in net.hosts()} if self.sweep_lan else set()
        hosts |= set(self.extra_hosts) | set(cands)
        self.progress["total"] = len(hosts)
        await asyncio.gather(*(self._check_host(ip, cands) for ip in hosts))

        self.progress["stage"] = "identify"
        arp = arp_table()
        hik = HikvisionAdapter()
        dahua = DahuaAdapter()

        async def identify(c: Candidate) -> None:
            c.mac = arp.get(c.ip)
            if c.mac:
                c.mac_vendor = OUI_VENDOR.get(c.mac[:8])
            for p in (554, 8554, 10554):
                if p in c.open_ports and await rtsp_options(c.ip, p):
                    c.rtsp_port = p
                    break
            for p in (80, 8000):
                if p in c.open_ports:
                    c.http_port = p
                    if await hik.probe(c.ip, p):
                        c.brand, c.adapter_hint = "Hikvision", "hikvision"
                    elif await dahua.probe(c.ip, p):
                        c.brand, c.adapter_hint = "Dahua", "dahua"
                    if c.brand:
                        break
            if not c.brand and V380_P2P_PORT in c.open_ports:
                c.brand, c.adapter_hint = "V380", "v380"
            if c.onvif:
                c.adapter_hint = "onvif"

        await asyncio.gather(*(identify(c) for c in cands.values()))
        found = [c for c in cands.values() if c.is_cctv]
        self.progress.update(stage="done", found=len(found))
        return found
