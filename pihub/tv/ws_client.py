from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

def _b64_name(name: str) -> str:
    return base64.b64encode(name.encode("utf-8")).decode("ascii")

@dataclass
class TvWsState:
    connected: bool = False
    last_error: str = ""
    token_present: bool = False

class TvWsClient:
    """
    Maintains a websocket connection while TV is on.
    Sends KEY_* messages quickly with no reconnect per key.
    """
    def __init__(self, *, tv_ip: str, token_file: str, name: str) -> None:
        self._tv_ip = tv_ip
        self._token_file = token_file
        self._name = name
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._lock = asyncio.Lock()
        self.state = TvWsState()

    def _read_token(self) -> str:
        try:
            tok = open(self._token_file, "r", encoding="utf-8").read().strip()
            self.state.token_present = bool(tok)
            return tok
        except FileNotFoundError:
            self.state.token_present = False
            return ""
        except Exception:
            self.state.token_present = False
            return ""

    def _ws_url(self) -> str:
        # This matches the pattern you saw in samsungtvws logs.
        # Token is appended if present. Name must be base64.
        name_b64 = _b64_name(self._name)
        token = self._read_token()
        base = f"wss://{self._tv_ip}:8002/api/v2/channels/samsung.remote.control?name={name_b64}"
        if token:
            base += f"&token={token}"
        return base

    async def connect(self, session: aiohttp.ClientSession, *, timeout_s: float = 2.0) -> bool:
        async with self._lock:
            if self._ws and not self._ws.closed:
                return True

            url = self._ws_url()
            try:
                self._ws = await session.ws_connect(
                    url,
                    heartbeat=30,
                    autoping=True,
                    ssl=False,  # Samsung uses self-signed/odd certs on IP
                    timeout=timeout_s,
                )
                self.state.connected = True
                self.state.last_error = ""
                logger.debug("[tvws] connected")
                return True
            except Exception as e:
                self.state.connected = False
                self.state.last_error = repr(e)
                logger.debug("[tvws] connect failed: %r", e)
                self._ws = None
                return False

    async def close(self) -> None:
        async with self._lock:
            ws, self._ws = self._ws, None
            self.state.connected = False
            if ws and not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    pass
            logger.debug("[tvws] closed")

    async def send_key(self, key: str) -> bool:
        payload = {
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Click",
                "DataOfCmd": key,
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey",
            },
        }

        async with self._lock:
            ws = self._ws
            if ws is None or ws.closed:
                self.state.connected = False
                return False
            try:
                await ws.send_str(json.dumps(payload))
                return True
            except Exception as e:
                self.state.connected = False
                self.state.last_error = repr(e)
                return False