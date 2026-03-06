"""SSDP-based TV discovery with passive NOTIFY and one-shot startup M-SEARCH.

This module is part of the TV domain. It listens for SSDP NOTIFY packets
from the configured TV and uses them as the logical on/off truth source
(via TvController.notify_ssdp). On a positive discovery signal, it will also
attempt a best-effort websocket connect for fast command delivery.

On startup, a single targeted SSDP M-SEARCH is sent to bootstrap discovery
for the restart/crash-recovery case where passive multicast events may have
been missed while the app was down.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Iterable

from .controller import TvController

logger = logging.getLogger(__name__)

_MCAST_GRP = "239.255.255.250"
_MCAST_PORT = 1900
_MSEARCH_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"


def _parse_headers(txt: str) -> dict[str, str]:
    hdr: dict[str, str] = {}
    for line in txt.split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            hdr[k.strip().upper()] = v.strip()
    return hdr


async def ssdp_listener(tv: TvController) -> None:
    """Listen for SSDP NOTIFY from the configured TV IP and forward to tv.notify_ssdp()."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", _MCAST_PORT))
        mreq = socket.inet_aton(_MCAST_GRP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(True)

        while True:
            try:
                data, addr = await asyncio.to_thread(sock.recvfrom, 65535)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tv:ssdp listener error")
                await asyncio.sleep(1)
                continue

            src_ip = addr[0]
            if src_ip != tv.tv_ip:
                continue

            txt = data.decode("utf-8", errors="ignore")
            if "NOTIFY * HTTP/1.1" not in txt:
                continue

            hdr = _parse_headers(txt)
            nts = hdr.get("NTS", "")
            nt = hdr.get("NT", "")
            usn = hdr.get("USN", "")
            loc = hdr.get("LOCATION")

            acted = tv.notify_ssdp(nts=nts, nt=nt, usn=usn, location=loc, source="ssdp")
            if acted and nts == "ssdp:alive":
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    logger.debug("tv:ssdp ws connect failed", exc_info=True)
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def startup_msearch_probe(tv: TvController, *, timeout_s: float = 3.0) -> bool:
    """Send one targeted SSDP M-SEARCH and treat a matching reply as an alive event."""
    msg = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {_MCAST_GRP}:{_MCAST_PORT}",
            'MAN: "ssdp:discover"',
            "MX: 2",
            f"ST: {_MSEARCH_ST}",
            "",
            "",
        ]
    ).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.settimeout(0.5)
        sock.sendto(msg, (_MCAST_GRP, _MCAST_PORT))
        deadline = asyncio.get_running_loop().time() + max(0.1, float(timeout_s))

        while asyncio.get_running_loop().time() < deadline:
            try:
                data, addr = await asyncio.to_thread(sock.recvfrom, 65535)
            except asyncio.CancelledError:
                raise
            except socket.timeout:
                continue
            except Exception:
                logger.exception("tv:msearch probe error")
                return False

            src_ip = addr[0]
            if src_ip != tv.tv_ip:
                continue

            txt = data.decode("utf-8", errors="ignore")
            if "HTTP/1.1 200 OK" not in txt:
                continue

            hdr = _parse_headers(txt)
            st = hdr.get("ST", "")
            usn = hdr.get("USN", "")
            loc = hdr.get("LOCATION")
            server = hdr.get("SERVER", "")

            is_dmr = st == _MSEARCH_ST or (loc and "/dmr" in loc)
            is_samsung = "Samsung" in server
            if not (is_dmr and is_samsung):
                continue

            acted = tv.notify_ssdp(
                nts="ssdp:alive",
                nt=st,
                usn=usn,
                location=loc,
                source="msearch",
            )
            if acted:
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    logger.debug("tv:msearch ws connect failed", exc_info=True)
                return True

        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def start_discovery_tasks(tv: TvController, *, msearch_timeout_s: float = 3.0) -> list[asyncio.Task]:
    """Start SSDP listener and one-shot startup M-SEARCH. Returns the created tasks."""
    return [
        asyncio.create_task(ssdp_listener(tv), name="tv:ssdp"),
        asyncio.create_task(startup_msearch_probe(tv, timeout_s=msearch_timeout_s), name="tv:msearch"),
    ]


async def stop_discovery_tasks(tasks: Iterable[asyncio.Task]) -> None:
    """Cancel and await discovery tasks."""
    tasks = list(tasks)
    for t in tasks:
        if not t.done():
            t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("tv discovery task crashed during stop")
