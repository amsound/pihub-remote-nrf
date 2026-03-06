"""
LinkPlay/WiiM speaker controller (TCP API only).

Policy (agreed):
- State refresh primitive: always MCU+PINFGET (no MEA / no SONGGET)
- Connected gating: connected=True only after first successful PINFGET parse
- Mute safety: always MUT+GET before changing mute
- Polling: only on Wi-Fi-ish sources; 10s play/loading / 30s pause / 60s idle; physical inputs 60s liveness
- After commands/hints: always PINFGET converge pull
- Health details should be minimal (this file provides snapshot() accordingly)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

_TCP_DEFAULT_PORT = 8899
_TCP_HEADER = b"\x18\x96\x18\x20"  # 0x18 0x96 0x18 0x20

_HTTPAPI_PATH = "/httpapi.asp"
_HTTP_TIMEOUT_S = 10
DEFAULT_HTTP_POWEROFF_CMD = "setPlayerCmd:poweroff"

# Poll knobs (tweak here)
POLL_WIFI_PLAYING_S = 10.0   # play OR loading
POLL_WIFI_PAUSED_S = 30.0
POLL_WIFI_IDLE_S = 60.0
POLL_PHYSICAL_S = 60.0

# Mute safety knobs
MUTE_GET_DELAY_S = 0.05      # tiny settle delay after MUT+GET
MUTE_GET_TIMEOUT_S = 0.6     # wait for AXX+MUT+... after GET

# PLM input modes -> app-friendly "source"
# (Doc: https://developer.arylic.com/tcpapi/ — see "Current Input Mode" / PLM table.)
# You asked for:
#   airplay / line-in / bluetooth / hdmi / optical / multiroom-secondary / idle
#   Everything else that is "network-y" gets grouped under "wifi".
_PLM_TO_SOURCE = {
    "000": "idle",
    "001": "airplay",
    "040": "line-in",
    "041": "bluetooth",
    "049": "hdmi",
    "043": "optical",
    "099": "multiroom-secondary",

    # Umbrella these under "wifi"
    "002": "wifi",  # DLNA
    "010": "wifi",  # online playlist / network playback
    "011": "wifi",  # USB playlist
    "020": "wifi",  # HTTP API playback mode
    "031": "wifi",  # Spotify Connect
    "032": "wifi",  # TIDAL Connect
}

_PHYSICAL_SOURCES = {"hdmi", "optical", "line-in", "bluetooth"}


def _now() -> float:
    return time.time()


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _parse_payload(payload: bytes) -> str:
    # Payloads are ASCII like "AXX+VOL+030" or longer ending with "&"
    try:
        return payload.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""

def _needs_amp(s: str) -> bool:
    # Doc: payloads longer than 11 bytes should end with '&'
    return len(s.encode("utf-8")) > 11 and not s.endswith("&")


def _mk_packet(payload_text: str) -> bytes:
    pb = payload_text.encode("utf-8")
    length = len(pb)
    checksum = sum(pb) & 0xFFFFFFFF
    # header(4) + length(4 LE) + checksum(4 LE) + reserved(8 zeros) + payload
    return _TCP_HEADER + struct.pack("<I", length) + struct.pack("<I", checksum) + (b"\x00" * 8) + pb


@dataclass
class SpeakerState:
    reachable: bool = False
    subscribed: bool = False        # TCP socket is up
    connected: bool = False         # becomes true only after first successful PINFGET parse
    last_error: str | None = None

    playback_status: str | None = None  # play/pause/stop/loading/... (wifi-ish only; physical inputs => None)
    volume: float | None = None         # 0..1
    muted: bool | None = None

    source: str | None = None           # wifi/bluetooth/line-in/optical/hdmi/airplay/...
    last_update_ts: float | None = None


class LinkPlaySpeaker:
    """
    TCP API driver.

    Public API matches the UPnP version:
      - start/stop
      - volume_up/volume_down/mute_toggle (+ set_muted)
      - play/pause/stop_playback/next_track/previous_track/play_pause
      - preset (+ next_preset/previous_preset)
      - play_url (still uses httpapi.asp)
      - set_source (best-effort via PLM if supported)
      - turn_off / clear_playlist (best-effort)
    """

    def __init__(
        self,
        *,
        host: str,
        tcp_port: int = _TCP_DEFAULT_PORT,
        http_scheme: str = "https",
        volume_step_pct: int = 2,
        command_interval_s: float = 0.2,
        reconnect_s: float = 3.0,
    ) -> None:
        self._host = host.strip()
        self._tcp_port = int(tcp_port)
        self._http_scheme = (http_scheme or "https").strip().lower()
        if self._http_scheme not in {"http", "https"}:
            self._http_scheme = "https"

        self._volume_step_pct = _clamp_int(int(volume_step_pct), 1, 25)
        self._command_interval_s = max(0.2, float(command_interval_s))  # doc says >=200ms
        self._reconnect_s = max(1.0, float(reconnect_s))

        self._state = SpeakerState()

        self._enabled = False
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        self._send_lock = asyncio.Lock()
        self._last_send_monotonic = 0.0

        self._session: aiohttp.ClientSession | None = None

        self._poll_task: asyncio.Task | None = None
        self._log_drop_once = False

        # Mute GET waiter
        self._mute_evt = asyncio.Event()

    # ---------- small public helpers ----------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> SpeakerState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        """Minimal state snapshot for /health details. Whole-second timestamps."""
        s = self._state
        now_i = int(time.time())
        last_i = None if s.last_update_ts is None else int(s.last_update_ts)
        age_i = None if last_i is None else max(0, now_i - last_i)
        return {
            "connected": bool(s.connected),
            "playback_status": s.playback_status,
            "volume_pct": None if s.volume is None else int(round(s.volume * 100)),
            "muted": s.muted,
            "source": s.source,
            "last_update_ts": last_i,
            "update_age_s": age_i,
        }

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._stop_evt.clear()

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            # Keep it simple; LinkPlay HTTPS is often self-signed
            self._session = aiohttp.ClientSession(timeout=timeout)

        self._task = asyncio.create_task(self._runner(), name=f"linkplay_tcp[{self._host}]")

    async def stop(self) -> None:
        self._enabled = False
        self._stop_evt.set()

        if self._task:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
        self._task = None

        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(Exception):
                await self._poll_task
        self._poll_task = None

        await self._disconnect()

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _runner(self) -> None:
        while self._enabled and not self._stop_evt.is_set():
            try:
                await self._connect()

                # Start polling loop alongside read loop
                self._poll_task = asyncio.create_task(self._poll_loop(), name=f"linkplay_poll[{self._host}]")

                # Initial handshake pull (this is what flips connected=True after parse)
                await self._pinfget()

                await self._read_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("TCP loop error host=%s err=%r", self._host, e)
                self._mark_down(str(e))
            finally:
                if self._poll_task:
                    self._poll_task.cancel()
                    with contextlib.suppress(Exception):
                        await self._poll_task
                self._poll_task = None
                await self._disconnect()

            # backoff before reconnect
            if self._enabled and not self._stop_evt.is_set():
                await asyncio.sleep(self._reconnect_s)

    async def _connect(self) -> None:
        self._state.last_error = None
        self._state.subscribed = False
        self._state.reachable = False
        self._state.connected = False
        self._log_drop_once = False

        reader, writer = await asyncio.open_connection(self._host, self._tcp_port)
        self._reader, self._writer = reader, writer
        self._state.reachable = True
        self._state.subscribed = True
        self._state.last_update_ts = _now()
        logger.info("linkplay tcp connected host=%s port=%s", self._host, self._tcp_port)

    async def _disconnect(self) -> None:
        # Reset flags; keep last_error as-is for inspection
        self._state.subscribed = False
        self._state.reachable = False
        self._state.connected = False
        self._state.last_update_ts = _now()

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader, self._writer = None, None

    def _mark_down(self, err: str | None) -> None:
        self._state.reachable = False
        self._state.subscribed = False
        self._state.connected = False
        self._state.last_error = err
        self._state.last_update_ts = _now()

    # ---------- send / receive ----------

    async def _send(self, payload: str) -> None:
        """Low-level send (does not apply the connected gate)."""
        if not self._writer:
            raise ConnectionError("not connected")

        if _needs_amp(payload):
            payload = payload + "&"

        packet = _mk_packet(payload)

        async with self._send_lock:
            # enforce >=200ms between commands (doc requirement)
            now_m = time.monotonic()
            dt = now_m - self._last_send_monotonic
            if dt < self._command_interval_s:
                await asyncio.sleep(self._command_interval_s - dt)

            self._writer.write(packet)
            await self._writer.drain()

            self._last_send_monotonic = time.monotonic()

    async def _send_control(self, payload: str) -> bool:
        """
        Control-plane sends. Drop until connected=True (handshake complete).
        Returns True if sent, False if dropped.
        """
        if not self._state.connected:
            if not self._log_drop_once:
                logger.info("speaker not connected yet; dropping command=%s", payload)
                self._log_drop_once = True
            return False
        try:
            await self._send(payload)
            return True
        except Exception as e:
            self._state.last_error = str(e)
            self._state.last_update_ts = _now()
            return False

    async def _pinfget(self) -> None:
        """Single truth pull (allowed even before connected gate)."""
        if not self._writer:
            return
        with contextlib.suppress(Exception):
            await self._send("MCU+PINFGET")

    async def _read_loop(self) -> None:
        assert self._reader is not None

        buf = bytearray()
        while self._enabled and not self._stop_evt.is_set():
            chunk = await self._reader.read(4096)
            if not chunk:
                raise ConnectionError("tcp closed by peer")
            buf.extend(chunk)

            # parse as many packets as possible
            while True:
                if len(buf) < 20:
                    break

                # resync to header
                hpos = buf.find(_TCP_HEADER)
                if hpos == -1:
                    buf.clear()
                    break
                if hpos > 0:
                    del buf[:hpos]
                    if len(buf) < 20:
                        break

                length = struct.unpack_from("<I", buf, 4)[0]
                checksum = struct.unpack_from("<I", buf, 8)[0]
                total = 20 + length
                if len(buf) < total:
                    break

                payload_bytes = bytes(buf[20:total])
                del buf[:total]

                # validate checksum but don't die if it fails (some firmwares are weird)
                if (sum(payload_bytes) & 0xFFFFFFFF) != checksum:
                    logger.debug("checksum mismatch host=%s", self._host)

                payload = _parse_payload(payload_bytes)
                if payload:
                    self._on_payload(payload)

    def _on_payload(self, payload: str) -> None:
        changed = False

        # Examples:
        #   AXX+VOL+030
        #   AXX+MUT+001
        #   AXX+PLM+010
        #   AXX+PLY+INF{ ... }&
        #   AXX+PLY+NEW
        #   AXX+KEY+003
        #   AXX+KEY+RDY
        p = payload.strip()
        if p.endswith("&"):
            p = p[:-1]

        if p.startswith("AXX+VOL+"):
            v = p.split("+", 2)[2]
            try:
                vi = int(v)
                self._state.volume = _clamp_int(vi, 0, 100) / 100.0
                changed = True
            except Exception:
                pass

        elif p.startswith("AXX+MUT+"):
            m = p.split("+", 2)[2]
            if m in ("000", "001"):
                self._state.muted = (m == "001")
                changed = True
                self._mute_evt.set()

        elif p.startswith("AXX+PLM+"):
            mode = p.split("+", 2)[2]
            self._state.source = _PLM_TO_SOURCE.get(mode, "unknown")
            # Physical inputs: playback status not applicable / not trusted
            if self._state.source in _PHYSICAL_SOURCES:
                self._state.playback_status = None
            changed = True

        elif p.startswith("AXX+PLY+INF"):
            # JSON blob after "AXX+PLY+INF"
            j = p[len("AXX+PLY+INF"):].strip()
            if j.startswith("{") and j.endswith("}"):
                try:
                    data = json.loads(j)

                    # Gate: first valid PINFGET parse makes us "connected"
                    was_ready = self._state.connected
                    self._state.connected = True
                    if not was_ready:
                        logger.info("linkplay tcp ready host=%s (initial PINFGET received)", self._host)

                    # Mode can mirror PLM
                    mode = str(data.get("mode", "")).strip()
                    if mode:
                        self._state.source = _PLM_TO_SOURCE.get(mode.zfill(3), self._state.source or "unknown")
                        if self._state.source in _PHYSICAL_SOURCES:
                            self._state.playback_status = None
                        changed = True

                    # status only meaningful for wifi-ish sources
                    st = str(data.get("status", "")).strip().lower()
                    if st and (self._state.source not in _PHYSICAL_SOURCES):
                        self._state.playback_status = st
                        changed = True

                    # vol/mute included here too
                    if "vol" in data:
                        try:
                            vi = int(str(data["vol"]))
                            self._state.volume = _clamp_int(vi, 0, 100) / 100.0
                            changed = True
                        except Exception:
                            pass

                    if "mute" in data:
                        try:
                            mi = int(str(data["mute"]))
                            self._state.muted = (mi != 0)
                            changed = True
                        except Exception:
                            pass
                except Exception:
                    pass

        elif p.startswith("AXX+PLY+NEW"):
            # Hint: pull authoritative state. For physical inputs, playback doesn't apply anyway,
            # but we still allow the occasional liveness poll to do the correction.
            asyncio.create_task(self._pinfget())

        # else: ignore

        if changed:
            self._state.last_update_ts = _now()

    async def _poll_loop(self) -> None:
        """
        Gentle polling to catch out-of-band changes (app/CEC/stream stalls).

        Rules:
        - Wi-Fi source: 10s play/loading, 30s pause, 60s idle/stop/unknown
        - Physical sources: 60s liveness poll
        """
        while self._enabled and not self._stop_evt.is_set():
            src = self._state.source or "unknown"

            if src == "wifi":
                st = (self._state.playback_status or "").lower()
                if st in ("play", "loading"):
                    interval = POLL_WIFI_PLAYING_S
                elif st == "pause":
                    interval = POLL_WIFI_PAUSED_S
                else:
                    interval = POLL_WIFI_IDLE_S
            else:
                interval = POLL_PHYSICAL_S

            await asyncio.sleep(float(interval))
            await self._pinfget()

    # ---------- controls (TCP) ----------

    async def volume_up(self) -> None:
        v = self._state.volume
        cur = int(round((v or 0.0) * 100))
        nxt = _clamp_int(cur + self._volume_step_pct, 0, 100)
        await self._send_control(f"MCU+VOL+{nxt:03d}")

    async def volume_down(self) -> None:
        v = self._state.volume
        cur = int(round((v or 0.0) * 100))
        nxt = _clamp_int(cur - self._volume_step_pct, 0, 100)
        await self._send_control(f"MCU+VOL+{nxt:03d}")

    async def set_muted(self, target: bool) -> None:
        # Always GET before SET (mute may have changed elsewhere)
        self._mute_evt.clear()
        # MUT+GET is safe even pre-connected; but _send_control will drop before connected.
        if self._state.connected:
            await self._send_control("MCU+MUT+GET")
        else:
            # If we're not connected yet, just drop (consistent with gating) and let handshake establish state
            await self._send_control("MCU+MUT+GET")
            return

        with contextlib.suppress(Exception):
            await asyncio.sleep(MUTE_GET_DELAY_S)
            await asyncio.wait_for(self._mute_evt.wait(), timeout=MUTE_GET_TIMEOUT_S)

        cur = bool(self._state.muted) if self._state.muted is not None else False
        if cur == bool(target):
            asyncio.create_task(self._pinfget())
            return

        new = "001" if target else "000"
        if await self._send_control(f"MCU+MUT+{new}"):
            asyncio.create_task(self._pinfget())

    async def mute_toggle(self) -> None:
        cur = bool(self._state.muted) if self._state.muted is not None else False
        await self.set_muted(not cur)

    async def play(self) -> None:
        if await self._send_control("MCU+PLY-PLA"):
            asyncio.create_task(self._pinfget())

    async def pause(self) -> None:
        if await self._send_control("MCU+PLY-PUS"):
            asyncio.create_task(self._pinfget())

    async def stop_playback(self) -> None:
        if await self._send_control("MCU+PLY-STP"):
            asyncio.create_task(self._pinfget())

    async def next_track(self) -> None:
        await self._send_control("MCU+PLY+NXT")

    async def previous_track(self) -> None:
        await self._send_control("MCU+PLY+PRV")

    async def play_pause(self) -> None:
        if await self._send_control("MCU+PLY+PUS"):
            asyncio.create_task(self._pinfget())

    async def preset(self, n: int) -> None:
        n = _clamp_int(int(n), 1, 10)
        await self._send_control(f"MCU+KEY+{n:03d}")

    async def next_preset(self) -> None:
        await self._send_control("MCU+KEY+NXT")

    async def previous_preset(self) -> None:
        await self._send_control("MCU+KEY+PRE")

    async def set_source(self, source: str) -> None:
        """
        Best-effort: many firmwares accept MCU+PLM+<mode> to switch input.
        Your device sources: wifi, hdmi, optical, line-in, bluetooth.
        """
        src = (source or "").strip().lower()
        want_mode: str | None = None
        if src == "wifi":
            want_mode = "010"
        elif src == "hdmi":
            want_mode = "049"
        elif src == "optical":
            want_mode = "043"
        elif src in ("line-in", "linein"):
            want_mode = "040"
        elif src in ("bluetooth", "bt"):
            want_mode = "041"

        if not want_mode:
            logger.info("set_source ignored (unsupported) source=%s", source)
            return

        if await self._send_control(f"MCU+PLM+{want_mode}"):
            asyncio.create_task(self._pinfget())

    async def clear_playlist(self) -> None:
        # Not standardized in the TCP doc; keep as no-op.
        logger.info("clear_playlist ignored (no standardized TCP command)")

    # -------------- HTTP API --------------

    async def turn_off(self) -> None:
        """Turn the speaker off/standby via HTTP API."""
        cmd = getattr(self, "_http_poweroff_cmd", DEFAULT_HTTP_POWEROFF_CMD)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._http_cmd(cmd))
        except RuntimeError:
            asyncio.run(self._http_cmd(cmd))

    async def play_url(self, url: str) -> None:
        """
        LinkPlay's TCP API doesn't cover "play arbitrary URL" for all firmwares.
        This keeps your existing behavior: call httpapi.asp over HTTPS (often self-signed).
        """
        if not url:
            return
        if not self._session:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)

        cmd = f"setPlayerCmd:play:{url}"
        endpoint = f"{self._http_scheme}://{self._host}{_HTTPAPI_PATH}"
        params = {"command": cmd}

        try:
            async with self._session.get(endpoint, params=params, ssl=False) as resp:
                _ = await resp.text(errors="replace")
                if resp.status >= 400:
                    raise RuntimeError(f"httpapi status {resp.status}")
        except Exception as e:
            self._state.last_error = f"httpapi play_url failed: {e}"
            self._state.last_update_ts = _now()
            raise
        finally:
            asyncio.create_task(self._pinfget())

    async def _http_cmd(self, cmd: str) -> None:
        if not self._session:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)

        endpoint = f"{self._http_scheme}://{self._host}{_HTTPAPI_PATH}"
        params = {"command": cmd}

        async with self._session.get(endpoint, params=params, ssl=False) as resp:
            _ = await resp.text(errors="replace")
            if resp.status >= 400:
                raise RuntimeError(f"httpapi status {resp.status}")