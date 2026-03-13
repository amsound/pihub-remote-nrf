from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .presence import presence_probe_up
from .wol import send_wol_burst
from .ws_client import TvWsClient

logger = logging.getLogger(__name__)


@dataclass
class TvSnapshot:
    initialized: bool
    logical_on: bool | None
    logical_source: str
    last_change_age_s: int | None
    ws_connected: bool
    token_present: bool
    last_error: str


class TvController:
    def __init__(
        self,
        *,
        tv_ip: str,
        tv_mac: str,
        token_file: str,
        name: str,
        state_change_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.tv_ip = tv_ip
        self.tv_mac = tv_mac
        self.token_file = token_file
        self.name = name
        self.ws = TvWsClient(tv_ip=tv_ip, token_file=token_file, name=name)

        logger.info(
            "initialised tv tv_ip=%s tv_mac=%s token_present=%s",
            tv_ip,
            tv_mac,
            "true" if self.ws.state.token_present else "false",
        )

        self._session: Optional[aiohttp.ClientSession] = None

        # Internal naming moved to "presence" even though the public snapshot
        # still uses logical_on/logical_source for compatibility.
        self._presence_cached: bool | None = None
        self._presence_source: str = "unknown"
        self._presence_last_change_ts: float | None = None

        self._power_on_active: bool = False
        self._last_power_off_request_ts: float | None = None

        self._state_change_callback = state_change_callback

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        await self.ws.close()
        sess, self._session = self._session, None
        if sess:
            await sess.close()

    def _commit_presence(self, on: bool, *, source: str) -> bool:
        prev_on = self._presence_cached is True
        if self._presence_cached is on:
            return False

        now = asyncio.get_running_loop().time()
        self._presence_cached = on
        self._presence_source = source
        self._presence_last_change_ts = now

        curr_on = self._presence_cached is True
        if not prev_on and curr_on:
            self._emit_state_change(
                "watch",
                {
                    "domain": "tv",
                    "logical_source": source,
                },
            )

        return True

    def notify_msearch(self, *, location: str | None) -> bool:
        if location and "/dmr" not in location:
            logger.info("tv msearch rejected location=%s", location)
            return False

        changed = self._commit_presence(True, source="msearch")
        logger.info(
            "tv msearch accepted changed=%s location=%s logical_on=%s logical_source=%s",
            "true" if changed else "false",
            location,
            "true" if self._presence_cached is True else "false",
            self._presence_source,
        )
        return changed

    def notify_ssdp(
        self,
        *,
        nts: str,
        nt: str,
        usn: str,
        location: str | None,
        source: str = "ssdp",
    ) -> bool:
        """
        Return True only when discovery changed logical TV presence.

        SSDP alive/byebye is the primary runtime truth signal.
        """
        is_renderer_presence = False
        if location and "/dmr" in location:
            is_renderer_presence = True
        if "MediaRenderer" in (nt or ""):
            is_renderer_presence = True
        if not is_renderer_presence:
            return False

        if nts == "ssdp:alive":
            return self._commit_presence(True, source="ssdp_alive")

        if nts == "ssdp:byebye":
            return self._commit_presence(False, source="ssdp_byebye")

        return False

    async def ensure_ws_connected(self) -> None:
        if not self._session:
            return
        await self.ws.connect(self._session)

    def snapshot(self) -> TvSnapshot:
        st = self.ws.state
        last_change_age_s: int | None = None
        if self._presence_last_change_ts is not None:
            last_change_age_s = int(asyncio.get_running_loop().time() - self._presence_last_change_ts)

        return TvSnapshot(
            initialized=self._presence_cached is not None,
            logical_on=self._presence_cached,
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
            logical_source=self._presence_source,
            last_change_age_s=last_change_age_s,
        )

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        cb = self._state_change_callback
        if cb is None:
            return
        try:
            asyncio.create_task(cb(name, payload))
        except Exception:
            logger.exception("tv state change callback failed name=%s", name)

    async def _wait_for_presence_true(self, *, timeout_s: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if self._presence_cached is True:
                return True
            await asyncio.sleep(0.1)
        return False

    async def power_off(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        if not self._session:
            return False

        if self._presence_cached is False:
            return True

        now = asyncio.get_running_loop().time()
        self._last_power_off_request_ts = now

        ok = await self.ws.send_key("KEY_POWER")
        if not ok:
            await self.ws.connect(self._session)
            ok = await self.ws.send_key("KEY_POWER")

        if not wait:
            return ok

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            # Preferred truth path: wait for SSDP byebye to land.
            if self._presence_cached is False:
                return True

            # Fallback: if SSDP is delayed/missed, use HTTP probe to confirm down.
            if not await presence_probe_up(self._session, self.tv_ip):
                self._commit_presence(False, source="probe_http_down")
                return True

            await asyncio.sleep(0.2)

        return False

    async def power_on(self, *, timeout_s: float = 60.0) -> bool:
        if not self._session:
            return False

        # Idempotent: if our truth says on, do nothing.
        if self._presence_cached is True:
            return True

        # Idempotent-ish: if another power_on is already in flight, wait for it.
        if self._power_on_active:
            return await self._wait_for_presence_true(timeout_s=timeout_s)

        self._power_on_active = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            start = loop.time()

            RECOVERY_WINDOW_S = 30.0

            WOL_FAST_INTERVAL_S = 0.25
            WOL_SLOW_INTERVAL_S = 1.0
            WOL_FAST_WINDOW_S = 2.0

            WS_FAST_INTERVAL_S = 0.25
            WS_SLOW_INTERVAL_S = 0.8
            WS_FAST_WINDOW_S = 2.0

            HTTP_PROBE_INTERVAL_S = 1.0

            # Recovery rescue:
            # one best-effort toggle in the recent-off window only, while presence
            # is still false. This avoids the "TV woke, then got toggled off again"
            # failure mode.
            rescue_recent_off = (
                self._last_power_off_request_ts is not None
                and (start - self._last_power_off_request_ts) <= RECOVERY_WINDOW_S
            )
            rescue_toggle_attempted = False

            last_wol = -1e9
            last_ws_attempt = -1e9
            last_http_probe = -1e9

            while loop.time() < deadline:
                now = loop.time()
                elapsed = now - start

                # Truth gate first: once presence is on, stop immediately.
                if self._presence_cached is True:
                    await self.ws.connect(self._session)
                    return True

                # HTTP probe is fallback only, not primary truth.
                if (now - last_http_probe) >= HTTP_PROBE_INTERVAL_S:
                    last_http_probe = now
                    try:
                        if await presence_probe_up(self._session, self.tv_ip):
                            self._commit_presence(True, source="probe_http_up")
                            await self.ws.connect(self._session)
                            return True
                    except Exception:
                        pass

                wol_interval_s = WOL_FAST_INTERVAL_S if elapsed < WOL_FAST_WINDOW_S else WOL_SLOW_INTERVAL_S
                ws_interval_s = WS_FAST_INTERVAL_S if elapsed < WS_FAST_WINDOW_S else WS_SLOW_INTERVAL_S

                if (now - last_wol) >= wol_interval_s:
                    try:
                        await send_wol_burst(
                            self.tv_mac,
                            count=3,
                            gap_s=0.25,
                            port=9,
                            broadcast="255.255.255.255",
                        )
                    except Exception:
                        logger.debug("tv wol burst failed", exc_info=True)
                    last_wol = now

                ws_connected_now = self.ws.state.connected
                if not ws_connected_now and (now - last_ws_attempt) >= ws_interval_s:
                    last_ws_attempt = now
                    try:
                        ws_connected_now = await self.ws.connect(self._session)
                    except Exception:
                        ws_connected_now = False

                # Recovery-only rescue path:
                # allow one websocket KEY_POWER attempt only while presence is still false.
                if rescue_recent_off and ws_connected_now and not rescue_toggle_attempted:
                    try:
                        rescue_toggle_attempted = True
                        await self.ws.send_key("KEY_POWER")
                    except Exception:
                        logger.debug("tv rescue power toggle failed", exc_info=True)

                await asyncio.sleep(0.2)

            return False
        finally:
            self._power_on_active = False

    async def volume_up(self) -> bool:
        if not self._session or self._presence_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_VOLUP")

    async def volume_down(self) -> bool:
        if not self._session or self._presence_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_VOLDOWN")

    async def mute_toggle(self) -> bool:
        if not self._session or self._presence_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_MUTE")

    async def send_key(self, *, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        await self.ws.send_key(key)

    async def press(self, *, key: str) -> None:
        await self.send_key(key=key)