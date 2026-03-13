from __future__ import annotations

import asyncio
import re
import socket

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def send_wol(mac: str, *, port: int = 9, broadcast: str = "255.255.255.255") -> None:
    """
    Send a single Wake-on-LAN magic packet.
    """
    mac = mac.strip()
    if not _MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC: {mac}")

    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))
    finally:
        s.close()


async def send_wol_burst(
    mac: str,
    *,
    count: int = 3,
    gap_s: float = 0.25,
    port: int = 9,
    broadcast: str = "255.255.255.255",
) -> None:
    """
    Send a short async burst of WOL packets.

    Keep the burst itself small and deterministic; controller.py decides
    whether to schedule another burst later.
    """
    for idx in range(count):
        send_wol(mac, port=port, broadcast=broadcast)
        if idx + 1 < count:
            await asyncio.sleep(gap_s)