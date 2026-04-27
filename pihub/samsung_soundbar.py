"""Local Samsung soundbar backend using Google Cast + AirPlay mDNS."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

import aiohttp
import pychromecast

logger = logging.getLogger(__name__)
logging.getLogger("pychromecast.discovery").setLevel(logging.ERROR)

DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"
AIRPLAY_ACTIVE_BIT = 0x800

HTTP_TIMEOUT_S = 3.0
AIRPLAY_SERVICE_TYPE = "_airplay._tcp.local."
AIRPLAY_MDNS_RESOLVE_TIMEOUT_MS = 2000

VOLUME_STEP = 0.01
CAST_DISCOVERY_TIMEOUT_S = 5


def _now() -> float:
    return time.time()


class _AirPlayServiceListener(ServiceListener):
    """Long-lived AirPlay mDNS listener.

    Zeroconf invokes these callbacks from its own thread, so this class only
    forwards the event into SamsungSoundbar via call_soon_threadsafe().
    """

    def __init__(self, owner: "SamsungSoundbar") -> None:
        self._owner = owner

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._owner._airplay_mdns_event_from_thread(zc, type_, name, removed=False)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._owner._airplay_mdns_event_from_thread(zc, type_, name, removed=False)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._owner._airplay_mdns_event_from_thread(zc, type_, name, removed=True)

class _CastStatusListener:
    """Receives pychromecast receiver status updates.

    pychromecast invokes this from its own thread. Forward into the asyncio loop.
    """

    def __init__(self, owner: "SamsungSoundbar") -> None:
        self._owner = owner

    def new_cast_status(self, status: Any) -> None:
        self._owner._cast_status_event_from_thread(status)


@dataclass
class SamsungSoundbarState:
    reachable: bool = False
    connected: bool = False
    ready: bool = False
    last_error: str | None = None

    power_on: bool | None = None
    playback_status: str | None = None
    volume: float | None = None  # 0..1
    muted: bool | None = None

    source: str | None = None
    raw_input_source: str | None = None
    sound_from: str | None = None
    listen_active: bool = False

    friendly_name: str | None = None
    cast_app_id: str | None = None
    cast_app_name: str | None = None

    airplay_flags: int | None = None
    airplay_device: str | None = None

    last_update_ts: float | None = None


class SamsungSoundbar:
    def __init__(
        self,
        *,
        speaker_ip: str,
        poll_interval_s: float = 5.0,
        command_interval_s: float = 0.10,
        tv: Any = None,
        state_change_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._speaker_ip = (speaker_ip or "").strip()
        self._poll_interval_s = max(2.0, float(poll_interval_s))
        self._command_interval_s = max(0.05, float(command_interval_s))

        self._enabled = False
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._poll_wake_evt = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

        self._state = SamsungSoundbarState()
        self._session: aiohttp.ClientSession | None = None

        self._loop: asyncio.AbstractEventLoop | None = None

        self._cast = None
        self._cast_browser = None
        self._cast_uuid = None
        self._cast_friendly_name = None
        self._cast_status_listener: _CastStatusListener | None = None

        self._airplay_zc: Zeroconf | None = None
        self._airplay_browser: ServiceBrowser | None = None
        self._airplay_listener: _AirPlayServiceListener | None = None
        self._airplay_service_raw_name: str | None = None

        self._last_send_monotonic = 0.0

        self._availability_logged_down = False
        self._last_failure_key = None

        self._cast_connected_logged = False
        self._cast_ready_logged = False

        self._tv = tv
        self._state_change_callback = state_change_callback

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> SamsungSoundbarState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        s = self._state
        now_i = int(time.time())
        last_i = None if s.last_update_ts is None else int(s.last_update_ts)
        age_i = None if last_i is None else max(0, now_i - last_i)
        return {
            "backend": "samsung_soundbar",
            "reachable": bool(s.reachable),
            "connected": bool(s.connected),
            "ready": bool(s.ready),
            "playback_status": s.playback_status,
            "volume_pct": None if s.volume is None else int(round(s.volume * 100)),
            "muted": s.muted,
            "source": s.source,
            "listen_active": s.listen_active,
            "last_update_ts": last_i,
            "update_age_s": age_i,
            "airplay_device": s.airplay_device,
        }

    async def start(self) -> None:
        if self._enabled:
            return

        if not self._speaker_ip:
            raise RuntimeError("speaker_ip_missing")

        self._enabled = True
        self._loop = asyncio.get_running_loop()
        self._stop_evt.clear()
        self._poll_wake_evt.clear()

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)

        self._start_airplay_mdns()

        logger.info("initialised local samsung soundbar backend speaker_ip=%s", self._speaker_ip)
        self._task = asyncio.create_task(
            self._runner(),
            name=f"samsung_soundbar[{self._speaker_ip}]",
        )

    async def stop(self) -> None:
        self._enabled = False
        self._stop_evt.set()
        self._poll_wake_evt.set()

        self._stop_airplay_mdns()

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

        await self._disconnect_cast()

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._loop = None

    async def request_refresh(self) -> dict[str, Any]:
        if not self._enabled:
            return {
                "ok": False,
                "outcome": "unavailable",
                "errors": ["speaker_disabled"],
            }

        if self._refresh_lock.locked():
            self._poll_wake_evt.set()
            return {
                "ok": True,
                "outcome": "refresh_coalesced",
                "errors": [],
            }

        errors: list[str] = []

        try:
            async with self._refresh_lock:
                self._poll_wake_evt.set()
                await self._refresh_now()
        except Exception as exc:
            self._apply_state_updates(
                reachable=False,
                connected=False,
                ready=False,
                last_error=str(exc),
            )
            self._note_refresh_failure(exc)
            errors.append(f"refresh_failed:{exc!r}")
            return {
                "ok": False,
                "outcome": "refresh_failed",
                "errors": errors,
            }

        self._note_refresh_success()
        return {
            "ok": True,
            "outcome": "refresh_requested",
            "errors": errors,
        }

    def _note_refresh_success(self) -> None:
        if not self._cast_ready_logged:
            logger.info(
                "speaker link ready speaker_ip=%s (initial status received)",
                self._speaker_ip,
            )
            self._cast_ready_logged = True

        if self._availability_logged_down:
            logger.info(
                "speaker link restored speaker_ip=%s",
                self._speaker_ip,
            )

        self._availability_logged_down = False
        self._last_failure_key = None

    def _note_refresh_failure(self, exc: Exception) -> None:
        failure_key = f"{type(exc).__name__}:{exc}"

        if (not self._availability_logged_down) or (failure_key != self._last_failure_key):
            logger.warning(
                "SamsungSoundbar unavailable speaker_ip=%s error=%s",
                self._speaker_ip,
                exc,
            )

        self._availability_logged_down = True
        self._last_failure_key = failure_key

    async def _runner(self) -> None:
        while self._enabled and not self._stop_evt.is_set():
            try:
                await self._refresh_now()
                self._note_refresh_success()

                self._poll_wake_evt.clear()
                try:
                    await asyncio.wait_for(
                        self._poll_wake_evt.wait(),
                        timeout=self._poll_interval_s,
                    )
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._apply_state_updates(
                    reachable=False,
                    connected=False,
                    ready=False,
                    last_error=str(exc),
                )

                self._note_refresh_failure(exc)

                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=self._poll_interval_s)
                except asyncio.TimeoutError:
                    pass

    async def _refresh_now(self) -> None:
        await self._ensure_cast()
        cast_status = await self._read_cast_status()
        self._apply_cast_snapshot(cast_status)

    async def _ensure_cast(self) -> None:
        if self._cast is not None:
            return

        cast, browser = await asyncio.to_thread(self._connect_cast_blocking)
        self._cast = cast
        self._cast_browser = browser

        if self._cast_status_listener is None:
            self._cast_status_listener = _CastStatusListener(self)

        with contextlib.suppress(Exception):
            cast.register_status_listener(self._cast_status_listener)

        if not self._cast_connected_logged:
            cast_info = getattr(cast, "cast_info", None)
            friendly_name = getattr(cast_info, "friendly_name", None) or getattr(cast, "name", None)
            logger.info(
                "speaker connected speaker_ip=%s friendly_name=%s",
                self._speaker_ip,
                friendly_name or "unknown",
            )
            self._cast_connected_logged = True

    async def _disconnect_cast(self) -> None:
        cast = self._cast
        browser = self._cast_browser
        listener = self._cast_status_listener

        self._cast = None
        self._cast_browser = None
        self._cast_connected_logged = False
        self._cast_ready_logged = False

        def _cleanup() -> None:
            with contextlib.suppress(Exception):
                if cast is not None and listener is not None:
                    cast.unregister_status_listener(listener)
            with contextlib.suppress(Exception):
                if browser is not None:
                    browser.stop_discovery()
            with contextlib.suppress(Exception):
                if cast is not None:
                    cast.disconnect()

        if cast is not None or browser is not None:
            await asyncio.to_thread(_cleanup)

    def _connect_cast_blocking(self):
        # Configured IP is authoritative. pychromecast known_hosts helps discovery,
        # but it does not guarantee returned casts are limited to that host.
        # Always filter candidates by host before accepting one.

        if self._cast_uuid is not None:
            casts, browser = pychromecast.get_listed_chromecasts(
                uuids=[self._cast_uuid],
                known_hosts=[self._speaker_ip],
                discovery_timeout=CAST_DISCOVERY_TIMEOUT_S,
            )
            cast = self._select_configured_cast(casts)
            if cast is not None:
                cast.wait()
                return cast, browser

            with contextlib.suppress(Exception):
                browser.stop_discovery()

            logger.warning(
                "cached cast uuid did not match configured speaker_ip=%s; clearing cached uuid",
                self._speaker_ip,
            )
            self._cast_uuid = None

        if self._cast_friendly_name:
            casts, browser = pychromecast.get_listed_chromecasts(
                friendly_names=[self._cast_friendly_name],
                known_hosts=[self._speaker_ip],
                discovery_timeout=CAST_DISCOVERY_TIMEOUT_S,
            )
            cast = self._select_configured_cast(casts)
            if cast is not None:
                cast.wait()
                return cast, browser

            with contextlib.suppress(Exception):
                browser.stop_discovery()

            logger.warning(
                "cached cast friendly_name did not match configured speaker_ip=%s cached_name=%s; clearing cached name",
                self._speaker_ip,
                self._cast_friendly_name,
            )
            self._cast_friendly_name = None

        casts, browser = pychromecast.get_chromecasts(known_hosts=[self._speaker_ip])
        cast = self._select_configured_cast(casts)
        if cast is None:
            with contextlib.suppress(Exception):
                browser.stop_discovery()

            names = []
            for candidate in casts:
                cast_info = getattr(candidate, "cast_info", None)
                names.append(
                    {
                        "host": self._cast_host(candidate),
                        "friendly_name": (
                            getattr(cast_info, "friendly_name", None)
                            or getattr(candidate, "name", None)
                        ),
                    }
                )

            raise RuntimeError(
                f"cast_not_found_for_configured_ip:{self._speaker_ip}:candidates={names!r}"
            )

        cast.wait()

        self._cast_uuid = getattr(cast, "uuid", None)
        cast_info = getattr(cast, "cast_info", None)
        self._cast_friendly_name = (
            getattr(cast_info, "friendly_name", None) or getattr(cast, "name", None)
        )

        return cast, browser
    
    @staticmethod
    def _cast_host(cast: Any) -> str | None:
        cast_info = getattr(cast, "cast_info", None)

        for value in (
            getattr(cast_info, "host", None),
            getattr(cast, "host", None),
            getattr(getattr(cast, "socket_client", None), "host", None),
        ):
            text = SamsungSoundbar._norm_str(value)
            if text:
                return text

        return None

    def _select_configured_cast(self, casts: list[Any]) -> Any | None:
        for cast in casts:
            host = self._cast_host(cast)
            if host == self._speaker_ip:
                return cast

        for cast in casts:
            cast_info = getattr(cast, "cast_info", None)
            logger.debug(
                "ignoring cast candidate speaker_ip=%s candidate_host=%s friendly_name=%s uuid=%s",
                self._speaker_ip,
                self._cast_host(cast) or "unknown",
                getattr(cast_info, "friendly_name", None) or getattr(cast, "name", None) or "unknown",
                getattr(cast, "uuid", None) or "unknown",
            )

        return None

    async def _read_cast_status(self) -> dict[str, Any]:
        if self._cast is None:
            raise RuntimeError("cast_not_connected")

        def _read() -> dict[str, Any]:
            cast = self._cast
            if cast is None:
                raise RuntimeError("cast_not_connected")

            status = getattr(cast, "status", None)
            cast_info = getattr(cast, "cast_info", None)

            return {
                "friendly_name": getattr(cast_info, "friendly_name", None) or getattr(cast, "name", None),
                "app_id": getattr(cast, "app_id", None),
                "app_name": getattr(cast, "app_display_name", None),
                "volume": getattr(status, "volume_level", None),
                "muted": getattr(status, "volume_muted", None),
            }

        try:
            return await asyncio.to_thread(_read)
        except Exception:
            await self._disconnect_cast()
            raise

    def _cast_status_event_from_thread(self, status: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        loop.call_soon_threadsafe(self._handle_cast_status_event, status)


    def _handle_cast_status_event(self, status: Any) -> None:
        if not self._enabled:
            return

        cast = self._cast
        cast_info = getattr(cast, "cast_info", None) if cast is not None else None

        cast_status = {
            "friendly_name": (
                getattr(cast_info, "friendly_name", None)
                or getattr(cast, "name", None)
                if cast is not None
                else None
            ),
            "app_id": getattr(cast, "app_id", None) if cast is not None else None,
            "app_name": getattr(cast, "app_display_name", None) if cast is not None else None,
            "volume": getattr(status, "volume_level", None),
            "muted": getattr(status, "volume_muted", None),
        }

        self._apply_cast_snapshot(cast_status)

    async def _cast_command(self, fn) -> None:
        async with self._send_lock:
            now_m = time.monotonic()
            dt = now_m - self._last_send_monotonic
            if dt < self._command_interval_s:
                await asyncio.sleep(self._command_interval_s - dt)

            await self._ensure_cast()
            await asyncio.to_thread(fn, self._cast)
            self._last_send_monotonic = time.monotonic()

        await asyncio.sleep(0.35)
        await self.request_refresh()

    # ---- AirPlay mDNS ----

    def _start_airplay_mdns(self) -> None:
        if self._airplay_zc is not None:
            return

        zc = Zeroconf()
        listener = _AirPlayServiceListener(self)
        browser = ServiceBrowser(zc, AIRPLAY_SERVICE_TYPE, listener)

        self._airplay_zc = zc
        self._airplay_listener = listener
        self._airplay_browser = browser

        logger.info(
            "airplay mdns listener started speaker_ip=%s service_type=%s",
            self._speaker_ip,
            AIRPLAY_SERVICE_TYPE,
        )

    def _stop_airplay_mdns(self) -> None:
        browser = self._airplay_browser
        zc = self._airplay_zc

        self._airplay_browser = None
        self._airplay_listener = None
        self._airplay_zc = None

        with contextlib.suppress(Exception):
            if browser is not None:
                browser.cancel()
        with contextlib.suppress(Exception):
            if zc is not None:
                zc.close()

    def _airplay_mdns_event_from_thread(
        self,
        zc: Zeroconf,
        type_: str,
        name: str,
        *,
        removed: bool,
    ) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        if removed:
            loop.call_soon_threadsafe(self._handle_airplay_removed, name)
            return

        try:
            info = zc.get_service_info(
                type_,
                name,
                timeout=AIRPLAY_MDNS_RESOLVE_TIMEOUT_MS,
            )
        except Exception:
            logger.debug(
                "airplay mdns resolve failed speaker_ip=%s service=%s",
                self._speaker_ip,
                name,
                exc_info=True,
            )
            return

        if info is None:
            return

        addresses: list[str] = []
        with contextlib.suppress(Exception):
            addresses = list(info.parsed_addresses())

        if self._speaker_ip not in addresses:
            return

        flags = self._parse_airplay_txt_flags(getattr(info, "properties", None))
        device = self._clean_airplay_device_name(name)

        loop.call_soon_threadsafe(
            self._handle_airplay_update,
            name,
            device,
            flags,
            addresses,
        )

    def _handle_airplay_update(
        self,
        raw_name: str,
        device: str | None,
        flags: int | None,
        addresses: list[str],
    ) -> None:
        if not self._enabled:
            return

        self._airplay_service_raw_name = raw_name

        logger.debug(
            "airplay mdns update speaker_ip=%s device=%s flags=%s addresses=%s",
            self._speaker_ip,
            device or "unknown",
            None if flags is None else hex(flags),
            addresses,
        )

        self._apply_airplay_snapshot(
            airplay_flags=flags,
            airplay_device=device,
        )

    def _handle_airplay_removed(self, raw_name: str) -> None:
        if raw_name != self._airplay_service_raw_name:
            return

        logger.info(
            "airplay mdns service removed speaker_ip=%s service=%s",
            self._speaker_ip,
            raw_name,
        )

        self._airplay_service_raw_name = None
        self._apply_airplay_snapshot(
            airplay_flags=None,
            airplay_device=self._state.airplay_device,
        )

    @staticmethod
    def _parse_airplay_txt_flags(properties: Any) -> int | None:
        if not isinstance(properties, dict):
            return None

        raw = None
        for key in (b"flags", "flags"):
            if key in properties:
                raw = properties[key]
                break

        if raw is None:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="ignore")

        text = str(raw).strip()
        if not text:
            return None

        try:
            return int(text, 0)
        except ValueError:
            logger.debug("airplay mdns flags parse failed raw=%r", raw)
            return None

    @staticmethod
    def _clean_airplay_device_name(name: Any) -> str | None:
        text = SamsungSoundbar._norm_str(name)
        if not text:
            return None

        suffix = "." + AIRPLAY_SERVICE_TYPE
        if text.endswith(suffix):
            text = text[: -len(suffix)]
        elif text.endswith(AIRPLAY_SERVICE_TYPE):
            text = text[: -len(AIRPLAY_SERVICE_TYPE)]

        text = text.rstrip(".").strip()

        # AirPlay/DNS-SD instance names often use hyphens where the displayed
        # device name uses spaces: Living-Room._airplay._tcp.local. -> Living Room.
        text = text.replace("-", " ")

        return text or None

    # ---- State application ----

    def _apply_state_updates(self, **updates: Any) -> bool:
        changed = False

        for key, value in updates.items():
            if not hasattr(self._state, key):
                raise AttributeError(f"unknown_state_field:{key}")

            if getattr(self._state, key) != value:
                setattr(self._state, key, value)
                changed = True

        if changed:
            self._state.last_update_ts = _now()

        return changed

    def _apply_airplay_snapshot(
        self,
        *,
        airplay_flags: int | None,
        airplay_device: str | None,
    ) -> None:
        old_listen = bool(self._state.listen_active)

        listen_active = bool(
            isinstance(airplay_flags, int) and (airplay_flags & AIRPLAY_ACTIVE_BIT)
        )

        source = self._derive_source(
            listen_active=listen_active,
            cast_app_id=self._state.cast_app_id,
        )
        playback_status = self._derive_playback_status(
            listen_active=listen_active,
            cast_app_id=self._state.cast_app_id,
        )

        changed = self._apply_state_updates(
            airplay_flags=airplay_flags,
            airplay_device=airplay_device,
            listen_active=listen_active,
            source=source,
            playback_status=playback_status,
            power_on=True if (listen_active or self._state.cast_app_id) else None,
            raw_input_source=self._state.cast_app_id,
            sound_from="airplay" if listen_active else ("google_cast" if self._state.cast_app_id else None),
        )

        if changed:
            self._maybe_emit_listen_edge(old_listen)

    def _apply_cast_snapshot(self, cast_status: dict[str, Any]) -> None:
        old_listen = bool(self._state.listen_active)

        app_id = self._norm_str(cast_status.get("app_id"))
        app_name = self._norm_str(cast_status.get("app_name"))
        friendly_name = self._norm_str(cast_status.get("friendly_name"))

        volume = cast_status.get("volume")
        if isinstance(volume, (int, float)):
            volume_norm = max(0.0, min(1.0, float(volume)))
        else:
            volume_norm = None

        muted = cast_status.get("muted")
        muted_norm = bool(muted) if isinstance(muted, bool) else None

        listen_active = bool(
            isinstance(self._state.airplay_flags, int)
            and (self._state.airplay_flags & AIRPLAY_ACTIVE_BIT)
        )

        source = self._derive_source(
            listen_active=listen_active,
            cast_app_id=app_id,
        )
        playback_status = self._derive_playback_status(
            listen_active=listen_active,
            cast_app_id=app_id,
        )

        if friendly_name:
            self._cast_friendly_name = friendly_name

        cast_obj = self._cast
        cast_uuid = getattr(cast_obj, "uuid", None) if cast_obj is not None else None
        if cast_uuid is not None:
            self._cast_uuid = cast_uuid

        changed = self._apply_state_updates(
            reachable=True,
            connected=True,
            ready=True,
            last_error=None,
            friendly_name=friendly_name,
            cast_app_id=app_id,
            cast_app_name=app_name,
            volume=volume_norm,
            muted=muted_norm,
            listen_active=listen_active,
            source=source,
            playback_status=playback_status,
            power_on=True if (listen_active or app_id) else None,
            raw_input_source=app_id,
            sound_from="airplay" if listen_active else ("google_cast" if app_id else None),
        )

        if changed:
            self._maybe_emit_listen_edge(old_listen)

    def _maybe_emit_listen_edge(self, old_listen: bool) -> None:
        if not old_listen and self._state.listen_active:
            self._emit_state_change(
                "listen",
                {
                    "domain": "speaker",
                    "source": self._state.source,
                    "playback_status": self._state.playback_status,
                },
            )

    @staticmethod
    def _norm_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _derive_source(self, *, listen_active: bool, cast_app_id: str | None) -> str | None:
        if listen_active:
            return "airplay"

        if cast_app_id:
            return "wifi"

        # Optional weak hint from TV presence only.
        if self._tv is not None and getattr(self._tv, "_presence_cached", None) is True:
            return "hdmi"

        return None

    @staticmethod
    def _derive_playback_status(*, listen_active: bool, cast_app_id: str | None) -> str | None:
        if listen_active:
            return "playing"
        if cast_app_id:
            return "idle"
        return None

    def _spawn_state_change_callback(self, name: str, payload: dict[str, Any]) -> None:
        cb = self._state_change_callback
        if cb is None:
            return

        try:
            task = asyncio.create_task(
                cb(name, payload),
                name=f"samsung_soundbar_state_change:{name}",
            )
        except Exception:
            logger.exception(
                "SamsungSoundbar state change callback spawn failed name=%s",
                name,
            )
            return

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug(
                    "SamsungSoundbar state change callback cancelled name=%s",
                    name,
                )
            except Exception:
                logger.exception(
                    "SamsungSoundbar state change callback failed name=%s",
                    name,
                )

        task.add_done_callback(_done)

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        self._spawn_state_change_callback(name, payload)

    # ---- Controls ----

    async def power_on(self) -> None:
        # Intentionally not used for this backend.
        raise RuntimeError("unsupported_on_backend:power_on")

    async def power_off(self) -> None:
        # Intentionally not used for this backend.
        raise RuntimeError("unsupported_on_backend:power_off")

    async def volume_up(self) -> None:
        cur = self._state.volume if self._state.volume is not None else 0.0
        target = min(1.0, cur + VOLUME_STEP)
        await self.set_volume(int(round(target * 100)))

    async def volume_down(self) -> None:
        cur = self._state.volume if self._state.volume is not None else 0.0
        target = max(0.0, cur - VOLUME_STEP)
        await self.set_volume(int(round(target * 100)))

    async def set_volume(self, pct: int) -> None:
        target = max(0.0, min(1.0, int(pct) / 100.0))

        def _cmd(cast) -> None:
            cast.set_volume(target)

        await self._cast_command(_cmd)

    async def set_muted(self, target: bool) -> None:
        def _cmd(cast) -> None:
            cast.set_volume_muted(bool(target))

        await self._cast_command(_cmd)

    async def mute_toggle(self) -> None:
        cur = bool(self._state.muted) if self._state.muted is not None else False
        await self.set_muted(not cur)

    async def play(self) -> None:
        raise RuntimeError("unsupported_on_backend:play")

    async def pause(self) -> None:
        raise RuntimeError("unsupported_on_backend:pause")

    async def stop_playback(self) -> None:
        def _cmd(cast) -> None:
            cast.start_app(DEFAULT_MEDIA_RECEIVER_APP_ID)

        await self._cast_command(_cmd)

    async def fast_forward(self) -> None:
        raise RuntimeError("unsupported_on_backend:fast_forward")

    async def rewind(self) -> None:
        raise RuntimeError("unsupported_on_backend:rewind")

    async def set_source(self, source: str) -> None:
        raise RuntimeError(f"unsupported_on_backend:set_source:{source}")

    async def preset(self, n: int) -> None:
        raise RuntimeError(f"unsupported_on_backend:preset:{n}")

    async def next_preset(self) -> None:
        raise RuntimeError("unsupported_on_backend:next_preset")

    async def previous_preset(self) -> None:
        raise RuntimeError("unsupported_on_backend:previous_preset")

    async def play_url(self, url: str) -> None:
        raise RuntimeError("unsupported_on_backend:play_url")