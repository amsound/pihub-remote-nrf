from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .dmr import dmr_up
from .wol import send_wol
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
        self._dmr_cached: bool | None = None

        self._power_on_active: bool = False
        self._last_ws_connect_attempt: float = 0.0

        self._logical_source: str = "unknown"
        self._logical_last_change_ts: float | None = None

        self._state_change_callback = state_change_callback

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        await self.ws.close()
        sess, self._session = self._session, None
        if sess:
            await sess.close()

    def _commit_discovery(self, on: bool, *, source: str) -> bool:
        prev_on = self._dmr_cached is True
        if self._dmr_cached is on:
            return False

        now = asyncio.get_running_loop().time()
        self._dmr_cached = on
        self._logical_source = source
        self._logical_last_change_ts = now

        curr_on = self._dmr_cached is True
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

        changed = self._commit_discovery(True, source="msearch")
        logger.info(
            "tv msearch accepted changed=%s location=%s logical_on=%s logical_source=%s",
            "true" if changed else "false",
            location,
            "true" if self._dmr_cached is True else "false",
            self._logical_source,
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
        """Return True only when discovery changed logical TV state."""
        is_dmr = False
        if location and "/dmr" in location:
            is_dmr = True
        if "MediaRenderer" in (nt or ""):
            is_dmr = True
        if not is_dmr:
            return False

        if nts == "ssdp:alive":
            return self._commit_discovery(True, source="ssdp_alive")

        if nts == "ssdp:byebye":
            return self._commit_discovery(False, source="ssdp_byebye")

        return False

    async def ensure_ws_connected(self) -> None:
        if not self._session:
            return
        await self.ws.connect(self._session)

    def snapshot(self) -> TvSnapshot:
        st = self.ws.state
        last_change_age_s: int | None = None
        if self._logical_last_change_ts is not None:
            last_change_age_s = int(asyncio.get_running_loop().time() - self._logical_last_change_ts)
        return TvSnapshot(
            initialized=self._dmr_cached is not None,
            logical_on=self._dmr_cached,
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
            logical_source=self._logical_source,
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

    async def power_off(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        if not self._session:
            return False

        if self._dmr_cached is False:
            return True

        ok = await self.ws.send_key("KEY_POWER")
        if not ok:
            await self.ws.connect(self._session)
            ok = await self.ws.send_key("KEY_POWER")

        if not wait:
            return ok

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if not await dmr_up(self._session, self.tv_ip):
                self._dmr_cached = False
                now = asyncio.get_running_loop().time()
                self._logical_source = "power_off"
                self._logical_last_change_ts = now
                return True
            await asyncio.sleep(0.2)
        return False

    async def power_on(self, *, timeout_s: float = 60.0) -> bool:
        if not self._session:
            return False

        if self._dmr_cached is True:
            return True

        self._power_on_active = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            start = loop.time()

            WOL_FAST_INTERVAL_S = 0.25
            WOL_SLOW_INTERVAL_S = 1.0
            WOL_FAST_WINDOW_S = 2.0
            WS_FAST_INTERVAL_S = 0.25
            WS_SLOW_INTERVAL_S = 0.8
            WS_FAST_WINDOW_S = 2.0

            power_toggle_sent = False
            last_wol = -1e9
            last_ws_attempt = -1e9

            while loop.time() < deadline:
                now = loop.time()
                elapsed = now - start

                if await dmr_up(self._session, self.tv_ip):
                    self._dmr_cached = True
                    self._logical_source = "power_on"
                    self._logical_last_change_ts = asyncio.get_running_loop().time()
                    await self.ws.connect(self._session)
                    return True

                wol_interval_s = WOL_FAST_INTERVAL_S if elapsed < WOL_FAST_WINDOW_S else WOL_SLOW_INTERVAL_S
                ws_interval_s = WS_FAST_INTERVAL_S if elapsed < WS_FAST_WINDOW_S else WS_SLOW_INTERVAL_S

                if (now - last_wol) >= wol_interval_s:
                    try:
                        send_wol(self.tv_mac)
                    except Exception:
                        pass
                    last_wol = now

                ws_connected_now = self.ws.state.connected
                if not ws_connected_now and (now - last_ws_attempt) >= ws_interval_s:
                    last_ws_attempt = now
                    try:
                        ws_connected_now = await self.ws.connect(self._session)
                    except Exception:
                        ws_connected_now = False

                if ws_connected_now and not power_toggle_sent:
                    try:
                        power_toggle_sent = await self.ws.send_key("KEY_POWER")
                    except Exception:
                        power_toggle_sent = False

                await asyncio.sleep(0.2)

            return False
        finally:
            self._power_on_active = False

    async def volume_up(self) -> bool:
        if not self._session or self._dmr_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_VOLUP")

    async def volume_down(self) -> bool:
        if not self._session or self._dmr_cached is not True:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_VOLDOWN")

    async def mute_toggle(self) -> bool:
        if not self._session or not self._dmr_cached:
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