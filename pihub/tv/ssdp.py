"""SSDP-based TV discovery.

Runtime truth comes from passive SSDP NOTIFY packets. On startup, a single
M-SEARCH probe is sent to bootstrap state after app restart when the TV is
already on.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Iterable
from urllib.parse import urlparse

from .controller import TvController

logger = logging.getLogger(__name__)

_MCAST_GRP = "239.255.255.250"
_MCAST_PORT = 1900
_MSEARCH_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
_MSEARCH_BURST_COUNT = 3
_MSEARCH_BURST_GAP_S = 0.4


async def ssdp_listener(tv: TvController) -> None:
    """Listen for SSDP NOTIFY from the configured TV IP and forward to controller."""
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

            hdr: dict[str, str] = {}
            for line in txt.split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdr[k.strip().upper()] = v.strip()

            acted = tv.notify_ssdp(
                nts=hdr.get("NTS", ""),
                nt=hdr.get("NT", ""),
                usn=hdr.get("USN", ""),
                location=hdr.get("LOCATION"),
                source="ssdp",
            )
            if acted and hdr.get("NTS", "") == "ssdp:alive":
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    logger.debug("tv:ssdp ws connect failed after alive", exc_info=True)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _parse_headers(packet: str) -> dict[str, str]:
    hdr: dict[str, str] = {}
    for line in packet.split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            hdr[k.strip().upper()] = v.strip()
    return hdr


async def msearch_bootstrap(tv: TvController, *, timeout_s: float = 3.0) -> None:
    """Send a short targeted M-SEARCH burst and accept replies for the configured TV IP."""
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
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)

        logger.debug(
            "tv:msearch bootstrap start tv_ip=%s burst_count=%d timeout_s=%.1f",
            tv.tv_ip,
            _MSEARCH_BURST_COUNT,
            timeout_s,
        )

        for i in range(_MSEARCH_BURST_COUNT):
            try:
                await asyncio.to_thread(sock.sendto, msg, (_MCAST_GRP, _MCAST_PORT))
                logger.debug(
                    "tv:msearch probe sent tv_ip=%s seq=%d/%d",
                    tv.tv_ip,
                    i + 1,
                    _MSEARCH_BURST_COUNT,
                )
            except Exception:
                logger.exception("tv:msearch send failed seq=%d", i + 1)
                break

            if i + 1 < _MSEARCH_BURST_COUNT:
                await asyncio.sleep(_MSEARCH_BURST_GAP_S)

        deadline = asyncio.get_running_loop().time() + timeout_s

        while asyncio.get_running_loop().time() < deadline:
            try:
                data, addr = await asyncio.to_thread(sock.recvfrom, 65535)
            except socket.timeout:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tv:msearch bootstrap error")
                return

            reply_ip = addr[0]
            txt = data.decode("utf-8", errors="ignore")

            if "HTTP/1.1 200 OK" not in txt:
                logger.debug("tv:msearch ignored non-200 reply from=%s", reply_ip)
                continue

            hdr = _parse_headers(txt)
            st = hdr.get("ST")
            location = hdr.get("LOCATION")
            location_ip = urlparse(location).hostname if location else None

            logger.debug(
                "tv:msearch reply from=%s st=%s location=%s",
                reply_ip,
                st,
                location,
            )

            if st != _MSEARCH_ST:
                logger.debug(
                    "tv:msearch ignored reply from=%s reason=st_mismatch st=%s expected=%s",
                    reply_ip,
                    st,
                    _MSEARCH_ST,
                )
                continue

            if reply_ip != tv.tv_ip and location_ip != tv.tv_ip:
                logger.debug(
                    "tv:msearch ignored reply from=%s reason=ip_mismatch location_ip=%s expected=%s",
                    reply_ip,
                    location_ip,
                    tv.tv_ip,
                )
                continue

            acted = tv.notify_msearch(location=location)
            logger.debug(
                "tv:msearch matched reply from=%s location=%s acted=%s",
                reply_ip,
                location,
                "true" if acted else "false",
            )

            if acted:
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    logger.debug("tv:msearch ws connect failed after bootstrap", exc_info=True)
            return

        logger.debug("tv:msearch bootstrap complete tv_ip=%s acted=false reason=timeout", tv.tv_ip)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def start_discovery_tasks(tv: TvController) -> list[asyncio.Task]:
    """Start passive SSDP listener plus one-shot M-SEARCH bootstrap."""
    return [
        asyncio.create_task(ssdp_listener(tv), name="tv:ssdp"),
        asyncio.create_task(msearch_bootstrap(tv), name="tv:msearch_bootstrap"),
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
