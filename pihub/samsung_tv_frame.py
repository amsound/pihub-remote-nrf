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
import time
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .initial_status import InitialStatus
from .samsung_tv import ssdp_listener
from .tv import BACKEND_FRAME, TvSnapshot

logger = logging.getLogger(__name__)

_COMMAND_RETRIES = 3
_VERIFY_DELAY_S = 0.35
_VERIFY_POLL_INTERVAL_S = 0.5
# Power is read when the TV announces its media renderer (SSDP), which it does
# in the same second as every on/off, and before every flow. It is never polled.
# Let an announcement burst (a switch-on sends byebye then alive) settle into one read.
ANNOUNCE_READ_DELAY_S = 0.5
SSDP_RESTART_DELAY_S = 5.0


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
        self.initial = InitialStatus(logger)

        self._presence_cached: bool | None = None
        self._presence_source = "unknown"
        self._presence_changed_at: float | None = None
        self._power: str | None = None
        self._input_source: str | None = None
        self._last_error = ""
        self._token_request_logged = False
        self._ssdp_task: asyncio.Task | None = None
        self._announce_read_task: asyncio.Task | None = None

        logger.info(
            "started tv_ip=%s token_present=%s",
            tv_ip,
            "true" if self._read_token() else "false",
        )

    def set_state_change_callback(
        self, callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        """Where watch/listen device-state signals go (the runtime)."""
        self._state_change_callback = callback

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=8)
            self._session = aiohttp.ClientSession(timeout=timeout)
        if self._ssdp_task is None or self._ssdp_task.done():
            self._ssdp_task = asyncio.create_task(self._listen_ssdp(), name="frame_tv:ssdp")

    async def stop(self) -> None:
        tasks = [t for t in (self._ssdp_task, self._announce_read_task) if t and not t.done()]
        self._ssdp_task = self._announce_read_task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        sess, self._session = self._session, None
        if sess:
            await sess.close()

    async def _listen_ssdp(self) -> None:
        """Hear the TV's SSDP announcements (see notify_ssdp); restart if the socket fails."""
        while True:
            try:
                await ssdp_listener(self)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("ssdp listener failed; restarting in %gs", SSDP_RESTART_DELAY_S, exc_info=True)
            await asyncio.sleep(SSDP_RESTART_DELAY_S)

    def notify_ssdp(
        self,
        *,
        nts: str,
        nt: str,
        usn: str,
        location: str | None,
        source: str = "ssdp",
    ) -> bool:
        """A media-renderer announcement: the power may have changed, so read it.

        The announcement is only the trigger. On and off come from the TV's own
        answer over IP control, which is exact (the Frame's network stays up
        when it is off). Always returns False: nothing else should act on it.
        """
        if "MediaRenderer" not in (nt or "") and "/dmr" not in (location or ""):
            return False
        if self._announce_read_task is None or self._announce_read_task.done():
            self._announce_read_task = asyncio.create_task(
                self._read_after_announce(), name="frame_tv:announce_read"
            )
        return False

    async def _read_after_announce(self) -> None:
        await asyncio.sleep(ANNOUNCE_READ_DELAY_S)
        try:
            await self.refresh_presence()
        except Exception:
            logger.debug("power read after announcement failed", exc_info=True)

    def _read_token(self) -> str:
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                return f.read().strip()
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

        # A first reading during start-up is where things stand, not a change: it must
        # not send a watch signal. (A device that only turns up later still does.)
        first_reading = not self.initial.settled
        if next_on is not None:
            self.initial.mark_received(f"power={'on' if next_on else 'off'} via={source}")

        if self._presence_cached is not next_on:
            self._presence_cached = next_on
            self._presence_source = source
            self._presence_changed_at = time.time()

            if not previous_on and next_on is True and not first_reading:
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
                logger.debug("state change callback cancelled name=%s", name)
            except Exception:
                logger.exception("state change callback failed name=%s", name)

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
                "token missing; requesting access token tv_ip=%s token_file=%s "
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
                "token not available tv_ip=%s token_file=%s error=%r",
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
        logger.info("token saved to %s", self.token_file)
        return token.strip()

    async def refresh_presence(self, *, timeout_s: float = 2.0) -> None:
        """Read the TV's real power state (it answers even when off).

        Called before every flow and on each media-renderer announcement, so a
        change made with the TV's own remote is picked up.
        """
        async with self._lock:
            if not self._read_token():
                return
            try:
                data = await self._rpc("powerControl", timeout_s=timeout_s)
            except Exception:
                logger.debug("power refresh failed", exc_info=True)
                return
            power = self._result(data).get("power")
            self._commit_power(power if isinstance(power, str) else None, source="ip_control_power")

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
                    logger.debug("power reconcile failed", exc_info=True)
                    errors.append(f"power:{exc!r}")

                if self._presence_cached is True:
                    try:
                        self._input_source = await self._get_input_source()
                    except Exception as exc:
                        logger.debug("source reconcile failed", exc_info=True)
                        errors.append(f"source:{exc!r}")

        changed = before_presence != self._presence_cached or before_source != self._input_source
        outcome = (
            "present_true" if self._presence_cached is True
            else "present_false" if self._presence_cached is False
            else "unknown"
        )

        if not self.initial.received:
            self.initial.mark_failed("; ".join(errors) or "tv gave no power state")

        return {
            "outcome": outcome,
            "presence_on": self._presence_cached,
            "presence_source": self._presence_source,
            "power": self._power,
            "input_source": self._input_source,
            "changed": changed,
            "errors": errors,
        }

    def snapshot(self) -> TvSnapshot:
        return TvSnapshot(
            backend=BACKEND_FRAME,
            presence_on=self._presence_cached,
            presence_source=self._presence_source,
            changed_at=self._presence_changed_at,
            ws_connected=False,
            token_present=bool(self._read_token()),
            last_error=self._last_error,
            input_source=self._input_source,
        )

    async def _wait_for_power_state(
        self,
        *,
        target: str,
        source: str,
        deadline: float,
    ) -> bool:
        last_power: str | None = None
        last_error = ""

        await asyncio.sleep(_VERIFY_DELAY_S)
        while asyncio.get_running_loop().time() < deadline:
            try:
                last_power = await self._get_power()
                self._commit_power(last_power, source=source)
                if last_power == target:
                    self._last_error = ""
                    return True
            except Exception as exc:
                last_error = repr(exc)
                logger.debug(
                    "power verification poll failed target=%s error=%r",
                    target,
                    exc,
                    exc_info=True,
                )

            await asyncio.sleep(_VERIFY_POLL_INTERVAL_S)

        self._last_error = (
            f"power_verify_timeout:last_power={last_power!r} target={target!r}"
            if not last_error
            else f"power_verify_timeout:last_power={last_power!r} target={target!r} last_error={last_error}"
        )
        return False

    async def _verified_power_control(
        self,
        *,
        target: str,
        source: str,
        timeout_s: float,
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_s)
        last_error = ""

        # Retry the command only until we get a valid ACK. Once the TV ACKs a
        # power transition, do not resend the command simply because the status
        # surface is still catching up; just poll for the final state.
        for attempt in range(1, _COMMAND_RETRIES + 1):
            try:
                remaining = max(0.5, deadline - asyncio.get_running_loop().time())
                data = await self._rpc(
                    "powerControl",
                    {"power": target},
                    timeout_s=min(timeout_s, remaining),
                )
                ack_power = self._result(data).get("power")
                if ack_power != target:
                    raise RuntimeError(f"power_ack_mismatch:{ack_power!r}!={target!r}")

                logger.debug(
                    "power command acknowledged target=%s attempt=%d",
                    target,
                    attempt,
                )

                if await self._wait_for_power_state(
                    target=target,
                    source=source,
                    deadline=deadline,
                ):
                    logger.debug(
                        "power command verified target=%s attempt=%d",
                        target,
                        attempt,
                    )
                    return True

                logger.warning(
                    "power command acknowledged but did not verify target=%s error=%s",
                    target,
                    self._last_error,
                )
                return False

            except Exception as exc:
                last_error = repr(exc)
                logger.debug(
                    "power command attempt failed target=%s attempt=%d/%d error=%r",
                    target,
                    attempt,
                    _COMMAND_RETRIES,
                    exc,
                    exc_info=True,
                )
                if attempt < _COMMAND_RETRIES:
                    await asyncio.sleep(0.25 * attempt)

        self._last_error = last_error or "power_command_failed"
        logger.warning("power command failed target=%s error=%s", target, self._last_error)
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

    async def set_input_hdmi1(self) -> bool:
        """Make sure the TV is on HDMI1: switch it only if it is on something else, and confirm.

        The TV shows an input banner whenever it is told to switch, even to the
        input it is already on, so the command is not sent when it isn't needed.
        """
        async with self._lock:
            if not await self._ensure_token():
                return False

            last_error = ""
            for attempt in range(1, _COMMAND_RETRIES + 1):
                try:
                    self._input_source = await self._get_input_source()
                    if self._input_source == "HDMI1":
                        logger.debug("tv already on HDMI1; source command not sent (attempt=%d)", attempt)
                        return True

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
                        logger.info("source command verified target=HDMI1 attempt=%d", attempt)
                        return True

                    raise RuntimeError(f"source_verify_mismatch:{actual_source!r}!='HDMI1'")
                except Exception as exc:
                    last_error = repr(exc)
                    logger.debug(
                        "source command attempt failed attempt=%d/%d error=%r",
                        attempt,
                        _COMMAND_RETRIES,
                        exc,
                        exc_info=True,
                    )
                    if attempt < _COMMAND_RETRIES:
                        await asyncio.sleep(0.25 * attempt)

            self._last_error = last_error or "source_command_failed"
            logger.warning("source command failed error=%s", self._last_error)
            return False
