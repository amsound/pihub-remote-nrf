"""Samsung SmartThings soundbar controller."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

SMARTTHINGS_API_BASE = "https://api.smartthings.com/v1"
SMARTTHINGS_AUTH_BASE = "https://auth-global.api.smartthings.com/oauth/token"

HTTP_TIMEOUT_S = 10.0


def _now() -> float:
    return time.time()


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

    source: str | None = None          # derived source for PiHub compatibility
    raw_input_source: str | None = None
    sound_from: str | None = None
    listen_active: bool = False

    last_update_ts: float | None = None


class SmartThingsTokenStore:
    """
    Reads /data/smartthings-token.json.

    Supported shapes:
    1) Proper owned SmartApp file:
       {
         "access_token": "...",
         "refresh_token": "...",
         "expires_at": 1774098857.9,
         "client_id": "...",
         "client_secret": "..."
       }

    2) HA-style nested dump pasted directly:
       {
         "data": {
           "token": {
             "access_token": "...",
             "refresh_token": "...",
             "expires_at": 1774098857.9
           }
         }
       }

    If client_id/client_secret are absent, the current access token is used
    as-is and cannot be refreshed automatically.
    """

    def __init__(self, path: str = "/data/smartthings-token.json") -> None:
        self._path = Path(path)

    def _read_json(self) -> dict[str, Any]:
        text = self._path.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"empty SmartThings token file: {self._path}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid SmartThings token JSON: {self._path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"SmartThings token file must contain a JSON object: {self._path}")
        return data

    def _extract_token_block(self, data: dict[str, Any]) -> dict[str, Any]:
        token_block = data
        if isinstance(data.get("data"), dict) and isinstance(data["data"].get("token"), dict):
            token_block = data["data"]["token"]

        return {
            "access_token": token_block.get("access_token"),
            "refresh_token": token_block.get("refresh_token"),
            "expires_at": token_block.get("expires_at"),
            "client_id": data.get("client_id"),
            "client_secret": data.get("client_secret"),
        }

    def _write_json(self, payload: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    async def get_access_token(self, session: aiohttp.ClientSession) -> str:
        raw = self._read_json()
        token = self._extract_token_block(raw)

        access_token = str(token.get("access_token") or "").strip()
        refresh_token = str(token.get("refresh_token") or "").strip()
        client_id = str(token.get("client_id") or "").strip()
        client_secret = str(token.get("client_secret") or "").strip()

        expires_at_raw = token.get("expires_at")
        try:
            expires_at = float(expires_at_raw) if expires_at_raw is not None else None
        except Exception:
            expires_at = None

        now = time.time()
        # Refresh 5 minutes early if we have enough information.
        if access_token and (expires_at is None or now < (expires_at - 300)):
            return access_token

        if not refresh_token:
            raise RuntimeError("SmartThings access token expired and no refresh_token is available")
        if not client_id or not client_secret:
            raise RuntimeError(
                "SmartThings access token expired and token file has no client_id/client_secret for refresh"
            )

        auth = aiohttp.BasicAuth(client_id, client_secret)
        async with session.post(
            SMARTTHINGS_AUTH_BASE,
            auth=auth,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            ssl=False,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"SmartThings token refresh failed status={resp.status} body={body}")

            payload = json.loads(body)

        new_access = str(payload.get("access_token") or "").strip()
        new_refresh = str(payload.get("refresh_token") or refresh_token).strip()
        expires_in = int(payload.get("expires_in") or 86400)
        new_expires_at = time.time() + expires_in

        updated = {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_at": new_expires_at,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        self._write_json(updated)
        return new_access


class SamsungSoundbar:
    def __init__(
        self,
        *,
        device_id: str,
        poll_interval_s: float = 30.0,
        command_interval_s: float = 0.2,
        token_file: str = "/data/smartthings-token.json",
        state_change_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._device_id = (device_id or "").strip()
        self._poll_interval_s = max(5.0, float(poll_interval_s))
        self._command_interval_s = max(0.1, float(command_interval_s))

        self._enabled = False
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._poll_wake_evt = asyncio.Event()

        self._state = SamsungSoundbarState()
        self._session: aiohttp.ClientSession | None = None
        self._token_store = SmartThingsTokenStore(token_file)
        self._send_lock = asyncio.Lock()
        self._last_send_monotonic = 0.0

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
            "last_update_ts": last_i,
            "update_age_s": age_i,
            "power_on": s.power_on,
            "raw_input_source": s.raw_input_source,
            "sound_from": s.sound_from,
            "listen_active": s.listen_active,
        }

    async def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._stop_evt.clear()
        self._poll_wake_evt.clear()

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)

        self._task = asyncio.create_task(self._runner(), name=f"smartthings[{self._device_id}]")

    async def stop(self) -> None:
        self._enabled = False
        self._stop_evt.set()
        self._poll_wake_evt.set()

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def request_refresh(self) -> None:
        self._poll_wake_evt.set()
        await self._refresh_now()

    async def _runner(self) -> None:
        while self._enabled and not self._stop_evt.is_set():
            try:
                await self._refresh_now()

                self._poll_wake_evt.clear()
                try:
                    await asyncio.wait_for(self._poll_wake_evt.wait(), timeout=self._poll_interval_s)
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
                logger.warning("SamsungSoundbar refresh failed: %s", exc)
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=self._poll_interval_s)
                except asyncio.TimeoutError:
                    pass

    async def _refresh_now(self) -> None:
        old_listen = bool(self._state.listen_active)

        payload = await self._get_device_payload()
        self._parse_payload(payload)

        if not old_listen and self._state.listen_active:
            self._emit_state_change(
                "listen",
                {
                    "domain": "speaker",
                    "source": self._state.source,
                    "playback_status": self._state.playback_status,
                },
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _auth_headers(self) -> dict[str, str]:
        session = await self._get_session()
        token = await self._token_store.get_access_token(session)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _get_device_payload(self) -> dict[str, Any]:
        session = await self._get_session()
        headers = await self._auth_headers()
        url = f"{SMARTTHINGS_API_BASE}/devices/{self._device_id}"

        async with session.get(url, headers=headers, ssl=False) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"SmartThings GET failed status={resp.status} body={body}")
            payload = json.loads(body)

        self._state.reachable = True
        self._state.connected = True
        self._state.ready = True
        self._state.last_error = None
        return payload

    async def _send_command(
        self,
        capability: str,
        command: str,
        arguments: list[Any] | None = None,
    ) -> bool:
        session = await self._get_session()
        headers = await self._auth_headers()
        url = f"{SMARTTHINGS_API_BASE}/devices/{self._device_id}/commands"

        body = {
            "commands": [
                {
                    "component": "main",
                    "capability": capability,
                    "command": command,
                    **({"arguments": arguments} if arguments else {}),
                }
            ]
        }

        async with self._send_lock:
            now_m = time.monotonic()
            dt = now_m - self._last_send_monotonic
            if dt < self._command_interval_s:
                await asyncio.sleep(self._command_interval_s - dt)

            async with session.post(url, headers=headers, json=body, ssl=False) as resp:
                text = await resp.text()
                self._last_send_monotonic = time.monotonic()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"SmartThings command failed capability={capability} command={command} "
                        f"status={resp.status} body={text}"
                    )

        self._poll_wake_evt.set()
        return True

    def _parse_payload(self, payload: dict[str, Any]) -> None:
        # Support either /devices payload (components list) or /status payload (components.main dict).
        main_map: dict[str, Any] = {}

        components = payload.get("components")
        if isinstance(components, list):
            for comp in components:
                if not isinstance(comp, dict) or comp.get("id") != "main":
                    continue
                caps = comp.get("capabilities") or []
                for item in caps:
                    if not isinstance(item, dict):
                        continue
                    cap_id = str(item.get("id") or "")
                    status = item.get("status") or {}
                    if isinstance(status, dict):
                        main_map[cap_id] = status
                break
        elif isinstance(components, dict):
            main = components.get("main") or {}
            if isinstance(main, dict):
                main_map = main

        power = self._get_attr(main_map, "switch", "switch")
        volume = self._get_attr(main_map, "audioVolume", "volume")
        mute = self._get_attr(main_map, "audioMute", "mute")
        input_source = self._get_attr(main_map, "samsungvd.audioInputSource", "inputSource")
        playback_status = self._get_attr(main_map, "samsungvd.audioPlayback", "playbackStatus")

        sound_from = (
            self._get_attr(main_map, "samsungvd.audioSoundFrom", "detailName")
            or self._get_attr(main_map, "samsungvd.soundFrom", "detailName")
        )

        power_on = str(power or "").strip().lower() == "on"

        derived_source = self._derive_source(
            power_on=power_on,
            raw_input_source=input_source,
            sound_from=sound_from,
        )
        listen_active = power_on and derived_source == "airplay"

        self._state.power_on = power_on
        self._state.raw_input_source = self._norm_str(input_source)
        self._state.sound_from = self._norm_str(sound_from)
        self._state.source = derived_source
        self._state.listen_active = listen_active

        self._state.playback_status = self._norm_str(playback_status)

        if isinstance(volume, (int, float)):
            self._state.volume = max(0.0, min(1.0, float(volume) / 100.0))
        else:
            self._state.volume = None

        if isinstance(mute, str):
            value = mute.strip().lower()
            if value == "muted":
                self._state.muted = True
            elif value == "unmuted":
                self._state.muted = False
            else:
                self._state.muted = None
        else:
            self._state.muted = None

        self._state.last_update_ts = _now()

    @staticmethod
    def _get_attr(main_map: dict[str, Any], capability: str, attr: str) -> Any:
        cap = main_map.get(capability)
        if isinstance(cap, dict):
            node = cap.get(attr)
            if isinstance(node, dict):
                return node.get("value")
        return None

    @staticmethod
    def _norm_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _derive_source(
        *,
        power_on: bool,
        raw_input_source: Any,
        sound_from: Any,
    ) -> str | None:
        if not power_on:
            return None

        source = str(raw_input_source or "").strip().upper()
        from_name = str(sound_from or "").strip().lower()

        if source == "D.IN":
            return "optical"
        if source == "BT":
            return "bluetooth"
        if source == "WIFI":
            return "wifi"

        # Samsung often leaves inputSource empty while the bar is in AirPlay/listen mode.
        # Preserve existing PiHub flow predicates by exposing a derived source.
        if source == "":
            if from_name == "tv":
                return "hdmi"
            return "airplay"

        if from_name == "tv":
            return "hdmi"

        return None

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        cb = self._state_change_callback
        if cb is None:
            return
        try:
            asyncio.create_task(cb(name, payload))
        except Exception:
            logger.exception("SamsungSoundbar state change callback failed name=%s", name)

    # ---- Controls ----

    async def power_on(self) -> None:
        await self._send_command("switch", "on")
        await self.request_refresh()

    async def power_off(self) -> None:
        await self._send_command("switch", "off")
        await self.request_refresh()

    async def volume_up(self) -> None:
        await self._send_command("audioVolume", "volumeUp")
        await self.request_refresh()

    async def volume_down(self) -> None:
        await self._send_command("audioVolume", "volumeDown")
        await self.request_refresh()

    async def set_volume(self, pct: int) -> None:
        target = max(0, min(100, int(pct)))
        await self._send_command("audioVolume", "setVolume", [target])
        await self.request_refresh()

    async def set_muted(self, target: bool) -> None:
        await self._send_command("audioMute", "mute" if target else "unmute")
        await self.request_refresh()

    async def mute_toggle(self) -> None:
        cur = bool(self._state.muted) if self._state.muted is not None else False
        await self.set_muted(not cur)

    async def play(self) -> None:
        await self._send_command("samsungvd.audioPlayback", "play")
        await self.request_refresh()

    async def pause(self) -> None:
        await self._send_command("samsungvd.audioPlayback", "pause")
        await self.request_refresh()

    async def stop_playback(self) -> None:
        await self._send_command("samsungvd.audioPlayback", "stop")
        await self.request_refresh()

    async def next_track(self) -> None:
        await self._send_command("samsungvd.audioPlayback", "fastForward")
        await self.request_refresh()

    async def previous_track(self) -> None:
        await self._send_command("samsungvd.audioPlayback", "rewind")
        await self.request_refresh()

    # ---- No-op methods so existing flows can stay unchanged ----

    async def set_source(self, source: str) -> None:
        del source
        logger.debug("SamsungSoundbar set_source ignored")

    async def preset(self, n: int) -> None:
        del n
        logger.debug("SamsungSoundbar preset ignored")

    async def next_preset(self) -> None:
        logger.debug("SamsungSoundbar next_preset ignored")

    async def previous_preset(self) -> None:
        logger.debug("SamsungSoundbar previous_preset ignored")

    async def play_url(self, url: str) -> None:
        del url
        logger.debug("SamsungSoundbar play_url ignored")