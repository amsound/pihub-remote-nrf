from __future__ import annotations
import aiohttp

async def dmr_up(session: aiohttp.ClientSession, tv_ip: str, timeout_s: float = 1.0) -> bool:
    # True iff TV answers DMR endpoint (your proven power truth signal)
    url = f"http://{tv_ip}:9197/dmr"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False