from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

async def pair_tv(*, tv_ip: str, token_file: str, name: str) -> dict:
    """
    One-time pairing bootstrap.
    Triggers TV 'Allow' prompt and saves token_file.
    Uses samsungtvws only for this bootstrap to avoid reimplementing auth quirks.
    """
    try:
        from samsungtvws import SamsungTVWS  # lazy import
    except Exception as e:
        return {"ok": False, "error": f"samsungtvws not available: {e!r}"}

    Path(token_file).parent.mkdir(parents=True, exist_ok=True)

    try:
        tv = SamsungTVWS(host=tv_ip, port=8002, token_file=token_file, name=name, timeout=5)
        # Any simple key triggers the auth prompt if not paired.
        tv.send_key("KEY_VOLUP")
        return {"ok": True, "note": "If TV prompted, accept it. Token should now be saved."}
    except Exception as e:
        logger.warning("[tv] pair failed: %r", e)
        return {"ok": False, "error": repr(e)}