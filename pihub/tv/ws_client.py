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
        self._rx_task: Optional[asyncio.Task] = None

        # Only log transitions
        self._logged_connected: Optional[bool] = None
        self._logged_token_present: Optional[bool] = None

        # Emit initial token presence once at startup
        self._refresh_token_present()

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

    def _refresh_token_present(self) -> None:
        tok = self._read_token()
        token_present = bool(tok)
        if self._logged_token_present is None or token_present != self._logged_token_present:
            self._logged_token_present = token_present
            logger.info(
                "initialised tv websocket tv_ip=%s token_present=%s",
                self._tv_ip,
                "true" if token_present else "false",
            )

    def _write_token(self, token: str) -> None:
        token = token.strip()
        if not token:
            return
        try:
            os.makedirs(os.path.dirname(self._token_file) or ".", exist_ok=True)
            with open(self._token_file, "w", encoding="utf-8") as f:
                f.write(token + "\n")
            self.state.token_present = True
            self._logged_token_present = True
            logger.info("token saved to %s", self._token_file)
        except Exception as e:
            self.state.last_error = repr(e)
            logger.exception("failed to save token to %s: %r", self._token_file, e)

    def _ws_url(self) -> str:
        # This matches the pattern you saw in samsungtvws logs.
        # Token is appended if present. Name must be base64.
        name_b64 = _b64_name(self._name)
        token = self._read_token()
        base = f"wss://{self._tv_ip}:8002/api/v2/channels/samsung.remote.control?name={name_b64}"
        if token:
            base += f"&token={token}"
        return base

    async def _rx_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                try:
                    j = json.loads(msg.data)
                except Exception:
                    continue

                data = (j or {}).get("data")
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = None

                if isinstance(data, dict):
                    tok = data.get("token")
                    if isinstance(tok, str) and tok.strip():
                        self._write_token(tok)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("rx loop ended: %r", e)

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
                    ssl=False,
                    timeout=timeout_s,
                )
                self.state.connected = True
                self.state.last_error = ""

                # Log transition to connected (once)
                if self._logged_connected is None or self._logged_connected is False:
                    self._logged_connected = True
                    logger.info("websocket connected")

                # ensure only one rx task exists
                if self._rx_task and not self._rx_task.done():
                    self._rx_task.cancel()

                ws = self._ws
                self._rx_task = asyncio.create_task(self._rx_loop(ws), name="tvws_rx")
                return True

            except Exception as e:
                self.state.connected = False
                self.state.last_error = repr(e)

                # Only log disconnect transition if we thought we were connected before
                if self._logged_connected is True:
                    self._logged_connected = False
                    logger.info("websocket disconnected")

                logger.debug("connect failed: %r", e)
                self._ws = None
                return False

    async def close(self) -> None:
        async with self._lock:
            task, self._rx_task = self._rx_task, None
            if task and not task.done():
                task.cancel()

            ws, self._ws = self._ws, None
            self.state.connected = False

            if self._logged_connected is True:
                self._logged_connected = False
                logger.info("websocket disconnected")

        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass

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