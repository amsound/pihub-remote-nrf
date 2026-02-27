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
        self._dmr_cached: bool = False

        self._power_on_active: bool = False
        self._last_ws_connect_attempt: float = 0.0

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        s = self.snapshot()
        logger.info(
            "status dmr_up=%s ws_connected=%s",
            "true" if s.dmr_up else "false",
            "true" if s.ws_connected else "false",
        )

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

        prev_up = self._dmr_cached
        up = await dmr_up(self._session, self.tv_ip)
        self._dmr_cached = up

        if up != prev_up:
            if up:
                logger.info("TV On - dmr up")
            else:
                logger.info("TV Off - dmr down")

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
        if not self._dmr_cached:
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

    async def power_on(self, *, timeout_s: float = 20.0) -> bool:
        """
        Idempotent-ish discrete ON:
        - If DMR already up -> return True
        - If DMR down:
            * spam WOL
            * send KEY_POWER immediately once if WS can connect
            * optionally send exactly one more KEY_POWER at +5s if still OFF
            * otherwise rely on WOL
        """
        if not self._session:
            return False
        if self._dmr_cached:
            return True  # already on

        self._power_on_active = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s

            sent_power_1 = False
            sent_power_2 = False
            t0 = loop.time()

            # We want an immediate WS attempt in the "cat hit OFF" gap.
            # After that, don't churn connect attempts too hard.
            next_ws_attempt = 0.0

            while loop.time() < deadline:
                # 1) WOL spam (cheap + safe)
                try:
                    send_wol(self.tv_mac)
                except Exception:
                    pass

                # 2) Stop condition: DMR is up => on
                if await dmr_up(self._session, self.tv_ip):
                    self._dmr_cached = True
                    await self.ws.connect(self._session)
                    return True

                now = loop.time()
                elapsed = now - t0

                # 3) Try to get a WS connection quickly at the start, then back off
                if now >= next_ws_attempt and not self.ws.state.connected:
                    next_ws_attempt = now + 0.8  # light backoff (don’t wait 3s)
                    await self.ws.connect(self._session)

                # 4) Send POWER at most twice, only while DMR is still down
                # First POWER: as soon as we have WS
                if self.ws.state.connected and not sent_power_1:
                    await self.ws.send_key("KEY_POWER")
                    sent_power_1 = True

                # Second POWER: exactly once at +5s if still off
                if self.ws.state.connected and sent_power_1 and (not sent_power_2) and elapsed >= 5.0:
                    await self.ws.send_key("KEY_POWER")
                    sent_power_2 = True

                await asyncio.sleep(0.5)

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