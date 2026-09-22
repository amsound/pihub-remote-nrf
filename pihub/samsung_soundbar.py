"""Local Samsung soundbar backend using Google Cast + AirPlay mDNS."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

import socket
from urllib.parse import quote

import aiohttp
import pychromecast

logger = logging.getLogger(__name__)
logging.getLogger("pychromecast.discovery").setLevel(logging.ERROR)

DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"

HLS_CONTENT_TYPE = "application/vnd.apple.mpegurl"

# The restreamer (github.com/amsound/restreamer) runs on the same host as
# pihub. The soundbar fetches from it directly, so it is addressed by this
# host's LAN IP. Restreamed slots are passed through as AAC.
RESTREAMER_PORT = 8000
RESTREAMER_FMT = "adts"
RESTREAMER_CONTENT_TYPE = "audio/aac"

# Server Content-Type → what to tell the Cast receiver.
_CAST_CONTENT_TYPES = {
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/aac": "audio/aac",
    "audio/aacp": "audio/aac",
    "audio/x-aac": "audio/aac",
    "audio/mp4": "audio/mp4",
    "audio/ogg": "audio/ogg",
    "application/ogg": "audio/ogg",
    "audio/flac": "audio/flac",
    "audio/x-flac": "audio/flac",
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
}
# Playlists the receiver cannot play itself; those slots need restreaming.
_PLAYLIST_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/x-mpegurl",
    "audio/mpegurl",
    "audio/x-scpls",
}
AIRPLAY_ACTIVE_BIT = 0x800

# Buffering is part of playing a live stream; reporting it separately would make
# automations flap every time the receiver tops up its buffer.
CAST_PLAYING_STATES = ("PLAYING", "BUFFERING")

HTTP_TIMEOUT_S = 3.0
AIRPLAY_SERVICE_TYPE = "_airplay._tcp.local."
AIRPLAY_MDNS_RESOLVE_TIMEOUT_MS = 2000

VOLUME_STEP = 0.01
CAST_DISCOVERY_TIMEOUT_S = 5
CAST_WATCHDOG_INTERVAL_S = 60.0


def _local_ip_for(host: str) -> str | None:
    """Best-effort source IP this machine would use to reach `host`."""
    for target in (host, "8.8.8.8"):
        if not target:
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((target, 80))
                ip = str(s.getsockname()[0])
            finally:
                s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
    return None


def _guess_content_type(url: str) -> str:
    """Guess a Cast content type from a media URL's extension."""
    path = url.lower().split("?")[0]
    if path.endswith(".m3u8"):
        return HLS_CONTENT_TYPE
    if path.endswith(".mp3"):
        return "audio/mpeg"
    if path.endswith((".aac", ".aacp")):
        return "audio/aac"
    if path.endswith(".ogg"):
        return "audio/ogg"
    return "audio/aac"  # reasonable default for live radio


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


class _CastMediaListener:
    """Receives pychromecast media status updates (playing / paused / idle).

    Bound to the cast it was registered on: this pychromecast version cannot
    unregister media listeners, so events from a replaced connection are
    ignored rather than applied.
    """

    def __init__(self, owner: "SamsungSoundbar", cast: Any) -> None:
        self._owner = owner
        self._cast = cast

    def new_media_status(self, status: Any) -> None:
        self._owner._cast_media_event_from_thread(self._cast, status)

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        logger.warning("cast load_media_failed item=%s error_code=%s", queue_item_id, error_code)


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
    # Receiver media state: PLAYING / BUFFERING / PAUSED / IDLE, or None.
    cast_player_state: str | None = None

    airplay_flags: int | None = None
    airplay_device: str | None = None

    last_update_ts: float | None = None


