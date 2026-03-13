from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

async def pair_tv(*, tv_ip: str, token_file: str, name: str) -> dict:
    try:
        from samsungtvws import SamsungTVWS  # lazy import
    except Exception as e:
        logger.warning("samsungtvws not available: %r", e)
        return {"ok": False, "error": f"samsungtvws not available: {e!r}"}

    Path(token_file).parent.mkdir(parents=True, exist_ok=True)

    try:
        tv = SamsungTVWS(host=tv_ip, port=8002, token_file=token_file, name=name, timeout=5)
        tv.send_key("KEY_VOLUP")

        logger.info("pair triggered; accept the prompt on the TV. token_file=%s", token_file)
        return {"ok": True, "note": "If TV prompted, accept it. Token should now be saved."}
    except Exception as e:
        logger.warning("pair failed: %r (token_file=%s)", e, token_file)
        return {"ok": False, "error": repr(e)}