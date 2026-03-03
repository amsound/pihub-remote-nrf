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
    logical_on: bool
    logical_source: str
    last_change_age_s: int | None
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

        # Poll cadence
        self._poll_idle_s: float = 5.0        # normal cadence (CEC/AirPlay detection)
        self._poll_fast_s: float = 0.5       # during intent windows
        self._fast_poll_until: float = 0.0
        self._fast_poll_reason: str = ""

        # Stability streaks (consecutive committed polls)
        self._on_dmr_streak: int = 0
        self._on_ws_streak: int = 0
        self._off_dmr_streak: int = 0
        self._off_ws_streak: int = 0

        self._on_stable_needed: int = 4       # 3–4 as you suggested
        self._off_stable_needed: int = 3

        self._logical_source: str = "unknown"
        self._logical_last_change_ts: float | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        await self.ws.close()
        sess, self._session = self._session, None
        if sess:
            await sess.close()

    def notify_ssdp(self, *, nts: str, nt: str, usn: str, location: str | None, source: str = "ssdp") -> bool:
        """
        SSDP NOTIFY handler. Returns True if this was a DMR-related event we acted on.
        We treat DMR SSDP alive/byebye as the logical power truth.
        """
        is_dmr = False
        if location and "/dmr" in location:
            is_dmr = True
        if "MediaRenderer" in (nt or ""):
            is_dmr = True
        if not is_dmr:
            return False

        if nts == "ssdp:alive":
            if self._dmr_cached is not True:
                self._dmr_cached = True

                now = asyncio.get_running_loop().time()
                self._logical_source = source
                self._logical_last_change_ts = now

                self._dmr_pending = None
                self._dmr_pending_count = 0
                self.enter_fast_poll(seconds=8.0, reason="external_on")
                return True

        if nts == "ssdp:byebye":
            if self._dmr_cached is not False:
                self._dmr_cached = False

                now = asyncio.get_running_loop().time()
                self._logical_source = source
                self._logical_last_change_ts = now

            self._dmr_pending = None
            self._dmr_pending_count = 0
            self.enter_fast_poll(seconds=6.0, reason="external_off")
            return True

        return False
    
    async def ensure_ws_connected(self) -> None:
        if self._session:
            await self.ws.connect(self._session)

    def set_power_state(self, on: bool, *, reason: str, source: str = "unknown") -> None:
        now = asyncio.get_running_loop().time()
        self._logical_source = source
        self._logical_last_change_ts = now
        self._dmr_cached = bool(on)
        self._dmr_pending = None
        self._dmr_pending_count = 0
        self.enter_fast_poll(seconds=8.0 if on else 6.0, reason=reason)

    def enter_fast_poll(self, *, seconds: float, reason: str) -> None:
        now = asyncio.get_running_loop().time()
        self._fast_poll_until = max(self._fast_poll_until, now + max(0.0, float(seconds)))
        self._fast_poll_reason = reason

        # reset streaks so we require consecutive confirmations after the intent
        self._on_dmr_streak = self._on_ws_streak = 0
        self._off_dmr_streak = self._off_ws_streak = 0

    def poll_interval_s(self) -> float:
        now = asyncio.get_running_loop().time()
        return self._poll_fast_s if now < self._fast_poll_until else self._poll_idle_s

    async def poll(self) -> None:
        if not self._session:
            return

        now = asyncio.get_running_loop().time()

        # ---- DMR sample ----
        sample = await dmr_up(self._session, self.tv_ip)  # bool

        # ---- Debounce ----
        prev_committed = self._dmr_cached  # None | bool

        if prev_committed is None:
            self._dmr_cached = sample
            self._dmr_pending = None
            self._dmr_pending_count = 0
            committed_changed = True

            self._logical_source = "poll"
            self._logical_last_change_ts = now

        else:
            committed_changed = False
            if sample == prev_committed:
                self._dmr_pending = None
                self._dmr_pending_count = 0
            else:
                if self._dmr_pending != sample:
                    self._dmr_pending = sample
                    self._dmr_pending_count = 1
                else:
                    self._dmr_pending_count += 1

                if self._dmr_pending_count >= self._dmr_debounce_n:
                    self._dmr_cached = sample
                    self._dmr_pending = None
                    self._dmr_pending_count = 0
                    committed_changed = True

                    self._logical_source = "poll"
                    self._logical_last_change_ts = now

        up = bool(self._dmr_cached)
        ws_up = bool(self.ws.state.connected)

        # External-on transition: DMR committed from off -> on
        if committed_changed and prev_committed is False and up is True:
            self.enter_fast_poll(seconds=8.0, reason="external_on")

        # ---- streaks ----
        if up:
            self._on_dmr_streak += 1
            self._off_dmr_streak = 0
        else:
            self._off_dmr_streak += 1
            self._on_dmr_streak = 0

        if ws_up:
            self._on_ws_streak += 1
            self._off_ws_streak = 0
        else:
            self._off_ws_streak += 1
            self._on_ws_streak = 0

        # Exit fast polling early when stable
        if now < self._fast_poll_until:
            if self._fast_poll_reason in ("power_on", "external_on"):
                if self._on_dmr_streak >= self._on_stable_needed and self._on_ws_streak >= self._on_stable_needed:
                    self._fast_poll_until = 0.0

            if self._fast_poll_reason == "power_off":
                if self._off_dmr_streak >= self._off_stable_needed and self._off_ws_streak >= self._off_stable_needed:
                    self._fast_poll_until = 0.0

        # ---- log ----
        if prev_committed is None or committed_changed:
            logger.debug(
                "tv logical_%s (source=%s, ws=%s)",
                "on" if up else "off",
                self._logical_source,
                "connected" if ws_up else "disconnected",
            )

        # ---- WS connect/disconnect behavior (your existing logic) ----
        if up:
            if not ws_up and (now - self._last_ws_connect_attempt) > 3.0:
                self._last_ws_connect_attempt = now
                await self.ws.connect(self._session)
            return

        if self._power_on_active:
            return

        if ws_up:
            await self.ws.close()

    def snapshot(self) -> TvSnapshot:
        st = self.ws.state
        logical_source: str = self._logical_source
        last_change_age_s: int | None = None
        if self._logical_last_change_ts is not None:
            last_change_age_s = int(asyncio.get_running_loop().time() - self._logical_last_change_ts)
        return TvSnapshot(
            logical_on=bool(self._dmr_cached),
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
            logical_source=logical_source,
            last_change_age_s=last_change_age_s,
        )

    async def power_off(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        if not self._session:
            return False
        
        self.enter_fast_poll(seconds=20.0, reason="power_off")

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
                now = asyncio.get_running_loop().time()
                self._logical_source = "power_off"
                self._logical_last_change_ts = now
                return True
            await asyncio.sleep(0.2)
        return False

    async def power_on(self, *, timeout_s: float = 60.0) -> bool:
        if not self._session:
            return False
        
        self.enter_fast_poll(seconds=20.0, reason="power_on")

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
                    now = asyncio.get_running_loop().time()
                    self._logical_source = "power_on"
                    self._logical_last_change_ts = now
                    await self.ws.connect(self._session)
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
    
    async def send_key(self, *, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        await self.ws.send_key(key)

    async def press(self, *, key: str) -> None:
        # same as send_key
        await self.send_key(key=key)