"""Passive Apple TV AirPlay activity detector via mDNS TXT flags.

This domain listens for Apple TV AirPlay mDNS updates, filters them by a
configured static IP address, and emits a debounced "watch" device-state signal
when the Apple TV advertises an active AirPlay receiver session.

Known Apple TV AirPlay TXT flag behaviour from local captures:

    0x18644   idle
    0x38644   AirPlay / mirroring session active
    0x118644  native Apple TV playback active
    0x138644  AirPlay session + playback active

For this domain we intentionally care only about the connected AirPlay session
bit, not the playback bit, because AirPlay mirroring/video may not expose a
separate "playing" bit.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

logger = logging.getLogger(__name__)

AIRPLAY_SERVICE_TYPE = "_airplay._tcp.local."
APPLE_TV_CONNECTED_SESSION_BIT = 0x20000


StateChangeCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class AppleTvAirPlayState:
    configured: bool = False
    enabled: bool = False
    ready: bool = False
    reachable: bool = False

    apple_tv_ip: str = ""
    service_name: str | None = None

    flags: int | None = None
    connected_session: bool = False

    last_update_ts: float | None = None
    last_emit_ts: float | None = None
    last_error: str | None = None


class AppleTvAirPlay:
    """Listen for Apple TV AirPlay receiver-session state and emit watch signals."""

    def __init__(
        self,
        *,
        apple_tv_ip: str,
        debounce_s: float = 2.5,
    ) -> None:
        self.apple_tv_ip = (apple_tv_ip or "").strip()
        self.debounce_s = max(0.0, float(debounce_s))

        self.state = AppleTvAirPlayState(
            configured=bool(self.apple_tv_ip),
            enabled=bool(self.apple_tv_ip),
            apple_tv_ip=self.apple_tv_ip,
        )

        self._target_ip = ipaddress.ip_address(self.apple_tv_ip) if self.apple_tv_ip else None
        self._state_change_callback: StateChangeCallback | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._aiozc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None

        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending_watch_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    @property
    def _now(self) -> float:
        return time.time()

    async def start(self) -> None:
        if not self.apple_tv_ip:
            self.state.last_error = "apple_tv_ip_missing"
            logger.info("apple_tv_airplay disabled: APPLE_TV_IP is empty")
            return

        self._loop = asyncio.get_running_loop()
        self._stopped.clear()

        self._aiozc = AsyncZeroconf()
        self._browser = AsyncServiceBrowser(
            self._aiozc.zeroconf,
            AIRPLAY_SERVICE_TYPE,
            handlers=[self._on_service_state_change],
        )

        self.state.ready = True
        logger.info(
            "apple_tv_airplay started ip=%s debounce_s=%s service=%s",
            self.apple_tv_ip,
            self.debounce_s,
            AIRPLAY_SERVICE_TYPE,
        )

    async def stop(self) -> None:
        self._stopped.set()

        if self._pending_watch_task is not None:
            self._pending_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pending_watch_task
            self._pending_watch_task = None

        for task in list(self._tasks):
            task.cancel()

        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._tasks.clear()

        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.async_cancel()
            self._browser = None

        if self._aiozc is not None:
            with contextlib.suppress(Exception):
                await self._aiozc.async_close()
            self._aiozc = None

        self.state.ready = False
        logger.info("apple_tv_airplay stopped")

    def set_state_change_callback(self, callback: StateChangeCallback | None) -> None:
        self._state_change_callback = callback

    def snapshot(self) -> dict[str, Any]:
        age_s: float | None = None
        if self.state.last_update_ts is not None:
            age_s = max(0.0, self._now - self.state.last_update_ts)

        return {
            "backend": "apple_tv_airplay",
            "configured": self.state.configured,
            "enabled": self.state.enabled,
            "ready": self.state.ready,
            "reachable": self.state.reachable,
            "apple_tv_ip": self.state.apple_tv_ip,
            "service_name": self.state.service_name,
            "flags": None if self.state.flags is None else f"0x{self.state.flags:x}",
            "connected_session": self.state.connected_session,
            "last_update_ts": self.state.last_update_ts,
            "update_age_s": age_s,
            "last_emit_ts": self.state.last_emit_ts,
            "last_error": self.state.last_error,
        }

    # zeroconf calls this synchronously; schedule async work onto our loop.
    def _on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        del zeroconf

        if self._loop is None or self._stopped.is_set():
            return

        if state_change == ServiceStateChange.Removed:
            self._loop.call_soon_threadsafe(
                self._create_task,
                self._handle_service_removed(service_type, name),
                f"apple_tv_airplay:removed:{name}",
            )
            return

        if state_change in {ServiceStateChange.Added, ServiceStateChange.Updated}:
            self._loop.call_soon_threadsafe(
                self._create_task,
                self._resolve_service(service_type, name),
                f"apple_tv_airplay:resolve:{name}",
            )

    def _create_task(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def _done(done_task: asyncio.Task[Any]) -> None:
            self._tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.state.last_error = str(exc)
                logger.exception("apple_tv_airplay task failed name=%s", name)

        task.add_done_callback(_done)

    async def _resolve_service(self, service_type: str, name: str) -> None:
        if self._aiozc is None or self._target_ip is None:
            return

        info = AsyncServiceInfo(service_type, name)
        ok = await info.async_request(self._aiozc.zeroconf, 1500)
        if not ok:
            return

        if not self._service_matches_target_ip(info):
            return

        flags = self._extract_flags(info.properties)
        if flags is None:
            self.state.last_error = "airplay_flags_missing"
            logger.debug("apple_tv_airplay matched ip but no flags service=%s", name)
            return

        connected_session = bool(flags & APPLE_TV_CONNECTED_SESSION_BIT)

        self.state.reachable = True
        self.state.service_name = name
        self.state.flags = flags
        self.state.connected_session = connected_session
        self.state.last_update_ts = self._now
        self.state.last_error = None

        logger.debug(
            "apple_tv_airplay update service=%s ip=%s flags=0x%x connected_session=%s",
            name,
            self.apple_tv_ip,
            flags,
            connected_session,
        )

        if connected_session:
            self._schedule_watch_signal(flags=flags, service_name=name)
        else:
            self._cancel_pending_watch_signal()

    async def _handle_service_removed(self, service_type: str, name: str) -> None:
        del service_type

        if self.state.service_name != name:
            return

        self.state.reachable = False
        self.state.connected_session = False
        self.state.last_update_ts = self._now
        self._cancel_pending_watch_signal()

        logger.debug("apple_tv_airplay service removed service=%s", name)

    def _service_matches_target_ip(self, info: AsyncServiceInfo) -> bool:
        if self._target_ip is None:
            return False

        for raw in info.addresses:
            try:
                if ipaddress.ip_address(raw) == self._target_ip:
                    return True
            except ValueError:
                continue

        return False

    @staticmethod
    def _extract_flags(properties: dict[bytes, bytes | None]) -> int | None:
        raw = properties.get(b"flags")
        if raw is None:
            return None

        if isinstance(raw, bytes):
            text = raw.decode("ascii", errors="ignore").strip()
        else:
            text = str(raw).strip()

        if not text:
            return None

        try:
            return int(text, 0)
        except ValueError:
            logger.debug("apple_tv_airplay invalid flags value=%r", text)
            return None

    def _schedule_watch_signal(self, *, flags: int, service_name: str) -> None:
        # Debounce: active session must still be true after debounce_s.
        # Repeated active updates may reschedule this; that is fine and avoids
        # triggering on transient or incorrect AirPlay target selections.
        self._cancel_pending_watch_signal()

        self._pending_watch_task = asyncio.create_task(
            self._emit_watch_after_debounce(flags=flags, service_name=service_name),
            name="apple_tv_airplay:watch_debounce",
        )

    def _cancel_pending_watch_signal(self) -> None:
        if self._pending_watch_task is not None and not self._pending_watch_task.done():
            self._pending_watch_task.cancel()
        self._pending_watch_task = None

    async def _emit_watch_after_debounce(self, *, flags: int, service_name: str) -> None:
        try:
            await asyncio.sleep(self.debounce_s)

            latest_flags = self.state.flags
            if latest_flags is None:
                return

            if not bool(latest_flags & APPLE_TV_CONNECTED_SESSION_BIT):
                return

            if not self.state.connected_session:
                return

            callback = self._state_change_callback
            if callback is None:
                return

            payload = {
                "domain": "apple_tv_airplay",
                "source": "mdns_airplay",
                "apple_tv_ip": self.apple_tv_ip,
                "service_name": service_name,
                "flags": f"0x{latest_flags:x}",
                "connected_session": True,
                "debounce_s": self.debounce_s,
            }

            self.state.last_emit_ts = self._now
            logger.info(
                "apple_tv_airplay emitting watch signal ip=%s flags=0x%x",
                self.apple_tv_ip,
                latest_flags,
            )
            await callback("watch", payload)

        except asyncio.CancelledError:
            return
        finally:
            if self._pending_watch_task is asyncio.current_task():
                self._pending_watch_task = None