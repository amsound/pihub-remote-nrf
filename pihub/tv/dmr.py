import aiohttp
import asyncio

async def dmr_up(session: aiohttp.ClientSession, tv_ip: str, timeout_s: float = 1.5) -> bool:
    url = f"http://{tv_ip}:9197/dmr"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    for _ in range(2):  # try twice
        try:
            async with session.get(url, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            await asyncio.sleep(0.05)
    return False