class SamsungSoundbar:
    def __init__(
        self,
        *,
        speaker_ip: str,
        command_interval_s: float = 0.10,
        tv: Any = None,
        state_change_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._speaker_ip = (speaker_ip or "").strip()
        self._command_interval_s = max(0.05, float(command_interval_s))

        self._enabled = False
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._watchdog_wake_evt = asyncio.Event()
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
        self._watchdog_wake_evt.clear()

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)

        self._start_airplay_mdns()

        logger.debug("started speaker_ip=%s", self._speaker_ip)
        self._task = asyncio.create_task(
            self._runner(),
            name=f"samsung_soundbar[{self._speaker_ip}]",
        )

    async def stop(self) -> None:
        self._enabled = False
        self._stop_evt.set()
        self._watchdog_wake_evt.set()

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

        self._watchdog_wake_evt.set()

        return {
            "ok": True,
            "outcome": "watchdog_woken",
            "errors": [],
        }

    def _note_refresh_success(self) -> None:
        if not self._cast_ready_logged:
            logger.info(
                "link ready speaker_ip=%s (initial status received)",
                self._speaker_ip,
            )
            self._cast_ready_logged = True

        if self._availability_logged_down:
            logger.info(
                "link restored speaker_ip=%s",
                self._speaker_ip,
            )

        self._availability_logged_down = False
        self._last_failure_key = None

    def _note_refresh_failure(self, exc: Exception) -> None:
        failure_key = f"{type(exc).__name__}:{exc}"

        if (not self._availability_logged_down) or (failure_key != self._last_failure_key):
            logger.warning(
                "unavailable speaker_ip=%s error=%s",
                self._speaker_ip,
                exc,
            )

        self._availability_logged_down = True
        self._last_failure_key = failure_key

    async def _runner(self) -> None:
        """Slow Cast watchdog.

        Live state is event-driven:
        - AirPlay source/activity comes from mDNS TXT updates.
        - Cast volume/mute comes from pychromecast status callbacks.

        This loop is only for recovery/reconcile:
        - establish the initial Cast connection/status
        - periodically check the Cast path still works
        - reconnect if pychromecast silently drops
        """
        while self._enabled and not self._stop_evt.is_set():
            try:
                await self._refresh_now()
                self._note_refresh_success()

                self._watchdog_wake_evt.clear()
                try:
                    await asyncio.wait_for(
                        self._watchdog_wake_evt.wait(),
                        timeout=CAST_WATCHDOG_INTERVAL_S,
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

                self._watchdog_wake_evt.clear()
                try:
                    await asyncio.wait_for(
                        self._watchdog_wake_evt.wait(),
                        timeout=CAST_WATCHDOG_INTERVAL_S,
                    )
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

        with contextlib.suppress(Exception):
            cast.media_controller.register_status_listener(_CastMediaListener(self, cast))

        if not self._cast_connected_logged:
            cast_info = getattr(cast, "cast_info", None)
            friendly_name = getattr(cast_info, "friendly_name", None) or getattr(cast, "name", None)
            logger.info(
                "connected speaker_ip=%s friendly_name=%s",
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

        logger.debug(
            "airplay listener started speaker_ip=%s service_type=%s",
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

    def _derived_state(
        self,
        *,
        airplay_flags: int | None,
        cast_app_id: str | None,
        cast_player_state: str | None,
    ) -> dict[str, Any]:
        """Fields that follow from AirPlay flags plus the Cast receiver state."""
        airplay_active = bool(isinstance(airplay_flags, int) and (airplay_flags & AIRPLAY_ACTIVE_BIT))
        cast_playing = bool(cast_app_id) and cast_player_state in CAST_PLAYING_STATES

        if airplay_active:
            sound_from = "airplay"
        elif cast_app_id:
            sound_from = "google_cast"
        else:
            sound_from = None

        listen_active = airplay_active or cast_playing
        return {
            "listen_active": listen_active,
            "source": self._derive_source(airplay_active=airplay_active, cast_app_id=cast_app_id),
            "playback_status": self._derive_playback_status(
                airplay_active=airplay_active,
                cast_app_id=cast_app_id,
                cast_player_state=cast_player_state,
            ),
            "power_on": True if (listen_active or cast_app_id) else None,
            "raw_input_source": cast_app_id,
            "sound_from": sound_from,
        }

    def _apply_airplay_snapshot(
        self,
        *,
        airplay_flags: int | None,
        airplay_device: str | None,
    ) -> None:
        old_listen = bool(self._state.listen_active)

        changed = self._apply_state_updates(
            airplay_flags=airplay_flags,
            airplay_device=airplay_device,
            **self._derived_state(
                airplay_flags=airplay_flags,
                cast_app_id=self._state.cast_app_id,
                cast_player_state=self._state.cast_player_state,
            ),
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

        # A different (or no) app means any media state we held is stale.
        player_state = self._state.cast_player_state if app_id == self._state.cast_app_id else None

        if app_id != self._state.cast_app_id and self._state.ready:
            # Rare (play, stop, crash), and the timing is what matters when
            # matching a stop against other events.
            logger.info("cast app changed old_app=%s new_app=%s", self._state.cast_app_id, app_id)

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
            cast_player_state=player_state,
            volume=volume_norm,
            muted=muted_norm,
            **self._derived_state(
                airplay_flags=self._state.airplay_flags,
                cast_app_id=app_id,
                cast_player_state=player_state,
            ),
        )

        if changed:
            self._maybe_emit_listen_edge(old_listen)

    def _cast_media_event_from_thread(self, cast: Any, status: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        loop.call_soon_threadsafe(self._handle_cast_media_event, cast, status)

    def _handle_cast_media_event(self, cast: Any, status: Any) -> None:
        if not self._enabled or cast is not self._cast:
            return

        player_state = self._norm_str(getattr(status, "player_state", None))
        if player_state == "UNKNOWN":
            player_state = None

        was_playing = self._state.cast_player_state in CAST_PLAYING_STATES
        if was_playing and player_state not in CAST_PLAYING_STATES:
            idle_reason = getattr(status, "idle_reason", None)
            if player_state == "PAUSED":
                logger.info("cast playback paused")
            elif idle_reason == "INTERRUPTED":
                # A new load replaced the session (e.g. Listen pressed again).
                logger.info("cast playback replaced by a new load")
            elif player_state is None:
                # The session vanished without a reason: the receiver app quit.
                logger.info("cast playback lost: media session disappeared (receiver quit?)")
            else:
                # CANCELLED is a normal stop, ERROR is the receiver failing,
                # FINISHED means it thought the stream ended.
                logger.info(
                    "cast playback ended state=%s idle_reason=%s",
                    player_state,
                    idle_reason,
                )

        old_listen = bool(self._state.listen_active)
        changed = self._apply_state_updates(
            cast_player_state=player_state,
            **self._derived_state(
                airplay_flags=self._state.airplay_flags,
                cast_app_id=self._state.cast_app_id,
                cast_player_state=player_state,
            ),
        )

        if changed:
            logger.debug("cast media state=%s playback_status=%s", player_state, self._state.playback_status)
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

    def _derive_source(self, *, airplay_active: bool, cast_app_id: str | None) -> str | None:
        if airplay_active:
            return "airplay"

        if cast_app_id:
            return "wifi"

        # Optional weak hint from TV presence only.
        if self._tv is not None and getattr(self._tv, "_presence_cached", None) is True:
            return "hdmi"

        return None

    @staticmethod
    def _derive_playback_status(
        *,
        airplay_active: bool,
        cast_app_id: str | None,
        cast_player_state: str | None,
    ) -> str | None:
        if airplay_active:
            return "playing"
        if cast_app_id:
            if cast_player_state in CAST_PLAYING_STATES:
                return "playing"
            if cast_player_state == "PAUSED":
                return "paused"
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
                "state change callback spawn failed name=%s",
                name,
            )
            return

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug(
                    "state change callback cancelled name=%s",
                    name,
                )
            except Exception:
                logger.exception(
                    "state change callback failed name=%s",
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

    @staticmethod
    def _media_status(cast) -> Any:
        """Current media status for the running receiver app, or None."""
        mc = cast.media_controller
        with contextlib.suppress(Exception):
            mc.update_status()
        return getattr(mc, "status", None)

    async def play(self) -> None:
        def _cmd(cast) -> None:
            cast.media_controller.play()

        await self._cast_command(_cmd)

    async def pause(self) -> None:
        def _cmd(cast) -> None:
            status = self._media_status(cast)
            # Live radio is not always pausable; the receiver advertises this.
            if status is not None and not status.supports_pause:
                raise RuntimeError("unsupported_on_backend:pause")
            cast.media_controller.pause()

        await self._cast_command(_cmd)

    async def play_pause(self) -> None:
        """Toggle transport state on whatever the soundbar is casting.

        Note that live radio only has the short DVR window the stream
        publishes, so resuming after a long pause drops back to the live edge
        rather than continuing where it left off.
        """
        def _cmd(cast) -> None:
            mc = cast.media_controller
            status = self._media_status(cast)

            if status is None or status.player_is_idle:
                raise RuntimeError("unsupported_on_backend:play_pause_no_media")

            if status.player_is_playing:
                if not status.supports_pause:
                    raise RuntimeError("unsupported_on_backend:play_pause_not_pausable")
                mc.pause()
            else:
                mc.play()

        await self._cast_command(_cmd)

    async def next_track(self) -> None:
        # Live radio has no track list; declared so the remote's transport keys
        # degrade quietly instead of raising a speaker fault.
        raise RuntimeError("unsupported_on_backend:next_track")

    async def previous_track(self) -> None:
        raise RuntimeError("unsupported_on_backend:previous_track")

    async def stop_playback(self) -> None:
        """Stop whatever the soundbar is playing over the network.

        Our own radio runs inside the Default Media Receiver, so it is stopped
        through its media session and the app is then closed, which leaves the
        soundbar looking switched off. Relaunching the app does not work here:
        the receiver just keeps the running session.

        For anything else (AirPlay), launching the Default Media Receiver takes
        audio focus, which is what stops it.
        """
        def _cmd(cast) -> None:
            status = self._media_status(cast)
            cast_media_active = (
                cast.app_id == DEFAULT_MEDIA_RECEIVER_APP_ID
                and status is not None
                and status.player_state not in (None, "UNKNOWN", "IDLE")
            )

            if cast_media_active:
                cast.media_controller.stop()
                cast.quit_app()
                return

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

    async def play_url(
        self,
        url: str,
        content_type: str | None = None,
        restream: bool = False,
    ) -> None:
        """Cast a stream to the soundbar.

        restream sends it via the local restreamer, which turns HLS, playlists
        and TuneIn stations into one steady AAC stream this receiver can hold
        onto. Otherwise the URL is cast as-is, typed from the server's own
        Content-Type because plenty of radio URLs have no file extension.
        """
        if not url:
            raise RuntimeError("play_url_missing")

        if restream:
            cast_url = self._restreamer_url(url)
            content_type = RESTREAMER_CONTENT_TYPE
        else:
            cast_url = url
            if not content_type:
                content_type = await self._probe_content_type(url) or _guess_content_type(url)

        logger.debug("cast play_url content_type=%s url=%s", content_type, cast_url)

        def _cmd(cast) -> None:
            cast.media_controller.play_media(
                cast_url,
                content_type,
                stream_type="LIVE",
            )
            cast.media_controller.block_until_active(timeout=5)

        await self._cast_command(_cmd)
        if restream:
            logger.info("cast play_url url=%s via restreamer", url)
        else:
            logger.info("cast play_url url=%s content_type=%s", url, content_type)

    def _restreamer_url(self, src: str) -> str:
        host_ip = _local_ip_for(self._speaker_ip)
        if not host_ip:
            raise RuntimeError("restreamer_host_ip_unknown")
        return (
            f"http://{host_ip}:{RESTREAMER_PORT}/play"
            f"?src={quote(src, safe='')}&fmt={RESTREAMER_FMT}"
        )

    async def _probe_content_type(self, url: str) -> str | None:
        """Read the stream's Content-Type without downloading it."""
        session = self._session
        owned = session is None or session.closed
        if owned:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S))
        try:
            async with session.get(url, headers={"Icy-MetaData": "0"}) as resp:
                raw = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        except Exception as exc:
            logger.debug("content-type probe failed url=%s: %s", url, exc)
            return None
        finally:
            if owned:
                await session.close()

        if raw in _PLAYLIST_CONTENT_TYPES:
            logger.warning(
                "url=%s is a playlist (%s); the soundbar cannot play it directly, "
                "tick Restream for this slot",
                url,
                raw,
            )
            return HLS_CONTENT_TYPE if "mpegurl" in raw else None
        return _CAST_CONTENT_TYPES.get(raw)
