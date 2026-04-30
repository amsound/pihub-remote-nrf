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

_COMMAND_RETRIES = 3
_VERIFY_DELAY_S = 0.35


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

    The 1516 control plane uses HTTPS JSON-RPC. If the access-token file is
    missing, the backend requests one with createAccessToken and persists it
    once the TV authorisation prompt is accepted.
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

    @staticmethod
    def _result(data: dict[str, Any]) -> dict[str, Any]:
        result = data.get("result") or {}
        if not isinstance(result, dict):
            raise RuntimeError("frame_tv_result_not_object")
        return result

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
        result = self._result(data)
        token = result.get("AccessToken")
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
        power = self._result(data).get("power")
        return power if isinstance(power, str) else None

    async def _get_input_source(self) -> str | None:
        data = await self._rpc("inputSourceControl")
        source = self._result(data).get("inputSource")
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

    async def _verified_power_control(
        self,
        *,
        target: str,
        source: str,
        timeout_s: float,
    ) -> bool:
        last_error = ""
        for attempt in range(1, _COMMAND_RETRIES + 1):
            try:
                data = await self._rpc(
                    "powerControl",
                    {"power": target},
                    timeout_s=timeout_s,
                )
                ack_power = self._result(data).get("power")
                if ack_power != target:
                    raise RuntimeError(f"power_ack_mismatch:{ack_power!r}!={target!r}")

                await asyncio.sleep(_VERIFY_DELAY_S)
                actual_power = await self._get_power()
                self._commit_power(actual_power, source=source)
                if actual_power == target:
                    logger.info(
                        "frame tv power command verified target=%s attempt=%d",
                        target,
                        attempt,
                    )
                    return True

                raise RuntimeError(f"power_verify_mismatch:{actual_power!r}!={target!r}")
            except Exception as exc:
                last_error = repr(exc)
                logger.debug(
                    "frame tv power command attempt failed target=%s attempt=%d/%d error=%r",
                    target,
                    attempt,
                    _COMMAND_RETRIES,
                    exc,
                    exc_info=True,
                )
                if attempt < _COMMAND_RETRIES:
                    await asyncio.sleep(0.25 * attempt)

        self._last_error = last_error or "power_command_failed"
        logger.warning("frame tv power command failed target=%s error=%s", target, self._last_error)
        return False

    async def power_on(self, *, timeout_s: float = 8.0) -> bool:
        async with self._lock:
            if not await self._ensure_token():
                return False
            return await self._verified_power_control(
                target="powerOn",
                source="ip_control_power_on",
                timeout_s=timeout_s,
            )

    async def power_off(self, *, wait: bool = True, timeout_s: float = 8.0) -> bool:
        del wait
        async with self._lock:
            if not await self._ensure_token():
                return False
            return await self._verified_power_control(
                target="powerOff",
                source="ip_control_power_off",
                timeout_s=timeout_s,
            )

    async def hdmi1(self) -> bool:
        return await self.set_hdmi1()

    async def set_hdmi1(self) -> bool:
        async with self._lock:
            if not await self._ensure_token():
                return False

            last_error = ""
            for attempt in range(1, _COMMAND_RETRIES + 1):
                try:
                    data = await self._rpc(
                        "inputSourceControl",
                        {"inputSource": "HDMI1"},
                    )
                    ack_source = self._result(data).get("inputSource")
                    if ack_source != "HDMI1":
                        raise RuntimeError(f"source_ack_mismatch:{ack_source!r}!='HDMI1'")

                    await asyncio.sleep(_VERIFY_DELAY_S)
                    actual_source = await self._get_input_source()
                    self._input_source = actual_source
                    if actual_source == "HDMI1":
                        logger.info("frame tv source command verified target=HDMI1 attempt=%d", attempt)
                        return True

                    raise RuntimeError(f"source_verify_mismatch:{actual_source!r}!='HDMI1'")
                except Exception as exc:
                    last_error = repr(exc)
                    logger.debug(
                        "frame tv HDMI1 command attempt failed attempt=%d/%d error=%r",
                        attempt,
                        _COMMAND_RETRIES,
                        exc,
                        exc_info=True,
                    )
                    if attempt < _COMMAND_RETRIES:
                        await asyncio.sleep(0.25 * attempt)

            self._last_error = last_error or "source_command_failed"
            logger.warning("frame tv HDMI1 command failed error=%s", self._last_error)
            return False

    async def return_key(self) -> bool:
        return await self.press(key="return")

    async def press(self, *, key: str) -> bool:
        key = (key or "").strip()
        if not key:
            return False
        async with self._lock:
            if not await self._ensure_token():
                return False

            last_error = ""
            for attempt in range(1, _COMMAND_RETRIES + 1):
                try:
                    data = await self._rpc("remoteKeyControl", {"remoteKey": key})
                    ack_key = self._result(data).get("remoteKey")
                    if ack_key != key:
                        raise RuntimeError(f"remote_key_ack_mismatch:{ack_key!r}!={key!r}")

                    logger.info("frame tv remote key acknowledged key=%s attempt=%d", key, attempt)
                    return True
                except Exception as exc:
                    last_error = repr(exc)
                    logger.debug(
                        "frame tv remote key attempt failed key=%s attempt=%d/%d error=%r",
                        key,
                        attempt,
                        _COMMAND_RETRIES,
                        exc,
                        exc_info=True,
                    )
                    if attempt < _COMMAND_RETRIES:
                        await asyncio.sleep(0.25 * attempt)

            self._last_error = last_error or "remote_key_failed"
            logger.warning("frame tv remote key failed key=%s error=%s", key, self._last_error)
            return False

    async def send_key(self, *, key: str) -> None:
        await self.press(key=key)
