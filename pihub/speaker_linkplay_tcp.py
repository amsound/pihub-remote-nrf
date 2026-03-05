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
import datetime
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

        # Hint-driven refresh (AXX+PLY+NEW)
        self._hint_task: asyncio.Task | None = None
        self._load_follow_task: asyncio.Task | None = None
        self._hint_armed: bool = False  # set True after hint until first PINFGET processed

    # ---------- small public helpers ----------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> SpeakerState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        """Compact state snapshot for /health.

        Intentionally minimal: one timestamp ("last_update") + an ISO string for humans.
        """
        s = self._state
        last_ts = s.last_update_ts
        now = time.time()
        age_s = None if last_ts is None else max(0.0, now - last_ts)
        last_iso = (
            None
            if last_ts is None
            else datetime.datetime.fromtimestamp(last_ts, tz=datetime.timezone.utc).isoformat()
        )

        return {
            "reachable": bool(s.reachable),
            "subscribed": bool(s.subscribed),  # kept for backwards-compat; means "connected" here
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
            "last_update_ts": last_ts,
            "last_update_iso": last_iso,
            "update_age_s": age_s,
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
        now = _now()
        self._state.last_notify_ts = now
        changed = False

        def _clear_now_playing(clear_source_detail: bool = False) -> None:
            """Clear fields that commonly go stale across track/source changes."""
            nonlocal changed
            if self._state.track_uri is not None:
                self._state.track_uri = None
                changed = True
            if self._state.title is not None:
                self._state.title = None
                changed = True
            if self._state.artist is not None:
                self._state.artist = None
                changed = True
            if self._state.album is not None:
                self._state.album = None
                changed = True
            if clear_source_detail and self._state.source_detail is not None:
                self._state.source_detail = None
                changed = True

        def _maybe_decode_text(s: str) -> str:
            """Some firmwares send hex strings, some send plain text."""
            s = (s or "").strip()
            if not s:
                return ""
            # Heuristic: if it looks like hex (even length, only hex chars), try decoding.
            hx = s.replace(" ", "")
            if len(hx) >= 2 and len(hx) % 2 == 0:
                ok = True
                for ch in hx:
                    if ch not in "0123456789abcdefABCDEF":
                        ok = False
                        break
                if ok:
                    try:
                        return _hex_to_text(s).strip()
                    except Exception:
                        pass
            return s

        def _parse_json_after_prefix(prefix: str) -> dict | None:
            """Robustly parse JSON even if firmware adds extra '+' tokens before '{'."""
            try:
                # Strip trailing '&' already handled above.
                # Find first '{' and parse from there.
                idx = p.find("{")
                if idx == -1:
                    return None
                j = p[idx:]
                if not j.endswith("}"):
                    return None
                return json.loads(j)
            except Exception:
                return None

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

        # --- IMPORTANT: put specific AXX+PLY+ handlers BEFORE the generic AXX+PLY+ ---
        elif p.startswith("AXX+PLY+INF"):
            data = _parse_json_after_prefix("AXX+PLY+INF")
            if isinstance(data, dict):
                self._ply = data

                # status: play/stop/pause are the only ones we treat as authoritative
                st = str(data.get("status", "")).lower().strip()
                if st in _STATUS_TO_TRANSPORT:
                    self._state.transport = _STATUS_TO_TRANSPORT[st]
                    changed = True
                    # If the device says stop/idle, blank now-playing to avoid stale display.
                    if self._state.transport in ("idle", "stopped", "stop"):
                        _clear_now_playing()

                # vendor/source detail (may be hex or plain)
                vend = _maybe_decode_text(str(data.get("vendor", "") or ""))
                if vend:
                    if self._state.source_detail != vend:
                        self._state.source_detail = vend
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

                # Title/Artist/Album fields (often hex)
                if "Title" in data:
                    raw_title = str(data.get("Title", "") or "")
                    dec_title = _maybe_decode_text(raw_title)
                    if dec_title.startswith("http://") or dec_title.startswith("https://"):
                        if self._state.track_uri != dec_title:
                            self._state.track_uri = dec_title
                            changed = True
                    else:
                        if self._state.title != dec_title:
                            self._state.title = dec_title
                            changed = True

                if "Artist" in data:
                    artist = _maybe_decode_text(str(data.get("Artist", "") or ""))
                    if self._state.artist != artist:
                        self._state.artist = artist
                        changed = True

                if "Album" in data:
                    album = _maybe_decode_text(str(data.get("Album", "") or ""))
                    if self._state.album != album:
                        self._state.album = album
                        changed = True

                # mode inside PLY can mirror PLM; if it changes, treat like a source change
                if "mode" in data:
                    mode = str(data.get("mode", "") or "").strip()
                    if mode and mode != getattr(self, "_plm", None):
                        self._plm = mode
                        new_source = _PLM_TO_SOURCE.get(mode, "unknown")
                        if self._state.source != new_source:
                            self._state.source = new_source
                            changed = True
                        _clear_now_playing(clear_source_detail=True)

        elif p.startswith("AXX+PLY+NEW"):
            # Something changed (preset/track/source). Clear stale fields and hint refresh.
            _clear_now_playing(clear_source_detail=False)
            self._schedule_hint_refresh()
            changed = True

        elif p.startswith("AXX+PLY+"):
            # Short playback state codes (e.g., AXX+PLY+000 / AXX+PLY+001)
            code = p.split("+", 2)[2].strip()
            if code == "000":
                if self._state.transport != "paused":
                    self._state.transport = "paused"
                    changed = True
            elif code == "001":
                if self._state.transport != "playing":
                    self._state.transport = "playing"
                    changed = True
            # Some firmwares may emit other codes; ignore unknowns.

        elif p.startswith("AXX+PLM+"):
            mode = p.split("+", 2)[2].strip()
            old_mode = getattr(self, "_plm", None)
            self._plm = mode

            new_source = _PLM_TO_SOURCE.get(mode, "unknown")
            # If mode/source changes, blank now-playing because old metadata is very likely stale.
            if mode != old_mode or self._state.source != new_source:
                self._state.source = new_source
                changed = True
                _clear_now_playing(clear_source_detail=True)
            else:
                self._state.source = new_source
                changed = True

            # "idle" implies not playing. Treat mode == "000" as idle (per your mapping).
            if mode == "000":
                if self._state.transport != "idle":
                    self._state.transport = "idle"
                    changed = True
                # Clear now-playing when idle to avoid stale track display
                _clear_now_playing()

        elif p.startswith("AXX+MEA+DAT"):
            data = _parse_json_after_prefix("AXX+MEA+DAT")
            if isinstance(data, dict):
                # These are often hex-encoded
                t = _maybe_decode_text(str(data.get("title", "") or ""))
                a = _maybe_decode_text(str(data.get("artist", "") or ""))
                al = _maybe_decode_text(str(data.get("album", "") or ""))

                vend = _maybe_decode_text(str(data.get("vendor", "") or ""))
                if vend and self._state.source_detail != vend:
                    self._state.source_detail = vend
                    changed = True

                url_raw = str(data.get("url", "") or "").strip()
                url = _maybe_decode_text(url_raw) if url_raw else ""

                # Apply only if any meaningful field is present.
                if t or a or al or url:
                    # Normalize empty strings to None
                    nt = t or None
                    na = a or None
                    nal = al or None
                    if self._state.title != nt:
                        self._state.title = nt
                        changed = True
                    if self._state.artist != na:
                        self._state.artist = na
                        changed = True
                    if self._state.album != nal:
                        self._state.album = nal
                        changed = True
                    if url:
                        if self._state.track_uri != url:
                            self._state.track_uri = url
                            changed = True

        elif p.startswith("AXX+SNG+INF"):
            # Short progress/status; use it to keep transport fresh, but ignore weird statuses.
            data = _parse_json_after_prefix("AXX+SNG+INF")
            if isinstance(data, dict):
                st = str(data.get("status", "")).lower().strip()
                if st in _STATUS_TO_TRANSPORT:
                    new_transport = _STATUS_TO_TRANSPORT[st]
                    if self._state.transport != new_transport:
                        self._state.transport = new_transport
                        changed = True
                    if new_transport in ("idle", "stopped", "stop"):
                        _clear_now_playing()

                vend = _maybe_decode_text(str(data.get("vendor", "") or ""))
                if vend and self._state.source_detail != vend:
                    self._state.source_detail = vend
                    changed = True

        # If anything meaningful changed, bump "last_update" (keep "last_notify" separate).
        if changed:
            self._state.last_update_ts = now
    
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
