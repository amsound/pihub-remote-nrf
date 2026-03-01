from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .dmr import dmr_up
from .wol import send_wol
from .ws_client import TvWsClient

logger = logging.getLogger(__name__)

POWER_RETRY_S = 1.5

@dataclass
class TvSnapshot:
    dmr_up: bool
    ws_connected: bool
    token_present: bool
    last_error: str

class TvController:
    def __init__(self, *, tv_ip: str, tv_mac: str, token_file: str, name: str) -> None:
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

        self._dmr_pending: bool | None = None
        self._dmr_pending_count: int = 0
        self._dmr_debounce_n: int = 2

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        await self.ws.close()
        sess, self._session = self._session, None
        if sess:
            await sess.close()

    async def poll(self) -> None:
        """
        Periodic poll: update dmr status and connect/disconnect ws accordingly.

        Important: while power_on() is running, do NOT close the websocket on DMR-down,
        otherwise we flap connect/close and KEY_POWER never lands reliably.
        """
        if not self._session:
            return

        # ---- DMR sample ----
        sample = await dmr_up(self._session, self.tv_ip)  # bool

        # ---- Debounce: require N consecutive samples before committing a change ----
        prev_committed = self._dmr_cached  # None | bool

        if prev_committed is None:
            # First observation: commit immediately so we get a boot log.
            self._dmr_cached = sample
            self._dmr_pending = None
            self._dmr_pending_count = 0
        else:
            if sample == prev_committed:
                # Stable, clear any pending transition
                self._dmr_pending = None
                self._dmr_pending_count = 0
            else:
                # Candidate transition
                if self._dmr_pending != sample:
                    self._dmr_pending = sample
                    self._dmr_pending_count = 1
                else:
                    self._dmr_pending_count += 1

                # Commit only after N consecutive samples
                if self._dmr_pending_count >= self._dmr_debounce_n:
                    self._dmr_cached = sample
                    self._dmr_pending = None
                    self._dmr_pending_count = 0

        up = bool(self._dmr_cached)

        # ---- Log once on boot, then only on committed transitions ----
        if prev_committed is None or self._dmr_cached != prev_committed:
            logger.debug("tv %s (dmr %s)", "on" if up else "off", "up" if up else "down")

        now = asyncio.get_running_loop().time()

        if up:
            # ensure websocket is up for instant keys (throttled)
            if not self.ws.state.connected and (now - self._last_ws_connect_attempt) > 3.0:
                self._last_ws_connect_attempt = now
                await self.ws.connect(self._session)
            return

        # DMR down: only close ws if we're NOT trying to power on
        if self._power_on_active:
            return

        if self.ws.state.connected:
            await self.ws.close()

    def snapshot(self) -> TvSnapshot:
        st = self.ws.state
        return TvSnapshot(
            dmr_up=self._dmr_cached,
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
        )

    async def power_off(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        if not self._session:
            return False
        if self._dmr_cached is False:
            return True  # already off

        # Send POWER only if on (dmr truth)
        ok = await self.ws.send_key("KEY_POWER")
        if not ok:
            # attempt quick connect and retry once
            await self.ws.connect(self._session)
            ok = await self.ws.send_key("KEY_POWER")

        if not wait:
            return ok

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if not await dmr_up(self._session, self.tv_ip):
                self._dmr_cached = False
                return True
            await asyncio.sleep(0.2)
        return False

    async def power_on(self, *, timeout_s: float = 60.0) -> bool:
        if not self._session:
            return False
        if self._dmr_cached is True:
            return True  # already on

        self._power_on_active = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s

            POWER_INTERVAL_S = 3.0
            WOL_INTERVAL_S = 1.0
            WS_CONNECT_INTERVAL_S = 0.8

            last_power = -1e9
            last_wol = -1e9
            last_ws_attempt = -1e9

            while loop.time() < deadline:
                now = loop.time()

                # Stop condition
                if await dmr_up(self._session, self.tv_ip):
                    self._dmr_cached = True
                    await self.ws.connect(self._session)  # keep ws up for instant keys
                    return True

                # 1) WOL spam until DMR up
                if (now - last_wol) >= WOL_INTERVAL_S:
                    try:
                        send_wol(self.tv_mac)
                    except Exception:
                        pass
                    last_wol = now

                # 2) Keep trying to (re)connect websocket frequently (best effort)
                if not self.ws.state.connected and (now - last_ws_attempt) >= WS_CONNECT_INTERVAL_S:
                    last_ws_attempt = now
                    try:
                        await self.ws.connect(self._session)
                    except Exception:
                        pass

                # 3) Send KEY_POWER immediately, then every 3 seconds until DMR up
                if (now - last_power) >= POWER_INTERVAL_S:
                    # Best-effort send; no ACK required
                    try:
                        # try current connection
                        sent = await self.ws.send_key("KEY_POWER")
                    except Exception:
                        sent = False

                    if not sent:
                        # try reconnect + send once
                        try:
                            await self.ws.connect(self._session)
                            await self.ws.send_key("KEY_POWER")
                        except Exception:
                            pass

                    last_power = now

                await asyncio.sleep(0.2)

            return False
        finally:
            self._power_on_active = False

    async def volume_up(self) -> bool:
        if not self._session or not self._dmr_cached:
            return False
        if not self.ws.state.connected:
            await self.ws.connect(self._session)
        return await self.ws.send_key("KEY_VOLUP")

    async def volume_down(self) -> bool:
        if not self._session or not self._dmr_cached:
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