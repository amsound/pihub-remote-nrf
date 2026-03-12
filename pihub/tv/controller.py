from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .dmr import dmr_up
from .wol import send_wol
from .ws_client import TvWsClient

logger = logging.getLogger(__name__)

POWER_RETRY_S = 1.5
VOLUME_STEP_PCT = 2
VOLUME_RECONCILE_DELAY_S = 0.75


@dataclass
class TvSnapshot:
    initialized: bool
    logical_on: bool | None
    logical_source: str
    last_change_age_s: int | None
    ws_connected: bool
    token_present: bool
    last_error: str
    volume_pct: int | None
    volume_last_update_age_s: int | None
    volume_optimistic: bool


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

        self._volume_pct: int | None = None
        self._volume_last_update_ts: float | None = None
        self._volume_optimistic: bool = False
        self._volume_reconcile_task: asyncio.Task | None = None

        self._state_change_callback = state_change_callback

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        await self.ws.close()
        task, self._volume_reconcile_task = self._volume_reconcile_task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
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
            self._schedule_volume_refresh(delay_s=0.0)
        if prev_on and not curr_on:
            self._volume_pct = None
            self._volume_last_update_ts = None
            self._volume_optimistic = False

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
        if self._dmr_cached is True and self._volume_pct is None:
            self._schedule_volume_refresh(delay_s=0.0)

    def snapshot(self) -> TvSnapshot:
        st = self.ws.state
        last_change_age_s: int | None = None
        if self._logical_last_change_ts is not None:
            last_change_age_s = int(asyncio.get_running_loop().time() - self._logical_last_change_ts)
        volume_last_update_age_s: int | None = None
        if self._volume_last_update_ts is not None:
            volume_last_update_age_s = int(asyncio.get_running_loop().time() - self._volume_last_update_ts)
        return TvSnapshot(
            initialized=self._dmr_cached is not None,
            logical_on=self._dmr_cached,
            ws_connected=st.connected,
            token_present=st.token_present,
            last_error=st.last_error,
            logical_source=self._logical_source,
            last_change_age_s=last_change_age_s,
            volume_pct=self._volume_pct,
            volume_last_update_age_s=volume_last_update_age_s,
            volume_optimistic=self._volume_optimistic,
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
                self._volume_pct = None
                self._volume_last_update_ts = None
                self._volume_optimistic = False
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
            if self._volume_pct is None:
                self._schedule_volume_refresh(delay_s=0.0)
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
                    self._schedule_volume_refresh(delay_s=0.0)
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

    def _set_local_volume(self, volume_pct: int, *, optimistic: bool) -> int:
        volume_pct = max(0, min(100, int(volume_pct)))
        self._volume_pct = volume_pct
        self._volume_last_update_ts = asyncio.get_running_loop().time()
        self._volume_optimistic = optimistic
        return volume_pct

    async def _rendering_control(self, action: str, inner_xml: str, *, timeout_s: float = 1.5) -> str:
        if not self._session:
            raise RuntimeError("tv session not started")

        url = f"http://{self.tv_ip}:9197/upnp/control/RenderingControl1"
        payload = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">
      {inner_xml}
    </u:{action}>
  </s:Body>
</s:Envelope>'''
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:RenderingControl:1#{action}"',
        }
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with self._session.post(url, data=payload.encode("utf-8"), headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def get_volume(self, *, refresh: bool = True) -> int | None:
        if not self._session or self._dmr_cached is not True:
            return self._volume_pct if not refresh else None

        if not refresh and self._volume_pct is not None:
            return self._volume_pct

        try:
            xml = await self._rendering_control(
                "GetVolume",
                "<InstanceID>0</InstanceID><Channel>Master</Channel>",
            )
            m = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", xml)
            if not m:
                raise RuntimeError(f"missing CurrentVolume in GetVolume response: {xml!r}")
            return self._set_local_volume(int(m.group(1)), optimistic=False)
        except Exception as e:
            self.ws.state.last_error = repr(e)
            logger.debug("tv get_volume failed: %r", e)
            return None

    async def set_volume(self, *, volume_pct: int, reconcile: bool = True) -> bool:
        if not self._session or self._dmr_cached is not True:
            return False

        volume_pct = self._set_local_volume(volume_pct, optimistic=True)
        try:
            await self._rendering_control(
                "SetVolume",
                f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{volume_pct}</DesiredVolume>",
            )
        except Exception as e:
            self.ws.state.last_error = repr(e)
            logger.debug("tv set_volume failed: %r", e)
            return False

        if reconcile:
            self._schedule_volume_refresh(delay_s=VOLUME_RECONCILE_DELAY_S)
        return True

    def _schedule_volume_refresh(self, *, delay_s: float) -> None:
        if not self._session or self._dmr_cached is not True:
            return
        task, self._volume_reconcile_task = self._volume_reconcile_task, None
        if task and not task.done():
            task.cancel()
        self._volume_reconcile_task = asyncio.create_task(
            self._refresh_volume_later(delay_s=delay_s),
            name="tv_volume_refresh",
        )

    async def _refresh_volume_later(self, *, delay_s: float) -> None:
        try:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            await self.get_volume(refresh=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("tv volume refresh failed", exc_info=True)

    async def volume_up(self) -> bool:
        if not self._session or self._dmr_cached is not True:
            return False
        current = self._volume_pct
        if current is None:
            current = await self.get_volume(refresh=True)
            if current is None:
                return False
        return await self.set_volume(volume_pct=current + VOLUME_STEP_PCT)

    async def volume_down(self) -> bool:
        if not self._session or self._dmr_cached is not True:
            return False
        current = self._volume_pct
        if current is None:
            current = await self.get_volume(refresh=True)
            if current is None:
                return False
        return await self.set_volume(volume_pct=current - VOLUME_STEP_PCT)

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
