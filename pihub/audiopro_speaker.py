"""Audio Pro/LinkPlay/WiiM speaker controller (TCP API only)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

HANDSHAKE_TIMEOUT_S = 5.0

_TCP_DEFAULT_PORT = 8899
_TCP_HEADER = b"\x18\x96\x18\x20"  # 0x18 0x96 0x18 0x20

_HTTPAPI_PATH = "/httpapi.asp"
_HTTP_TIMEOUT_S = 2.0
SEND_TIMEOUT_S = 2.0        # fail fast if a TCP send/drain stalls
DEFAULT_HTTP_POWEROFF_CMD = "setShutdown:0"
HTTP_SCHEME = "https"

VOLUME_STEP_PCT = 2

HDMI_SOFT_MUTE_RESTORE_DEFAULT_PCT = 30

HINT_PINFGET_DELAY_S = 1.0
PINFGET_MIN_GAP_S = 1.0
PINFGET_HINT_DELAY_S = 1.0
PINFGET_TRIGGER_PMS = {"000", "001"}
PINFGET_TRIGGER_KEYS = {"FFF"}  # only these KEY values trigger PINFGET

_PLM_TO_SOURCE = {
    "000": "idle",
    "040": "line-in",
    "041": "bluetooth",
    "043": "optical",
    "049": "hdmi",

    "001": "airplay",
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
_NETWORK_SOURCES = {"wifi", "airplay", "multiroom-secondary"}

_UNSET = object()

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
    connected: bool = False             # TCP socket is up
    ready: bool = False                 # becomes true only after first successful PINFGET parse
    last_error: str | None = None

    playback_status: str | None = None  # play/pause/stop/load/... (wifi-ish only; physical inputs => None)
    volume: float | None = None         # 0..1
    muted: bool | None = None
    source: str | None = None
    source_detail: str | None = None    # Specifically for WiFi source mode like TuneIn or Custom URL

    last_update_ts: float | None = None
    last_play_ts: float | None = None


class AudioProSpeaker:
    """
    TCP API driver.

      - start/stop
      - set volume/volume_up/volume_down/mute_toggle (+ set_muted)
      - play/pause/stop_playback/next_track/previous_track/play_pause
      - set preset/next_preset/previous_preset
      - play_url (uses httpapi.asp with cmd "setURL:{url}")
      - set_source 
      - power_off (uses httpapi.asp with default cmd "setShutdown:0")
    """

    def __init__(
        self,
        *,
        speaker_ip: str,
        tcp_port: int = _TCP_DEFAULT_PORT,
        command_interval_s: float = 0.2,
        reconnect_s: float = 3.0,
        connect_timeout_s: float = 3.0,
        state_change_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._speaker_ip = speaker_ip.strip()
        self._tcp_port = int(tcp_port)
        self._volume_step_pct = _clamp_int(int(VOLUME_STEP_PCT), 1, 10)
        self._command_interval_s = max(0.2, float(command_interval_s))  # doc says >=200ms
        
        self._reconnect_s = max(1.0, float(reconnect_s))
        self._connect_timeout_s = max(1.0, float(connect_timeout_s))

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
        self._poll_wake_evt = asyncio.Event()
        self._log_drop_once = False
        self._log_http_drop_once = False
        self._link_down_logged = False

        self._physical_mute_restore_pct: int | None = None
        self._physical_muted = False

        # State change callback for external state change
        self._state_change_callback = state_change_callback

        self._pending_pinfget_task: asyncio.Task | None = None
        self._last_pinfget_monotonic = 0.0

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
            "backend": "audiopro",
            "reachable": bool(s.reachable),
            "connected": bool(s.connected),
            "ready": bool(s.ready),
            "playback_status": s.playback_status,
            "volume_pct": None if s.volume is None else int(round(s.volume * 100)),
            "muted": s.muted,
            "source": s.source,
            "source_detail": self._state.source_detail,
            "last_update_ts": last_i,
            "update_age_s": age_i,
        }

    def _wake_poll_loop(self) -> None:
        self._poll_wake_evt.set()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._stop_evt.clear()
        self._poll_wake_evt.clear()

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            # Keep it simple; LinkPlay HTTPS is often self-signed
            self._session = aiohttp.ClientSession(timeout=timeout)

        self._task = asyncio.create_task(self._runner(), name=f"linkplay_tcp[{self._speaker_ip}]")

    async def stop(self) -> None:
        self._enabled = False
        self._stop_evt.set()
        self._cancel_pending_pinfget()

        self._poll_wake_evt.set()

        await self._cancel_task(self._task, name="runner_task")
        self._task = None

        await self._cancel_task(self._poll_task, name="poll_task")
        self._poll_task = None

        await self._disconnect()

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def request_refresh(self) -> dict[str, Any]:
        """
        Best-effort immediate state refresh.

        Returns structured outcome so operator APIs can report truthfully whether
        the refresh path actually succeeded.
        """
        if not self.enabled:
            return {
                "ok": False,
                "outcome": "unavailable",
                "errors": ["speaker_disabled"],
            }

        errors: list[str] = []
        pinfget_sent = False

        try:
            self._wake_poll_loop()
        except Exception as exc:
            logger.debug("speaker request_refresh wake failed", exc_info=True)
            errors.append(f"wake_failed:{exc!r}")

        if self._state.connected:
            try:
                await self._pinfget()
                pinfget_sent = True
            except Exception as exc:
                logger.debug("speaker request_refresh pinfget failed", exc_info=True)
                errors.append(f"pinfget_failed:{exc!r}")

        if self._state.connected and not pinfget_sent:
            return {
                "ok": False,
                "outcome": "refresh_failed",
                "errors": errors,
            }

        return {
            "ok": True,
            "outcome": "refresh_requested",
            "errors": errors,
        }

    async def _runner(self) -> None:
        attempt = 0

        while self._enabled and not self._stop_evt.is_set():
            read_task: asyncio.Task | None = None
            poll_task: asyncio.Task | None = None

            try:
                attempt += 1
                logger.debug(
                    "speaker tcp connect attempt speaker_ip=%s attempt=%s reconnect_s=%s",
                    self._speaker_ip,
                    attempt,
                    self._reconnect_s,
                )

                await self._connect()

                poll_task = asyncio.create_task(
                    self._poll_loop(),
                    name=f"linkplay_poll[{self._speaker_ip}]",
                )
                self._poll_task = poll_task

                read_task = asyncio.create_task(
                    self._read_loop(),
                    name=f"linkplay_read[{self._speaker_ip}]",
                )

                # Initial truth pull / readiness gate.
                await self._pinfget()
                await self._wait_ready()
                attempt = 0

                # Either background task dying means this connection cycle is over.
                done, _ = await asyncio.wait(
                    {read_task, poll_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc

                raise ConnectionError("speaker task exited unexpectedly")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._mark_down(str(e))
                if not self._link_down_logged:
                    logger.warning(
                        "speaker unavailable err=%r; continuing without connection",
                        e,
                    )
                    self._link_down_logged = True
            finally:
                # Tear down transport first so any in-flight read/drain wakes up quickly.
                await self._disconnect()

                await self._cancel_task(read_task, name="read_task")
                await self._cancel_task(poll_task, name="poll_task")
                self._poll_task = None

            if self._enabled and not self._stop_evt.is_set():
                await asyncio.sleep(self._reconnect_s)

    async def _connect(self) -> None:
        self._state.last_error = None
        self._state.reachable = False
        self._state.connected = False
        self._state.ready = False
        self._log_drop_once = False
        self._log_http_drop_once = False
        self._poll_wake_evt.clear()

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._speaker_ip, self._tcp_port),
                timeout=self._connect_timeout_s,
            )
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"connect timeout after {self._connect_timeout_s:.1f}s"
            ) from e

        self._reader, self._writer = reader, writer
        self._state.reachable = True
        self._state.connected = True
        self._state.last_update_ts = _now()
        self._link_down_logged = False
        self._wake_poll_loop()
        logger.info("speaker tcp connected speaker_ip=%s port=%s", self._speaker_ip, self._tcp_port)

    async def _disconnect(self) -> None:
        self._cancel_pending_pinfget()
        self._state.reachable = False
        self._state.connected = False
        self._state.ready = False
        self._state.last_update_ts = _now()
        self._wake_poll_loop()

        writer = self._writer
        self._reader, self._writer = None, None

        if writer:
            try:
                writer.close()
                with contextlib.suppress(Exception, asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    def _mark_down(self, err: str | None) -> None:
        self._state.reachable = False
        self._state.connected = False
        self._state.ready = False
        self._state.last_error = err
        self._state.last_update_ts = _now()
        self._wake_poll_loop()

    async def _fail_link(self, reason: str) -> None:
        self._mark_down(reason)
        await self._disconnect()
        raise ConnectionError(reason)

    async def _wait_ready(self, timeout_s: float = HANDSHAKE_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout_s
        while self._enabled and not self._stop_evt.is_set():
            if self._state.ready:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionError("handshake timeout")
            await asyncio.sleep(min(0.1, remaining))

    async def _cancel_task(self, task: asyncio.Task | None, *, name: str, timeout_s: float = 1.0) -> None:
        if task is None:
            return

        if task.done():
            with contextlib.suppress(Exception):
                task.result()
            return

        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout_s)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.debug(
                "speaker task cancel timed out speaker_ip=%s task=%s",
                self._speaker_ip,
                name,
            )
        except Exception as e:
            logger.debug(
                "speaker task finished with error during cancel speaker_ip=%s task=%s err=%r",
                self._speaker_ip,
                name,
                e,
            )

    # ---------- send / receive ----------

    async def _send(self, payload: str) -> None:
        """Low-level send (does not apply the ready gate)."""
        if not self._writer:
            raise ConnectionError("not connected")

        if _needs_amp(payload):
            payload = payload + "&"

        packet = _mk_packet(payload)
        send_error: Exception | None = None

        async with self._send_lock:
            now_m = time.monotonic()
            dt = now_m - self._last_send_monotonic
            if dt < self._command_interval_s:
                await asyncio.sleep(self._command_interval_s - dt)

            try:
                self._writer.write(packet)
                await asyncio.wait_for(self._writer.drain(), timeout=SEND_TIMEOUT_S)
                self._last_send_monotonic = time.monotonic()
                return
            except asyncio.TimeoutError as e:
                send_error = ConnectionError(f"send timeout after {SEND_TIMEOUT_S:.1f}s")
            except Exception as e:
                send_error = e

        await self._fail_link(f"send failed: {send_error}")

    async def _send_control(self, payload: str) -> bool:
        """
        Control-plane sends. Drop until ready=True (handshake complete).
        Returns True if sent, False if dropped.
        """
        if not self._state.ready:
            if not self._log_drop_once:
                logger.info("speaker link not ready yet; dropping command=%s", payload)
                self._log_drop_once = True
            return False
        try:
            await self._send(payload)
            return True
        except Exception as e:
            self._state.last_error = str(e)
            self._state.last_update_ts = _now()
            return False

    async def _http_control(self, cmd: str) -> bool:
        """
        HTTP control-plane sends. Use the same front-door policy as TCP:
        drop unless ready=True.

        Returns True if the HTTP request was attempted successfully,
        False if dropped or failed.
        """
        if not self._state.ready:
            if not self._log_http_drop_once:
                logger.info("speaker link not ready yet; dropping http command=%s", cmd)
                self._log_http_drop_once = True
            return False

        try:
            await self._http_cmd(cmd)
            return True
        except Exception as e:
            self._state.last_error = f"httpapi failed: {e}"
            self._state.last_update_ts = _now()
            return False

    async def _pinfget(self) -> None:
        """Single truth pull (allowed even before ready gate)."""
        if not self._writer:
            raise ConnectionError("not connected")
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
                    logger.debug("checksum mismatch speaker_ip=%s", self._speaker_ip)

                payload = _parse_payload(payload_bytes)
                if payload:
                    self._on_payload(payload)

    def _on_payload(self, payload: str) -> None:
        p = payload.strip()
        if p.endswith("&"):
            p = p[:-1]

        if p.startswith("AXX+VOL+"):
            self._handle_vol(p)
            return

        if p.startswith("AXX+MUT+"):
            self._handle_mut(p)
            return

        if p.startswith("AXX+PLM+"):
            self._handle_plm(p)
            return

        if p.startswith("AXX+PLY+INF"):
            self._handle_ply_inf(p)
            return

        if p.startswith("AXX+PMS+"):
            self._handle_pms(p)
            return

        if p.startswith("AXX+PLY+NEW"):
            self._handle_ply_new(p)
            return

        if p.startswith("AXX+KEY+"):
            self._handle_key(p)
            return

        self._handle_unknown(p)

    def _compute_poll_interval(self) -> float:
        src = (self._state.source or "unknown").strip().lower()
        st = (self._state.playback_status or "").strip().lower()

        if src in _PHYSICAL_SOURCES:
            return 10.0

        if src in _NETWORK_SOURCES:
            if st == "load":
                return 2.0
            if st == "play":
                return 5.0
            if st == "stop":
                if self._state.last_play_ts is not None:
                    age = _now() - self._state.last_play_ts
                    if age < 60.0:
                        return 10.0
                return 15.0
            return 10.0

        return 15.0
    
    async def _poll_loop(self) -> None:
        while self._enabled and not self._stop_evt.is_set():
            interval = self._compute_poll_interval()

            self._poll_wake_evt.clear()

            try:
                await asyncio.wait_for(
                    self._poll_wake_evt.wait(),
                    timeout=float(interval),
                )
                continue
            except asyncio.TimeoutError:
                pass

            await self._pinfget()
            self._last_pinfget_monotonic = time.monotonic()

    def _spawn_state_change_callback(self, name: str, payload: dict[str, Any]) -> None:
        cb = self._state_change_callback
        if cb is None:
            return

        try:
            task = asyncio.create_task(
                cb(name, payload),
                name=f"audiopro_state_change:{name}",
            )
        except Exception:
            logger.exception("speaker state change callback spawn failed name=%s", name)
            return

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                logger.debug("speaker state change callback cancelled name=%s", name)
            except Exception:
                logger.exception("speaker state change callback failed name=%s", name)

        task.add_done_callback(_done)

    def _emit_state_change(self, name: str, payload: dict[str, Any]) -> None:
        self._spawn_state_change_callback(name, payload)

    def _apply_updates(
        self,
        *,
        source: str | None | object = _UNSET,
        source_detail: str | None | object = _UNSET,
        playback_status: str | None | object = _UNSET,
        volume_pct: int | None = None,
        muted: bool | None = None,
        ready: bool | None = None,
    ) -> None:
        old_source = self._state.source
        old_status = self._state.playback_status
        old_ready = self._state.ready

        changed = False

        if source is not _UNSET and source != self._state.source:
            self._state.source = source
            changed = True

        if source_detail is not _UNSET and source_detail != self._state.source_detail:
            self._state.source_detail = source_detail
            changed = True

        if playback_status is not _UNSET and playback_status != self._state.playback_status:
            self._state.playback_status = playback_status
            changed = True
            if playback_status == "play":
                self._state.last_play_ts = _now()

        if volume_pct is not None:
            v = _clamp_int(int(volume_pct), 0, 100) / 100.0
            if self._state.volume != v:
                self._state.volume = v
                changed = True

        if muted is not None and muted != self._state.muted:
            self._state.muted = muted
            changed = True

        if ready is not None and ready != self._state.ready:
            self._state.ready = ready
            changed = True

        if changed:
            self._state.last_update_ts = _now()

        if (
            self._state.source != old_source
            or self._state.playback_status != old_status
            or self._state.ready != old_ready
        ):
            self._wake_poll_loop()

        self._emit_listen_edge(old_source, old_status)

    def _emit_listen_edge(self, old_source: str | None, old_status: str | None) -> None:
        def _active(source: str | None, status: str | None) -> bool:
            src = (source or "").strip().lower()
            st = (status or "").strip().lower()
            return src in _NETWORK_SOURCES and st in {"load", "play"}

        old_active = _active(old_source, old_status)
        new_active = _active(self._state.source, self._state.playback_status)

        if not old_active and new_active:
            self._emit_state_change(
                "listen",
                {
                    "domain": "speaker",
                    "source": self._state.source,
                    "playback_status": self._state.playback_status,
                },
            )

    def _request_pinfget(self, *, reason: str, delay_s: float = PINFGET_HINT_DELAY_S) -> None:
        if not self._enabled or self._stop_evt.is_set():
            return

        task = self._pending_pinfget_task
        if task is not None and not task.done():
            return

        self._pending_pinfget_task = asyncio.create_task(
            self._run_scheduled_pinfget(delay_s=delay_s, reason=reason),
            name=f"speaker_pinfget[{self._speaker_ip}]",
        )
        self._pending_pinfget_task.add_done_callback(self._clear_pending_pinfget)

    def _clear_pending_pinfget(self, task: asyncio.Task) -> None:
        if self._pending_pinfget_task is task:
            self._pending_pinfget_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("speaker scheduled PINFGET cancelled speaker_ip=%s", self._speaker_ip)
        except Exception as e:
            logger.debug("speaker scheduled PINFGET failed speaker_ip=%s error=%s", self._speaker_ip, e)

    async def _run_scheduled_pinfget(self, *, delay_s: float, reason: str) -> None:
        logger.debug(
            "speaker scheduling PINFGET speaker_ip=%s delay_s=%.2f reason=%s",
            self._speaker_ip,
            delay_s,
            reason,
        )

        if delay_s > 0:
            await asyncio.sleep(delay_s)

        if not self._enabled or self._stop_evt.is_set():
            logger.debug(
                "speaker scheduled PINFGET skipped speaker_ip=%s reason=%s state=disabled_or_stopped",
                self._speaker_ip,
                reason,
            )
            return

        now_mono = time.monotonic()
        since_last = now_mono - self._last_pinfget_monotonic
        if since_last < PINFGET_MIN_GAP_S:
            extra = PINFGET_MIN_GAP_S - since_last
            logger.debug(
                "speaker delaying PINFGET for min gap speaker_ip=%s extra_s=%.2f reason=%s",
                self._speaker_ip,
                extra,
                reason,
            )
            await asyncio.sleep(extra)

        if not self._enabled or self._stop_evt.is_set():
            logger.debug(
                "speaker scheduled PINFGET aborted speaker_ip=%s reason=%s state=disabled_or_stopped",
                self._speaker_ip,
                reason,
            )
            return

        logger.debug(
            "speaker running scheduled PINFGET speaker_ip=%s reason=%s",
            self._speaker_ip,
            reason,
        )
        await self._pinfget()
        self._last_pinfget_monotonic = time.monotonic()

    def _cancel_pending_pinfget(self) -> None:
        task = self._pending_pinfget_task
        if task is not None and not task.done():
            task.cancel()
        self._pending_pinfget_task = None

    def _parse_ply_inf_json(self, payload: str) -> dict[str, Any] | None:
        p = payload.strip()
        if p.endswith("&"):
            p = p[:-1]

        if not p.startswith("AXX+PLY+INF"):
            return None

        raw = p[len("AXX+PLY+INF"):].strip()
        if not (raw.startswith("{") and raw.endswith("}")):
            return None

        try:
            data = json.loads(raw)
        except Exception:
            return None

        return {
            "type": str(data.get("type", "")).strip(),
            "mode": str(data.get("mode", "")).strip().zfill(3),
            "vendor": str(data.get("vendor", "")).strip(),
            "status": str(data.get("status", "")).strip().lower(),
            "vol": str(data.get("vol", "")).strip(),
            "mute": str(data.get("mute", "")).strip(),
        }
    
    def _handle_vol(self, payload: str) -> None:
        try:
            vol_s = payload.strip().split("+")[-1]
            self._apply_updates(volume_pct=int(vol_s))
        except Exception:
            return

    def _handle_mut(self, payload: str) -> None:
        code = payload.strip().split("+")[-1]
        if code not in {"000", "001"}:
            return
        self._apply_updates(muted=(code == "001"))

    def _handle_plm(self, payload: str) -> None:
        mode = payload.strip().split("+")[-1].strip().zfill(3)
        source = _PLM_TO_SOURCE.get(mode, "unknown")

        if source in _PHYSICAL_SOURCES:
            self._apply_updates(source=source, source_detail=None, playback_status=None)
        else:
            self._apply_updates(source=source, source_detail=None)

        self._request_pinfget(reason=f"plm:{mode}")

    def _handle_pms(self, payload: str) -> None:
        code = payload.strip().split("+")[-1]
        if code in PINFGET_TRIGGER_PMS:
            self._request_pinfget(reason=f"pms:{code}")

    def _handle_ply_new(self, payload: str) -> None:
        self._request_pinfget(reason="ply_new")

    def _handle_key(self, payload: str) -> None:
        code = payload.strip().split("+")[-1]
        if code in PINFGET_TRIGGER_KEYS:
            self._request_pinfget(reason=f"key:{code}")

    def _handle_ply_inf(self, payload: str) -> None:
        info = self._parse_ply_inf_json(payload)
        if not info:
            return

        source = _PLM_TO_SOURCE.get(info["mode"], self._state.source or "unknown")
        status = info["status"]

        if source in _PHYSICAL_SOURCES:
            status = None
        elif source == "multiroom-secondary" and info["type"] == "1":
            status = "play"

        try:
            vol_pct = int(info["vol"])
        except Exception:
            vol_pct = None

        if info["mute"] == "0":
            muted = False
        elif info["mute"] == "1":
            muted = True
        else:
            muted = None

        was_ready = self._state.ready

        self._apply_updates(
            source=source,
            source_detail=info["vendor"] or None,
            playback_status=status,
            volume_pct=vol_pct,
            muted=muted,
            ready=True,
        )

        if not was_ready and self._state.ready:
            logger.info(
                "speaker tcp link ready speaker_ip=%s (initial status received)",
                self._speaker_ip,
            )

    def _handle_unknown(self, payload: str) -> None:
        return

    # ---------- controls (TCP) ----------

    async def _require_control_sent(self, ok: bool, *, action: str) -> None:
        if ok:
            return

        detail = str(self._state.last_error or "").strip()
        if detail:
            raise RuntimeError(f"{action}_not_sent: {detail}")
        raise RuntimeError(f"{action}_not_sent")

    async def _tcp_command(
        self,
        payload: str,
        *,
        action: str,
        refresh: bool = False,
        delayed_refresh: bool = False,
    ) -> None:
        ok = await self._send_control(payload)
        await self._require_control_sent(ok, action=action)

        if refresh:
            delay_s = 0.75 if delayed_refresh else 0.25
            self._request_pinfget(reason=f"cmd:{action}", delay_s=delay_s)

    async def _http_command(
        self,
        cmd: str,
        *,
        action: str,
        refresh: bool = False,
        delayed_refresh: bool = False,
    ) -> None:
        ok = await self._http_control(cmd)
        await self._require_control_sent(ok, action=action)

        if refresh:
            delay_s = 0.75 if delayed_refresh else 0.25
            self._request_pinfget(reason=f"http:{action}", delay_s=delay_s)

    # Refresh policy is intentional and firmware-specific:
    # - commands that do not reliably produce sufficient state updates schedule PINFGET
    # - commands where firmware already emits adequate state do not force a refresh

    async def volume_up(self) -> None:
        src = (self._state.source or "").strip().lower()

        if src == "hdmi" and self._physical_muted:
            restore = self._physical_mute_restore_pct
            if restore is None or restore <= 0:
                restore = HDMI_SOFT_MUTE_RESTORE_DEFAULT_PCT
            self._physical_muted = False
            self._state.muted = False
            nxt = _clamp_int(restore + self._volume_step_pct, 0, 100)
            await self._tcp_command(f"MCU+VOL+{nxt:03d}", action="volume_up")
            return

        v = self._state.volume
        cur = int(round((v or 0.0) * 100))
        nxt = _clamp_int(cur + self._volume_step_pct, 0, 100)
        await self._tcp_command(f"MCU+VOL+{nxt:03d}", action="volume_up")

    async def volume_down(self) -> None:
        src = (self._state.source or "").strip().lower()

        if src == "hdmi" and self._physical_muted:
            restore = self._physical_mute_restore_pct
            if restore is None or restore <= 0:
                restore = HDMI_SOFT_MUTE_RESTORE_DEFAULT_PCT
            self._physical_muted = False
            self._state.muted = False
            nxt = _clamp_int(restore - self._volume_step_pct, 0, 100)
            await self._tcp_command(f"MCU+VOL+{nxt:03d}", action="volume_down")
            return

        v = self._state.volume
        cur = int(round((v or 0.0) * 100))
        nxt = _clamp_int(cur - self._volume_step_pct, 0, 100)
        await self._tcp_command(f"MCU+VOL+{nxt:03d}", action="volume_down")

    async def set_volume(self, pct: int) -> None:
        target = _clamp_int(int(pct), 0, 100)
        await self._tcp_command(f"MCU+VOL+{target:03d}", action="set_volume")

    async def set_muted(self, target: bool) -> None:
        src = (self._state.source or "").strip().lower()

        # HDMI path: firmware mute appears to be ineffective, so emulate mute
        # by storing the current volume and setting volume to 0.
        if src == "hdmi":
            cur_pct = 0 if self._state.volume is None else int(round(self._state.volume * 100))

            if target:
                if not self._physical_muted:
                    if cur_pct > 0:
                        self._physical_mute_restore_pct = cur_pct
                    elif self._physical_mute_restore_pct is None:
                        self._physical_mute_restore_pct = HDMI_SOFT_MUTE_RESTORE_DEFAULT_PCT

                self._physical_muted = True
                self._apply_updates(muted=True)
                await self.set_volume(0)
                return

            restore = self._physical_mute_restore_pct
            if restore is None or restore <= 0:
                restore = HDMI_SOFT_MUTE_RESTORE_DEFAULT_PCT

            self._physical_muted = False
            self._apply_updates(muted=False)
            await self.set_volume(restore)
            return

        # Default firmware mute path for software/network inputs.
        current = self._state.muted
        if current is not None and bool(current) == bool(target):
            return

        new = "001" if target else "000"
        await self._tcp_command(f"MCU+MUT+{new}", action="set_muted")
        self._apply_updates(muted=target)

    async def mute_toggle(self) -> None:
        cur = bool(self._state.muted) if self._state.muted is not None else False
        await self.set_muted(not cur)

    async def play(self) -> None:
        await self._tcp_command("MCU+PLY-PLA", action="play", refresh=True)

    async def pause(self) -> None:
        await self._tcp_command("MCU+PLY-PUS", action="pause", refresh=True)

    async def stop_playback(self) -> None:
        await self._tcp_command("MCU+PLY-STP", action="stop_playback", refresh=True)

    async def next_track(self) -> None:
        await self._tcp_command("MCU+PLY+NXT", action="next_track")

    async def previous_track(self) -> None:
        await self._tcp_command("MCU+PLY+PRV", action="previous_track")

    async def play_pause(self) -> None:
        await self._tcp_command("MCU+PLY+PUS", action="play_pause", refresh=True)

    async def preset(self, n: int) -> None:
        n = _clamp_int(int(n), 1, 10)
        await self._tcp_command(f"MCU+KEY+{n:03d}", action="preset")

    async def next_preset(self) -> None:
        await self._tcp_command("MCU+KEY+NXT", action="next_preset")

    async def previous_preset(self) -> None:
        await self._tcp_command("MCU+KEY+PRE", action="previous_preset")

    async def set_source(self, source: str) -> None:
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
            raise RuntimeError(f"set_source_unsupported:{source}")

        await self._tcp_command(
            f"MCU+PLM+{want_mode}",
            action="set_source",
            refresh=True,
            delayed_refresh=True,
        )

    # -------------- HTTP API --------------

    async def power_off(self) -> None:
        """Courtesy amplifier off via HTTP API, but only when ready and actually sent."""
        cmd = getattr(self, "_http_poweroff_cmd", DEFAULT_HTTP_POWEROFF_CMD)
        await self._http_command(cmd, action="power_off")

    async def play_url(self, url: str) -> None:
        if not url:
            raise RuntimeError("play_url_missing")

        cmd = f"setPlayerCmd:play:{url}"
        await self._http_command(cmd, action="play_url", refresh=True)

    async def _http_cmd(self, cmd: str) -> None:
        if not self._session:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
            self._session = aiohttp.ClientSession(timeout=timeout)

        endpoint = f"{HTTP_SCHEME}://{self._speaker_ip}{_HTTPAPI_PATH}"
        params = {"command": cmd}

        async with self._session.get(endpoint, params=params, ssl=False) as resp:
            _ = await resp.text(errors="replace")
            if resp.status >= 400:
                raise RuntimeError(f"httpapi status {resp.status}")