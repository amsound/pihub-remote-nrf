"""
LinkPlay/WiiM speaker controller (TCP API only).

Why this exists:
- LinkPlay "TCP API" is a single TCP connection (default port 8899) that pushes
  state changes (volume/mute/playback/media info) to the client.
- This avoids UPnP/SSDP multicast + GENA eventing headaches across VLANs.

Implements the same public surface as the existing speaker_linkplay.py (start/stop,
volume/mute/transport, preset, play_url) so you can swap it in with minimal changes.
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

    # Optical isn't listed for every model in the public PLM table; you've observed 043 on your device.
    "043": "optical",
    # Some firmwares use related codes for digital inputs; treat them as "optical" for the app UI.
    "045": "optical",  # coaxial
    "056": "optical",  # optical 2
    "057": "optical",  # coaxial 2
    "058": "optical",  # ARC

    "099": "multiroom-secondary",

    # Umbrella these under "wifi"
    "002": "wifi",  # DLNA
    "010": "wifi",  # online playlist / network playback
    "020": "wifi",  # HTTP API playback mode
    "031": "wifi",  # Spotify Connect
    "032": "wifi",  # TIDAL Connect
    "011": "wifi",  # USB playlist (treated as "wifi" for your app needs)
}


_STATUS_TO_TRANSPORT = {
    "play": "playing",
    "pause": "paused",
    "stop": "stopped",
}


def _now() -> float:
    return time.time()


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _hex_to_text(maybe_hex: str) -> str:
    """
    LinkPlay uses hex-encoded UTF-8 strings in a bunch of places (Title/Artist/Album, etc).
    If it isn't valid hex, return as-is.
    """
    if not maybe_hex:
        return ""
    s = maybe_hex.strip()
    # quick filter
    if len(s) % 2 != 0:
        return s
    try:
        raw = bytes.fromhex(s)
        # strip nulls
        raw = raw.replace(b"\x00", b"")
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return s


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
    subscribed: bool = False  # "subscribed" = TCP connected
    last_error: str | None = None

    transport: str | None = None  # playing/paused/stopped/idle/unknown
    volume: float | None = None   # 0..1
    muted: bool | None = None

    source: str | None = None     # wifi/bluetooth/line-in/optical/hdmi/airplay/...
    source_detail: str | None = None  # e.g. vendor field (CustomRadio/newTuneIn/etc)
    track_uri: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    last_update_ts: float | None = None


class LinkPlaySpeaker:
    """
    TCP API driver.

    Public API matches the UPnP version:
      - start/stop
      - volume_up/volume_down/mute_toggle
      - play/pause/stop_playback/next_track/previous_track/play_pause
      - preset
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

        # Keep the most recent PLM and PLY blobs around (helps with "source"/"transport")
        self._plm: str | None = None
        self._ply: dict[str, Any] | None = None

    # ---------- small public helpers ----------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> SpeakerState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        # Keep parity with your "health" / status JSON usage.
        s = self._state
        return {
            "reachable": bool(s.reachable),
            "subscribed": bool(s.subscribed),
            "last_error": s.last_error,
            "transport": s.transport,
            "volume_pct": None if s.volume is None else int(round(s.volume * 100)),
            "muted": s.muted,
            "source": s.source,
            "source_detail": s.source_detail,
            "track_uri": s.track_uri,
            "title": s.title,
            "artist": s.artist,
            "album": s.album,
            "last_update_ts": s.last_update_ts,
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

        await self._disconnect()

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _runner(self) -> None:
        while self._enabled and not self._stop_evt.is_set():
            try:
                await self._connect()
                await self._prime_state()
                await self._read_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("TCP loop error host=%s err=%r", self._host, e)
                self._mark_down(str(e))
            finally:
                await self._disconnect()
            # backoff before reconnect
            if self._enabled and not self._stop_evt.is_set():
                await asyncio.sleep(self._reconnect_s)

    async def _connect(self) -> None:
        self._state.last_error = None
        self._state.subscribed = False
        self._state.reachable = False

        reader, writer = await asyncio.open_connection(self._host, self._tcp_port)
        self._reader, self._writer = reader, writer
        self._state.reachable = True
        self._state.subscribed = True
        self._state.last_update_ts = _now()
        logger.info("linkplay tcp connected host=%s port=%s", self._host, self._tcp_port)

    async def _disconnect(self) -> None:
        self._state.subscribed = False
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
        self._state.last_error = err
        self._state.last_update_ts = _now()

    # ---------- send / receive ----------

    async def _send(self, payload: str) -> None:
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
        self._state.last_notify_ts = _now()
        changed = False

        # Examples:
        #   AXX+VOL+030
        #   AXX+MUT+001
        #   AXX+PLM+010
        #   AXX+MEA+DAT{ ... }&
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

        elif p.startswith("AXX+PLM+"):
            mode = p.split("+", 2)[2]
            self._plm = mode
            self._state.source = _PLM_TO_SOURCE.get(mode, "unknown")
            # "idle" also implies not playing
            if mode == "000" and self._state.transport in (None, "unknown", "playing", "paused"):
                self._state.transport = "idle"
            changed = True

        elif p.startswith("AXX+PLY+INF"):
            # JSON blob after "AXX+PLY+INF"
            j = p[len("AXX+PLY+INF") :]
            j = j.strip()
            if j.startswith("{") and j.endswith("}"):
                try:
                    data = json.loads(j)
                    self._ply = data
                    # status
                    st = str(data.get("status", "")).lower()
                    vend = str(data.get("vendor", "") or "").strip()
                    if vend:
                        self._state.source_detail = vend
                        changed = True
                    if st:
                        self._state.transport = _STATUS_TO_TRANSPORT.get(st, st)
                        changed = True
                    # vol/mute sometimes included here too
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
                    # Title/Artist/Album are often hex-encoded.
                    # Some firmwares put a URL directly in Title when streaming.
                    if "Title" in data:
                        raw_title = str(data.get("Title", "") or "")
                        dec_title = _hex_to_text(raw_title)
                        if dec_title.startswith("http://") or dec_title.startswith("https://"):
                            self._state.track_uri = dec_title
                        else:
                            self._state.title = dec_title
                        changed = True
                    if "Artist" in data:
                        self._state.artist = _hex_to_text(str(data.get("Artist", "")))
                        changed = True
                    if "Album" in data:
                        self._state.album = _hex_to_text(str(data.get("Album", "")))
                        changed = True
                    # mode inside PLY can mirror PLM
                    if "mode" in data:
                        mode = str(data.get("mode", ""))
                        if mode:
                            self._plm = mode
                            self._state.source = _PLM_TO_SOURCE.get(mode, self._state.source or "unknown")
                            changed = True
                except Exception:
                    pass

        elif p.startswith("AXX+MEA+DAT"):
            j = p[len("AXX+MEA+DAT") :].strip()
            if j.startswith("{") and j.endswith("}"):
                try:
                    data = json.loads(j)

                    t = _hex_to_text(str(data.get("title", "") or "")).strip()
                    a = _hex_to_text(str(data.get("artist", "") or "")).strip()
                    al = _hex_to_text(str(data.get("album", "") or "")).strip()

                    url_raw = str(data.get("url", "") or "").strip()
                    url = _hex_to_text(url_raw).strip() if url_raw else ""

                    # Apply atomically only if any meaningful field is present.
                    if t or a or al or url:
                        self._state.title = t or None
                        self._state.artist = a or None
                        self._state.album = al or None
                        if url:
                            self._state.track_uri = url
                        changed = True
                except Exception:
                    pass


        elif p.startswith("AXX+SNG+INF"):
            # short progress/status; we only care about status to keep transport fresh
            j = p[len("AXX+SNG+INF") :].strip()
            if j.startswith("{") and j.endswith("}"):
                try:
                    data = json.loads(j)
                    st = str(data.get("status", "")).lower()
                    vend = str(data.get("vendor", "") or "").strip()
                    if vend:
                        self._state.source_detail = vend
                        changed = True
                    if st:
                        self._state.transport = _STATUS_TO_TRANSPORT.get(st, st)
                        changed = True
                except Exception:
                    pass

        elif p.startswith("AXX+PLY+NEW"):
            # Something changed (preset/track/source). Clear stale now-playing fields so we do not mix old/new.
            self._state.track_uri = None
            self._state.title = None
            self._state.artist = None
            self._state.album = None
            self._schedule_hint_refresh()
            changed = True
    
    
    async def _pinfget(self) -> None:
        """Fetch a single current-status snapshot from the speaker."""
        if not self._writer:
            return
        try:
            await self._send("MCU+PINFGET")
        except Exception:
            return

    async def _mutget(self) -> None:
        """Fetch current mute state from the speaker."""
        if not self._writer:
            return
        try:
            await self._send("MCU+MUT+GET")
        except Exception:
            return

    def _schedule_hint_refresh(self) -> None:
        """
        Called on AXX+PLY+NEW ("something changed").
        Policy:
        - Immediately request a snapshot (MCU+PINFGET).
        - If the snapshot reports status == load/loading, do a bounded follow-up:
            after 2s -> PINFGET, if still loading
            after 5s -> PINFGET, then stop.
        """
        # Arm: the next AXX+PLY+INF we parse can decide whether we need follow-ups.
        self._hint_armed = True

        async def _immediate() -> None:
            # Cancel any in-flight immediate task
            await self._pinfget()

        # avoid spawning endless tasks if hints spam; keep one immediate task
        if self._hint_task and not self._hint_task.done():
            return
        self._hint_task = asyncio.create_task(_immediate(), name=f"linkplay_hint_immediate[{self._host}]")

    async def _resync_after_control(self) -> None:
        """
        Event-triggered sync after sending a control command.
        This is NOT periodic polling: we only do it right after user actions,
        because some firmwares don't push transport/mute changes reliably.
        """
        if not self._writer:
            return
        # Keep it short: transport + media + mute/vol.
        for c in ("MCU+PINFGET", "MCU+MEA+GET", "MCU+VOL+GET", "MCU+MUT+GET", "MCU+PLM+GET"):
            try:
                await self._send(c)
            except Exception:
                return
    
    async def _prime_state(self) -> None:
        """
        Ask for a full status snapshot so we can avoid polling and still converge quickly
        after reconnect or out-of-band changes.
        """
        if not self._writer:
            return

        # Ask for the things you care about:
        # - device info (optional)
        # - input mode (PLM)
        # - now playing (PINF + MEA + SONG)
        cmds = [
            "MCU+DEV+GET",
            "MCU+PLM+GET",
            "MCU+PINFGET",
            "MCU+MEA+GET",
            "MCU+SONGGET",
            "MCU+VOL+GET",
            "MCU+MUT+GET",
        ]
        for c in cmds:
            try:
                await self._send(c)
            except Exception:
                # if we're mid-disconnect, just bail
                return

    # ---------- controls (TCP) ----------

    async def volume_up(self) -> None:
        v = self._state.volume
        cur = int(round((v or 0.0) * 100))
        nxt = _clamp_int(cur + self._volume_step_pct, 0, 100)
        await self._send(f"MCU+VOL+{nxt:03d}")

    async def volume_down(self) -> None:
        v = self._state.volume
        cur = int(round((v or 0.0) * 100))
        nxt = _clamp_int(cur - self._volume_step_pct, 0, 100)
        await self._send(f"MCU+VOL+{nxt:03d}")

    
    async def mute_toggle(self) -> None:
        """
        Robust mute toggle:
          - Ask current mute state first (handles external changes)
          - Set opposite
          - Ask again to confirm
        """
        await self._mutget()
        await asyncio.sleep(0.25)

        cur = bool(self._state.muted) if self._state.muted is not None else False
        new = "000" if cur else "001"
        await self._send(f"MCU+MUT+{new}")

        await asyncio.sleep(0.25)
        await self._mutget()

    async def play(self) -> None:
        await self._send("MCU+PLY-PLA")
        await self._pinfget()

    async def pause(self) -> None:
        await self._send("MCU+PLY-PUS")
        await self._pinfget()

    async def stop_playback(self) -> None:
        await self._send("MCU+PLY-STP")
        await self._pinfget()

    async def next_track(self) -> None:
        await self._send("MCU+PLY+NXT")

    async def previous_track(self) -> None:
        await self._send("MCU+PLY+PRV")

    async def play_pause(self) -> None:
        await self._send("MCU+PLY+PUS")
        await self._pinfget()

    async def preset(self, n: int) -> None:
        n = _clamp_int(int(n), 1, 10)
        await self._send(f"MCU+KEY+{n:03d}")
        asyncio.create_task(self._resync_after_control())

    async def set_source(self, source: str) -> None:
        """
        Best-effort: the TCP API doc does not standardize "set input" in the basic set.
        Some firmwares support it via PAS passthrough; keep this as a no-op unless you
        know the right command for your model.
        """
        logger.info("set_source ignored (no standardized TCP command) source=%s", source)

    async def clear_playlist(self) -> None:
        # Not standardized in the TCP doc; keep as no-op.
        logger.info("clear_playlist ignored (no standardized TCP command)")

    async def turn_off(self) -> None:
        """Turn the speaker off/standby via HTTP API."""
        cmd = getattr(self, "_http_poweroff_cmd", DEFAULT_HTTP_POWEROFF_CMD)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._http_cmd(cmd))
        except RuntimeError:
            asyncio.run(self._http_cmd(cmd))


    # -------------- (HTTP API) --------------

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

        # Most LinkPlay firmwares accept: /httpapi.asp?command=setPlayerCmd:play:{url}
        # Some variants use setPlayerCmd:play or setPlayerCmd:play_url. Keep play: as default.
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
            # Ask for fresh now-playing
            asyncio.create_task(self._prime_state())

    async def turn_off(self) -> None:
        """Turn the speaker off/standby via HTTP API."""
        cmd = getattr(self, "_http_poweroff_cmd", DEFAULT_HTTP_POWEROFF_CMD)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._http_cmd(cmd))
        except RuntimeError:
            asyncio.run(self._http_cmd(cmd))
