"""Samsung Frame TV control via Samsung IP Control G2.

This backend talks to the newer Samsung IP Remote surface exposed on
https://<tv>:1516.  It is deliberately narrow: discreet power, HDMI 1 source
selection, return/dismiss, and gentle power/source reconciliation.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class TvFrameSnapshot:
    initialised: bool
    presence_on: bool | None
    presence_source: str
    last_change_age_s: int | None
    ws_connected: bool
    token_present: bool
    last_error: str
    backend: str = "samsung_frame_ip"
    power: str | None = None
    input_source: str | None = None


class SamsungFrameTv:
    """Narrow Samsung Frame IP-control backend.

    The 1516 control plane uses HTTPS JSON-RPC, not Samsung's regular
    websocket.  If the access token file is missing, the backend can request
    one with createAccessToken and persist it once the TV authorisation prompt
    is accepted.
    """

    def __init__(
        self,
        *,
        tv_ip: str,
        token_file: str,
        state_change_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.tv_ip = tv_ip
        self.token_file = token_file
        self._session: Optional[aiohttp.ClientSession] = None
        self._rpc_ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._state_change_callback = state_change_callback

        self._presence_cached: bool | None = None
        self._presence_source = "unknown"
        self._presence_last_change_ts: float | None = None
        self._power: str | None = None
        self._input_source: str | None = None
        self._last_error = ""
        self._initial_status_logged = False
        self._token_request_logged = False

        logger.info(
            "initialised frame tv tv_ip=%s token_present=%s",
            tv_ip,
            "true" if self._read_token() else "false",
        )

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=8)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self) -> None:
        sess, self._session = self._session, None
        if sess:
            await sess.close()

    def _read_token(self) -> str:
        try:
            return open(self.token_file, "r", encoding="utf-8").read().strip()
        except FileNotFoundError:
            return ""
        except Exception:
            logger.debug("failed to read frame tv token", exc_info=True)
            return ""

    def _write_token(self, token: str) -> None:
        token = (token or "").strip()
        if not token:
            return
        os.makedirs(os.path.dirname(self.token_file) or ".", exist_ok=True)
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write(token + "\n")

    def _commit_power(self, power: str | None, *, source: str) -> None:
        power = (power or "").strip() or None
        previous_on = self._presence_cached is True
        self._power = power

        if power == "powerOn":
            next_on: bool | None = True
        elif power == "powerOff":
            next_on = False
        else:
            next_on = self._presence_cached

        if self._presence_cached is not next_on:
            self._presence_cached = next_on
            self._presence_source = source
            self._presence_last_change_ts = asyncio.get_running_loop().time()

            if not previous_on and next_on is True:
                self._emit_state_change(
                    "watch",
                    {
                        "domain": "tv",
                        "presence_source": source,
                    },
                )

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        cb = self._state_change_callback
        if cb is None:
            return

        async def _run() -> None:
            await cb(name, payload)

        task = asyncio.create_task(_run(), name=f"frame_tv_state_change:{name}")

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug("frame tv state change callback cancelled name=%s", name)
            except Exception:
                logger.exception("frame tv state change callback failed name=%s", name)

        task.add_done_callback(_done)

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        include_token: bool = True,
        timeout_s: float = 8.0,
    ) -> dict[str, Any]:
        if not self._session:
            await self.start()
        if not self._session:
            raise RuntimeError("frame_tv_session_unavailable")

        payload_params = dict(params or {})
        if include_token:
            token = self._read_token()
            if not token:
                raise RuntimeError("frame_tv_token_missing")
            payload_params.setdefault("AccessToken", token)

        request_id = str(next(self._rpc_ids))
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
        }
        if payload_params or include_token:
            payload["params"] = payload_params

        url = f"https://{self.tv_ip}:1516"
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        try:
            async with self._session.post(
                url,
                json=payload,
                headers={"Accept": "application/json"},
                ssl=False,
                timeout=timeout,
            ) as resp:
                data = await resp.json(content_type=None)
        except Exception as exc:
            self._last_error = repr(exc)
            raise

        if not isinstance(data, dict):
            self._last_error = "invalid_jsonrpc_response"
            raise RuntimeError("invalid_jsonrpc_response")

        if "error" in data:
            self._last_error = str(data.get("error"))
            raise RuntimeError(f"frame_tv_rpc_error:{data.get('error')}")

        self._last_error = ""
        return data

    async def _ensure_token(self) -> bool:
        if self._read_token():
            return True

        if not self._token_request_logged:
            self._token_request_logged = True
            logger.info(
                "frame tv token missing; requesting access token tv_ip=%s token_file=%s "
                "accept the prompt on the TV if shown",
                self.tv_ip,
                self.token_file,
            )

        try:
            await self._request_access_token_unlocked()
            return True
        except Exception as exc:
            self._last_error = repr(exc)
            logger.info(
                "frame tv access token not available yet tv_ip=%s token_file=%s error=%r",
                self.tv_ip,
                self.token_file,
                exc,
            )
            return False

    async def _request_access_token_unlocked(self) -> str:
        data = await self._rpc("createAccessToken", include_token=False)
        result = data.get("result") or {}
        token = result.get("AccessToken") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("frame_tv_access_token_missing")
        self._write_token(token)
        self._last_error = ""
        logger.info("frame tv access token saved to %s", self.token_file)
        return token.strip()

    async def request_access_token(self) -> str:
        """Request and persist a new access token.

        This can display an on-screen authorization prompt on the TV.
        """
        async with self._lock:
            return await self._request_access_token_unlocked()

    async def _get_power(self) -> str | None:
        data = await self._rpc("powerControl")
        result = data.get("result") or {}
        power = result.get("power") if isinstance(result, dict) else None
        return power if isinstance(power, str) else None

    async def _get_input_source(self) -> str | None:
        data = await self._rpc("inputSourceControl")
        result = data.get("result") or {}
        source = result.get("inputSource") if isinstance(result, dict) else None
        return source if isinstance(source, str) else None

    async def reconcile_presence(self, *, bootstrap_timeout_s: float = 3.0) -> dict[str, Any]:
        del bootstrap_timeout_s
        if not self._session:
            await self.start()

        errors: list[str] = []
        before_presence = self._presence_cached
        before_source = self._input_source

        async with self._lock:
            if not await self._ensure_token():
                errors.append("token:frame_tv_token_missing")
            else:
                try:
                    power = await self._get_power()
                    self._commit_power(power, source="ip_control_power")
                except Exception as exc:
                    logger.debug("frame tv power reconcile failed", exc_info=True)
                    errors.append(f"power:{exc!r}")

                if self._presence_cached is True:
                    try:
                        self._input_source = await self._get_input_source()
                    except Exception as exc:
                        logger.debug("frame tv source reconcile failed", exc_info=True)
                        errors.append(f"source:{exc!r}")

        changed = before_presence != self._presence_cached or before_source != self._input_source
        outcome = (
            "present_true" if self._presence_cached is True
            else "present_false" if self._presence_cached is False
            else "unknown"
        )

        if not self._initial_status_logged and not errors:
            self._initial_status_logged = True
            logger.info(
                "frame tv initial status power=%s input_source=%s presence_on=%s",
                self._power,
                self._input_source,
                "true" if self._presence_cached is True else "false" if self._presence_cached is False else "unknown",
            )

        return {
            "outcome": outcome,
            "presence_on": self._presence_cached,
            "presence_source": self._presence_source,
            "power": self._power,
            "input_source": self._input_source,
            "changed": changed,
            "errors": errors,
        }

    def snapshot(self) -> TvFrameSnapshot:
        last_change_age_s: int | None = None
        if self._presence_last_change_ts is not None:
            last_change_age_s = int(asyncio.get_running_loop().time() - self._presence_last_change_ts)

        token_present = bool(self._read_token())
        return TvFrameSnapshot(
            initialised=self._presence_cached is not None,
            presence_on=self._presence_cached,
            presence_source=self._presence_source,
            last_change_age_s=last_change_age_s,
            ws_connected=False,
            token_present=token_present,
            last_error=self._last_error,
            power=self._power,
            input_source=self._input_source,
        )

    async def power_on(self, *, timeout_s: float = 8.0) -> bool:
        async with self._lock:
            try:
                if not await self._ensure_token():
                    return False
                data = await self._rpc(
                    "powerControl",
                    {"power": "powerOn"},
                    timeout_s=timeout_s,
                )
                result = data.get("result") or {}
                self._commit_power(
                    result.get("power") if isinstance(result, dict) else "powerOn",
                    source="ip_control_power_on",
                )
                return True
            except Exception:
                logger.debug("frame tv power_on failed", exc_info=True)
                return False

    async def power_off(self, *, wait: bool = True, timeout_s: float = 8.0) -> bool:
        del wait
        async with self._lock:
            try:
                if not await self._ensure_token():
                    return False
                data = await self._rpc(
                    "powerControl",
                    {"power": "powerOff"},
                    timeout_s=timeout_s,
                )
                result = data.get("result") or {}
                self._commit_power(
                    result.get("power") if isinstance(result, dict) else "powerOff",
                    source="ip_control_power_off",
                )
                return True
            except Exception:
                logger.debug("frame tv power_off failed", exc_info=True)
                return False

    async def hdmi1(self) -> bool:
        return await self.set_hdmi1()

    async def set_hdmi1(self) -> bool:
        async with self._lock:
            try:
                if not await self._ensure_token():
                    return False
                data = await self._rpc(
                    "inputSourceControl",
                    {"inputSource": "HDMI1"},
                )
                result = data.get("result") or {}
                if isinstance(result, dict):
                    source = result.get("inputSource")
                    if isinstance(source, str):
                        self._input_source = source
                return True
            except Exception:
                logger.debug("frame tv set HDMI1 failed", exc_info=True)
                return False

    async def return_key(self) -> bool:
        return await self.press(key="return")

    async def press(self, *, key: str) -> bool:
        key = (key or "").strip()
        if not key:
            return False
        async with self._lock:
            try:
                if not await self._ensure_token():
                    return False
                await self._rpc("remoteKeyControl", {"remoteKey": key})
                return True
            except Exception:
                logger.debug("frame tv remote key failed key=%s", key, exc_info=True)
                return False

    async def send_key(self, *, key: str) -> None:
        await self.press(key=key)

    async def ensure_ws_connected(self) -> None:
        # Compatibility no-op: this backend does not use the Samsung websocket.
        return None
