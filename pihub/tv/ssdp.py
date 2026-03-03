"""SSDP-based TV power state discovery (push) + slow DMR fallback (pull).

This module is part of the TV domain. It listens for SSDP NOTIFY packets
from the configured TV and uses them as the logical on/off truth source
(via TvController.notify_ssdp). On ssdp:alive, it will also attempt a
best-effort websocket connect for fast command delivery.

A very slow DMR poller (default 60s) is provided as a safety net in case
multicast packets are dropped.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Iterable

from .dmr import dmr_up
from .controller import TvController

logger = logging.getLogger(__name__)

_MCAST_GRP = "239.255.255.250"
_MCAST_PORT = 1900


async def ssdp_listener(tv: TvController) -> None:
    """Listen for SSDP NOTIFY from the TV IP and forward to tv.notify_ssdp()."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", _MCAST_PORT))
        mreq = socket.inet_aton(_MCAST_GRP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        # IMPORTANT: blocking socket when using asyncio.to_thread(sock.recvfrom,...)
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

            hdr: dict[str, str] = {}
            for line in txt.split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdr[k.strip().upper()] = v.strip()

            nts = hdr.get("NTS", "")
            nt = hdr.get("NT", "")
            usn = hdr.get("USN", "")
            loc = hdr.get("LOCATION")

            acted = tv.notify_ssdp(nts=nts, nt=nt, usn=usn, location=loc, source="ssdp")
            if not acted:
                continue

            # Requirement: on positive SSDP (alive), attempt WS connect immediately.
            if nts == "ssdp:alive":
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def dmr_fallback_poller(tv: TvController, *, interval_s: float = 60.0) -> None:
    """Very slow DMR poll to reconcile logical power state if SSDP is missed."""
    while True:
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise

        if not getattr(tv, "_session", None):
            continue

        try:
            sample = await dmr_up(tv._session, tv.tv_ip)  # True/False
        except Exception:
            continue

        cached = tv._dmr_cached
        if cached is None:
            tv.set_power_state(bool(sample), reason="fallback_init", source="fallback")
            if sample:
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    pass
            continue

        if bool(sample) != bool(cached):
            tv.set_power_state(bool(sample), reason="fallback_reconcile", source="fallback")
            if sample:
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    pass


def start_discovery_tasks(tv: TvController, *, interval_s: float = 60.0) -> list[asyncio.Task]:
    """Start SSDP listener + slow DMR fallback tasks. Returns the created tasks."""
    return [
        asyncio.create_task(ssdp_listener(tv), name="tv:ssdp"),
        asyncio.create_task(dmr_fallback_poller(tv, interval_s=interval_s), name="tv:dmr_fallback"),
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
