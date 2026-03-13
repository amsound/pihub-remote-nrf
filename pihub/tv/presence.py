from __future__ import annotations

import asyncio

import aiohttp


async def presence_probe_up(
    session: aiohttp.ClientSession,
    tv_ip: str,
    timeout_s: float = 1.5,
) -> bool:
    """
    Lightweight HTTP fallback probe for Samsung's renderer endpoint.

    This is *not* the primary source of truth for logical presence.
    SSDP alive/byebye plus startup M-SEARCH bootstrap remain the preferred signals.
    """
    url = f"http://{tv_ip}:9197/dmr"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    for _ in range(2):
        try:
            async with session.get(url, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            await asyncio.sleep(0.05)
    return False