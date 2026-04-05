"""Local Samsung soundbar backend using Google Cast + AirPlay /info."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import plistlib
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
import pychromecast

logger = logging.getLogger(__name__)
logging.getLogger("pychromecast.discovery").setLevel(logging.ERROR)

DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"
AIRPLAY_PORT = 45167
AIRPLAY_ACTIVE_BIT = 0x800

HTTP_TIMEOUT_S = 3.0

VOLUME_STEP = 0.01
CAST_DISCOVERY_TIMEOUT_S = 5

def _now() -> float:
    return time.time()


@dataclass
class SamsungSoundbarLocalState:
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

    last_update_ts: float | None = None


class SamsungSoundbarLocal:
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

        self._state = SamsungSoundbarLocalState()
        self._session: aiohttp.ClientSession | None = None

        self._cast = None
        self._cast_browser = None
        self._cast_uuid = None
        self._cast_friendly_name = None
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
    def state(self) -> SamsungSoundbarLocalState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        s = self._state
        now_i = int(time.time())
        last_i = None if s.last_update_ts is None else int(s.last_update_ts)
        age_i = None if last_i is None else max(0, now_i - last_i)
        return {
            "backend": "samsung_soundbar_local",
            "reachable": bool(s.reachable),
            "connected": bool(s.connected),
            "ready": bool(s.ready),
            "playback_status": s.playback_status,
            "volume_pct": None if s.volume is None else int(round(s.volume * 100)),
            "muted": s.muted,
            "source": s.source,
            "last_update_ts": last_i,
            "update_age_s": age_i,
            "power_on": s.power_on,
            "raw_input_source": s.raw_input_source,
            "sound_from": s.sound_from,
            "listen_active": s.listen_active,
            "cast_app_id": s.cast_app_id,
            "cast_app_name": s.cast_app_name,
            "airplay_flags": s.airplay_flags,
        }

    async def start(self) -> None:
        if self._enabled:
            return

        if not self._speaker_ip:
            raise RuntimeError("speaker_ip_missing")

        self._enabled = True
        self._stop_evt.clear()
        self._poll_wake_evt.clear()

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)

        logger.info("initialised local samsung soundbar backend speaker_ip=%s", self._speaker_ip)
        self._task = asyncio.create_task(
            self._runner(),
            name=f"samsung_soundbar_local[{self._speaker_ip}]",
        )

    async def stop(self) -> None:
        self._enabled = False
        self._stop_evt.set()
        self._poll_wake_evt.set()

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

        await self._disconnect_cast()

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def request_refresh(self) -> None:
        if not self._enabled:
            return

        if self._refresh_lock.locked():
            self._poll_wake_evt.set()
            return

        async with self._refresh_lock:
            self._poll_wake_evt.set()
            await self._refresh_now()

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
                "SamsungSoundbarLocal unavailable speaker_ip=%s error=%s",
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
                self._state.reachable = False
                self._state.connected = False
                self._state.ready = False
                self._state.last_error = str(exc)
                self._state.last_update_ts = _now()

                self._note_refresh_failure(exc)

                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=self._poll_interval_s)
                except asyncio.TimeoutError:
                    pass

    async def _refresh_now(self) -> None:
        old_listen = bool(self._state.listen_active)

        airplay_flags = await self._read_airplay_flags()
        await self._ensure_cast()
        cast_status = await self._read_cast_status()

        self._apply_status(airplay_flags=airplay_flags, cast_status=cast_status)

        if not old_listen and self._state.listen_active:
            self._emit_state_change(
                "listen",
                {
                    "domain": "speaker",
                    "source": self._state.source,
                    "playback_status": self._state.playback_status,
                },
            )

    async def _ensure_cast(self) -> None:
        if self._cast is not None:
            return

        cast, browser = await asyncio.to_thread(self._connect_cast_blocking)
        self._cast = cast
        self._cast_browser = browser

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
        self._cast = None
        self._cast_browser = None
        self._cast_connected_logged = False
        self._cast_ready_logged = False

        def _cleanup() -> None:
            with contextlib.suppress(Exception):
                if browser is not None:
                    browser.stop_discovery()
            with contextlib.suppress(Exception):
                if cast is not None:
                    cast.disconnect()

        if cast is not None or browser is not None:
            await asyncio.to_thread(_cleanup)

    def _connect_cast_blocking(self):
        # Preferred reconnect path: use cached stable identifiers with the
        # current public helper, while still seeding discovery with known_hosts.
        if self._cast_uuid is not None:
            casts, browser = pychromecast.get_listed_chromecasts(
                uuids=[self._cast_uuid],
                known_hosts=[self._speaker_ip],
                discovery_timeout=CAST_DISCOVERY_TIMEOUT_S,
            )
            if casts:
                cast = casts[0]
                cast.wait()
                return cast, browser

        if self._cast_friendly_name:
            casts, browser = pychromecast.get_listed_chromecasts(
                friendly_names=[self._cast_friendly_name],
                known_hosts=[self._speaker_ip],
                discovery_timeout=CAST_DISCOVERY_TIMEOUT_S,
            )
            if casts:
                cast = casts[0]
                cast.wait()
                return cast, browser

        # Bootstrap fallback: host-directed discovery path. Keep this because
        # your config is IP-based and it works across your VLAN setup.
        casts, browser = pychromecast.get_chromecasts(known_hosts=[self._speaker_ip])
        if not casts:
            raise RuntimeError(f"cast_not_found:{self._speaker_ip}")

        cast = casts[0]
        cast.wait()

        self._cast_uuid = getattr(cast, "uuid", None)
        cast_info = getattr(cast, "cast_info", None)
        self._cast_friendly_name = (
            getattr(cast_info, "friendly_name", None) or getattr(cast, "name", None)
        )

        return cast, browser

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

    async def _read_airplay_flags(self) -> int | None:
        url = f"http://{self._speaker_ip}:{AIRPLAY_PORT}/info"

        def _fetch() -> int | None:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                payload = resp.read()
            data = plistlib.loads(payload)
            value = data.get("statusFlags")
            if isinstance(value, int):
                return value
            return None

        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            logger.debug("airplay /info read failed speaker_ip=%s", self._speaker_ip, exc_info=True)
            return None

    def _apply_status(self, *, airplay_flags: int | None, cast_status: dict[str, Any]) -> None:
        listen_active = bool(
            isinstance(airplay_flags, int) and (airplay_flags & AIRPLAY_ACTIVE_BIT)
        )

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

        source = self._derive_source(
            listen_active=listen_active,
            cast_app_id=app_id,
        )

        playback_status = self._derive_playback_status(
            listen_active=listen_active,
            cast_app_id=app_id,
        )

        self._state.reachable = True
        self._state.connected = True
        self._state.ready = True
        self._state.last_error = None

        self._state.friendly_name = friendly_name
        self._state.cast_app_id = app_id
        self._state.cast_app_name = app_name
        self._state.airplay_flags = airplay_flags

        if friendly_name:
            self._cast_friendly_name = friendly_name

        cast_obj = self._cast
        cast_uuid = getattr(cast_obj, "uuid", None) if cast_obj is not None else None
        if cast_uuid is not None:
            self._cast_uuid = cast_uuid

        self._state.volume = volume_norm
        self._state.muted = muted_norm
        self._state.listen_active = listen_active
        self._state.source = source
        self._state.playback_status = playback_status

        self._state.power_on = True if (listen_active or app_id) else None
        self._state.raw_input_source = app_id
        self._state.sound_from = "airplay" if listen_active else ("google_cast" if app_id else None)

        self._state.last_update_ts = _now()

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
                name=f"samsung_soundbar_local_state_change:{name}",
            )
        except Exception:
            logger.exception(
                "SamsungSoundbarLocal state change callback spawn failed name=%s",
                name,
            )
            return

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug(
                    "SamsungSoundbarLocal state change callback cancelled name=%s",
                    name,
                )
            except Exception:
                logger.exception(
                    "SamsungSoundbarLocal state change callback failed name=%s",
                    name,
                )

        task.add_done_callback(_done)

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        self._spawn_state_change_callback(name, payload)

    # ---- Controls ----

    async def power_on(self) -> None:
        # Intentionally not used for this backend.
        logger.debug("SamsungSoundbarLocal power_on noop")
        await self.request_refresh()

    async def power_off(self) -> None:
        # Intentionally not used for this backend.
        logger.debug("SamsungSoundbarLocal power_off noop")
        await self.request_refresh()

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