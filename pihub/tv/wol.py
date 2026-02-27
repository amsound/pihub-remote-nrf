from __future__ import annotations
import socket
import re

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

def send_wol(mac: str, *, port: int = 9, broadcast: str = "255.255.255.255") -> None:
    """
    Send a magic packet. Works well with host networking in Docker.
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