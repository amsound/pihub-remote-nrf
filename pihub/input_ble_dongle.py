"""Bluetooth-over-USB dongle link (serial / CDC ACM).

This module replaces the older split implementation (ble_serial.py + controller.py + hid_client.py)
with a single file while keeping the same *behavioral contract*:

- Resilient auto-healing serial transport (reconnect w/ jittered backoff)
- ASCII control plane (PING/STATUS/UNPAIR, EVT/STATUS/PONG replies)
- Hot path key delivery:
    - Default: binary frames:
        - keyboard: 0x01 + 8-byte boot keyboard report
        - consumer: 0x02 + 2-byte little-endian consumer usage
    - Optional legacy text frames if your firmware expects them:
        - "KB <16hex>\n" and "CC <4hex>\n"
      Set PIHUB_SERIAL_HOTPATH=text to enable.

HID usage tables are loaded from packaged assets: pihub.assets/hid_keymap.json
with schema:
  { "keyboard": { "<name>": <hid_usage_id>, ... },
    "consumer": { "<name>": <usage_id>, ... } }

Public API matches how the app/dispatcher previously used BTLEController/HIDClient/BleSerial:
  - start()/stop()
  - wait_ready()
  - status property
  - key_down()/key_up()
  - send_key()/run_macro()
  - ping()/status_cmd()/unpair()
  - compile_ble_frames()/compiled_key_down()/compiled_key_up()
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

import serial  # pyserial
from serial import SerialException

try:
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover
    import importlib_resources

Usage = Literal["keyboard", "consumer"]

logger = logging.getLogger(__name__)


# ------------------------- compiled frames -------------------------

@dataclass(frozen=True)
class CompiledBleFrames:
    """Precompiled hot-path frames for a single activity's BLE bindings."""
    kb_down: Dict[str, bytes]  # code -> payload (bin or text)
    kb_up: Dict[str, bytes]
    cc_down: Dict[str, bytes]
    cc_up: Dict[str, bytes]


# ------------------------- dongle state -------------------------

@dataclass
class DongleState:
    ready: bool = False
    advertising: bool = False
    connected: bool = False
    proto_boot: bool = False
    error: bool = False

    sec: int = 0
    suspend: bool = False
    notify: Optional[Dict[str, bool]] = None

    conn_params: Optional[Dict[str, Any]] = None
    phy: Optional[Dict[str, Any]] = None
    last_disc_reason: Optional[int] = None


def discover_cmd_ports() -> List[str]:
    """Best-effort port discovery (Linux + macOS)."""
    ports: List[str] = []
    ports += sorted(glob.glob("/dev/ttyACM*"))
    # Dedup preserving order
    seen = set()
    out: List[str] = []
    for p in ports:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ------------------------- main link -------------------------

class BleDongleLink:
    def __init__(
        self,
        serial_port: str = "auto",
        baud: int = 115200,
        *,
        ping_timeout_s: float = 3.0,
        reconnect_delay_s: float = 0.5,
        status_poll_s: float = 30.0,
        **_ignored: object,
    ) -> None:
        self._serial_port = (serial_port or "auto").strip()
        self._baud = int(baud)

        # Ports list: env override wins, then explicit, then discovery.
        env_port = os.getenv("BLE_SERIAL_DEVICE", "").strip()
        requested = (env_port or self._serial_port).strip()
        if requested and requested.lower() != "auto":
            self._ports = [requested]
        else:
            self._ports = discover_cmd_ports() or ["/dev/ttyACM0"]

        self._ping_timeout_s = float(ping_timeout_s)
        self._reconnect_delay_s = float(reconnect_delay_s)
        self._status_poll_s = float(status_poll_s)

        # Hot path framing mode: bin (default) or text (legacy)
        self._hotpath = os.getenv("PIHUB_SERIAL_HOTPATH", "bin").strip().lower()
        if self._hotpath not in {"bin", "text"}:
            self._hotpath = "bin"

        # HID usage tables from assets
        self._kb, self._cc = self._load_hid_tables()

        # Serial handle + state
        self._ser: Optional[serial.Serial] = None
        self._active_port: Optional[str] = None
        self.state = DongleState()

        # Tasks
        self._connect_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._status_poll_task: Optional[asyncio.Task] = None

        # TX path
        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._tx_lock = asyncio.Lock()

        # RX line buffer
        self._rx_buf = bytearray()

        # Handshake tracking
        self._pong_counter = 0
        self._ready_evt = asyncio.Event()

        # compiled frames cache
        self._compiled: Optional[CompiledBleFrames] = None

        self._started = False

        logger.info("initialised port=%s baud=%s hotpath=%s", self._ports[0] if self._ports else None, self._baud, self._hotpath)

    # ---------- lifecycle ----------

    @property
    def is_open(self) -> bool:
        return self._ser is not None and getattr(self._ser, "is_open", False)

    @property
    def active_port(self) -> Optional[str]:
        return self._active_port

    @property
    def serial_ready(self) -> bool:
        # "ready" is your firmware’s LINK_READY (connected+secured+notifying)
        return bool(self.is_open and self.state.ready)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.create_task(self._connect_loop(), name="ble-dongle-connect")

    async def stop(self) -> None:
        self._started = False
        for t in (self._connect_task, self._reader_task, self._keepalive_task, self._status_poll_task):
            if t and not t.done():
                t.cancel()
        for t in (self._connect_task, self._reader_task, self._keepalive_task, self._status_poll_task):
            if t:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        self._connect_task = self._reader_task = self._keepalive_task = self._status_poll_task = None
        self._close()

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        if self.state.ready:
            return True
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- status / health snapshot ----------

    @property
    def status(self) -> dict:
        return {
            "adapter_present": self.is_open,
            "active_port": self._active_port,
            "ready": self.state.ready,
            "advertising": self.state.advertising,
            "connected": self.state.connected,
            "proto_boot": self.state.proto_boot,
            "sec": getattr(self.state, "sec", 0),
            "suspend": getattr(self.state, "suspend", False),
            "notify": dict(self.state.notify) if self.state.notify else None,
            "error": self.state.error,
            "conn_params": dict(self.state.conn_params) if self.state.conn_params else None,
            "phy": dict(self.state.phy) if self.state.phy else None,
            "last_disc_reason": self.state.last_disc_reason,
        }

    # ---------- Dispatcher / HIDClient-style surface ----------

    def _link_ready(self) -> bool:
        return bool(self.serial_ready and self.state.connected)

    def notify_keyboard(self, report: bytes) -> None:
        """Transport hook used by HID encoding path."""
        if not self._link_ready():
            return
        if len(report) != 8:
            return
        if self._hotpath == "text":
            payload = ("KB " + report.hex().upper() + "\n").encode("ascii")
        else:
            payload = b"\x01" + report
        self._enqueue_nowait(payload)

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        """Transport hook used by HID encoding path."""
        if not self._link_ready():
            return
        usage = int(usage_id) if pressed else 0
        usage &= 0xFFFF
        le = bytes((usage & 0xFF, (usage >> 8) & 0xFF))
        if self._hotpath == "text":
            payload = ("CC " + le.hex().upper() + "\n").encode("ascii")
        else:
            payload = b"\x02" + le
        self._enqueue_nowait(payload)

    # edge-level API (sync, hot path)
    def key_down(self, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            down = self._encode_keyboard_down(code)
            if down is None:
                return
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("keyboard %s down", code)
            self.notify_keyboard(down)
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("consumer %s down (0x%04X)", code, usage_id)
            if usage_id:
                self.notify_consumer(usage_id, True)

    def key_up(self, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            if code not in self._kb:
                logger.warning("unknown keyboard code %s up; ignoring", code)
                return
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("keyboard %s up", code)
            self.notify_keyboard(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("consumer %s up (0x%04X)", code, usage_id)
            if usage_id:
                self.notify_consumer(usage_id, False)

    # tap API (async)
    async def send_key(self, *, usage: Usage, code: str, hold_ms: int = 40) -> None:
        self.key_down(usage=usage, code=code)
        await asyncio.sleep(max(0, int(hold_ms)) / 1000.0)
        self.key_up(usage=usage, code=code)

    async def run_macro(
        self,
        steps: List[Dict[str, Any]],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        for step in steps:
            if "wait_ms" in step:
                await asyncio.sleep(max(0, int(step["wait_ms"])) / 1000.0)
                continue
            usage = step.get("usage")
            code = step.get("code")
            hold = int(step.get("hold_ms", default_hold_ms))
            if isinstance(usage, str) and isinstance(code, str):
                await self.send_key(usage=usage, code=code, hold_ms=hold)
                await asyncio.sleep(max(0, int(inter_delay_ms)) / 1000.0)

    # ---------- compiled hot path ----------

    def compile_ble_frames(self, bindings: Dict[str, List[Dict[str, Any]]]) -> CompiledBleFrames:
        kb_down: Dict[str, bytes] = {}
        kb_up: Dict[str, bytes] = {}
        cc_down: Dict[str, bytes] = {}
        cc_up: Dict[str, bytes] = {}

        for _rem_key, actions in (bindings or {}).items():
            for a in actions or []:
                if (a or {}).get("do") != "ble":
                    continue
                usage = a.get("usage")
                code = a.get("code")
                if not (isinstance(usage, str) and isinstance(code, str)):
                    continue

                if usage == "keyboard":
                    down = self._encode_keyboard_down(code)
                    if down is None:
                        continue
                    up = b"\x00\x00\x00\x00\x00\x00\x00\x00"
                    if self._hotpath == "text":
                        kb_down[code] = ("KB " + down.hex().upper() + "\n").encode("ascii")
                        kb_up[code] = ("KB " + up.hex().upper() + "\n").encode("ascii")
                    else:
                        kb_down[code] = b"\x01" + down
                        kb_up[code] = b"\x01" + up

                elif usage == "consumer":
                    usage_id = self._encode_consumer_usage(code)
                    if not usage_id:
                        continue
                    le = bytes((usage_id & 0xFF, (usage_id >> 8) & 0xFF))
                    z2 = b"\x00\x00"
                    if self._hotpath == "text":
                        cc_down[code] = ("CC " + le.hex().upper() + "\n").encode("ascii")
                        cc_up[code] = ("CC " + z2.hex().upper() + "\n").encode("ascii")
                    else:
                        cc_down[code] = b"\x02" + le
                        cc_up[code] = b"\x02" + z2

        frames = CompiledBleFrames(kb_down=kb_down, kb_up=kb_up, cc_down=cc_down, cc_up=cc_up)
        self._compiled = frames
        return frames

    def compiled_key_down(self, frames: CompiledBleFrames, *, usage: Usage, code: str) -> None:
        payload = frames.kb_down.get(code) if usage == "keyboard" else frames.cc_down.get(code)
        if payload:
            self._enqueue_nowait(payload)

    def compiled_key_up(self, frames: CompiledBleFrames, *, usage: Usage, code: str) -> None:
        payload = frames.kb_up.get(code) if usage == "keyboard" else frames.cc_up.get(code)
        if payload:
            self._enqueue_nowait(payload)

    # ---------- ops / control plane ----------

    async def ping(self) -> None:
        if self.is_open:
            await self._write_line("PING")

    async def status_cmd(self) -> None:
        if self.is_open:
            await self._write_line("STATUS")

    async def unpair(self) -> None:
        if self.is_open:
            await self._write_line("UNPAIR")
            # Some firmware emits no EVT for UNPAIR; force a status resync.
            asyncio.create_task(self._delayed_status_resync(0.25), name="ble-dongle-unpair-status")

    # ---------- internals: connect / io ----------

    def _close(self) -> None:
        try:
            if self._ser is not None:
                self._ser.close()
        finally:
            self._ser = None
            self._active_port = None
            self.state = DongleState()
            self._ready_evt.clear()

    @staticmethod
    def _sleep_with_jitter(base_s: float, jitter_frac: float = 0.25) -> float:
        j = base_s * jitter_frac
        return max(0.05, base_s + random.uniform(-j, j))

    async def _connect_loop(self) -> None:
        backoff = self._reconnect_delay_s
        backoff_max = 8.0

        while self._started:
            if self.is_open:
                await asyncio.sleep(0.25)
                continue

            connected = False
            for port in list(self._ports):
                try:
                    if await self._try_open_and_handshake(port):
                        logger.info("port ready on %s", port)
                        # emit an INFO line once status settles
                        asyncio.create_task(self._log_state_once_after_open(), name="ble-dongle-state-once")

                        if self._keepalive_task is None or self._keepalive_task.done():
                            self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="ble-dongle-keepalive")

                        if self._status_poll_s > 0 and (self._status_poll_task is None or self._status_poll_task.done()):
                            self._status_poll_task = asyncio.create_task(self._status_poll_loop(), name="ble-dongle-status-poll")

                        connected = True
                        backoff = self._reconnect_delay_s
                        break

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("open/handshake failed on %s: %r", port, exc)

            if connected:
                continue

            await asyncio.sleep(self._sleep_with_jitter(backoff))
            backoff = min(backoff * 2.0, backoff_max)

    async def _try_open_and_handshake(self, port: str) -> bool:
        ser = serial.Serial(
            port=port,
            baudrate=self._baud,
            timeout=0.1,
            write_timeout=0.5,
            exclusive=True,
        )
        try:
            ser.setDTR(True)
            ser.setRTS(True)
        except Exception:
            pass
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass

        self._ser = ser
        self._active_port = port
        self.state = DongleState()
        self._pong_counter = 0
        self._ready_evt.clear()

        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._reader_loop(), name="ble-dongle-reader")
        # writer loop is implicit via _tx_q + _write_payload()
        await asyncio.sleep(0.15)

        ok = await self._handshake_once(timeout_s=1.0)
        if ok:
            self._request_status_resync()
        if not ok:
            await self._force_reconnect("handshake_timeout")
        return ok

    async def _force_reconnect(self, reason: str) -> None:
        logger.warning("link lost; will retry") if reason != "handshake_timeout" else logger.debug("forcing reconnect (%s)", reason)
        self._close()

    async def _handshake_once(self, *, timeout_s: float = 1.0) -> bool:
        await self._write_line("PING")
        try:
            await asyncio.wait_for(self._wait_pong_increment(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def _wait_pong_increment(self) -> None:
        start = self._pong_counter
        while self._pong_counter == start:
            await asyncio.sleep(0.01)

    def _request_status_resync(self) -> None:
        asyncio.create_task(self.status_cmd(), name="ble-dongle-status")

    def _maybe_log_state(self, *, src: str) -> None:
        """Log state changes at INFO to match the old ble_serial.py behavior."""
        # snapshot
        adv = self.state.advertising
        conn = self.state.connected
        ready = self.state.ready

        prev = getattr(self, "_last_logged_state", None)
        cur = (adv, conn, ready, self.state.sec, bool(self.state.error))
        if prev == cur:
            return
        self._last_logged_state = cur
        logger.info("state: adv=%d conn=%d ready=%d sec=%s err=%d (%s)",
                    1 if adv else 0,
                    1 if conn else 0,
                    1 if ready else 0,
                    self.state.sec,
                    1 if self.state.error else 0,
                    src)

    async def _log_state_once_after_open(self) -> None:
        await asyncio.sleep(0.25)
        logger.info("state: adv=%s conn=%s ready=%s", int(self.state.advertising), int(self.state.connected), int(self.state.ready))

    async def _keepalive_loop(self) -> None:
        while self._started and self.is_open:
            try:
                await asyncio.sleep(max(0.5, self._ping_timeout_s))
                if not self.is_open:
                    return
                prev = self._pong_counter
                await self._write_line("PING")
                # wait for pong to bump
                t0 = time.monotonic()
                while time.monotonic() - t0 < self._ping_timeout_s:
                    if self._pong_counter != prev:
                        break
                    await asyncio.sleep(0.05)
                else:
                    await self._force_reconnect("ping_timeout")
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("keepalive error: %r", exc)
                await self._force_reconnect("keepalive_error")
                return

    async def _status_poll_loop(self) -> None:
        while self._started and self.is_open:
            try:
                await asyncio.sleep(self._status_poll_s)
                if self.is_open:
                    await self._write_line("STATUS")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("status poll error: %r", exc)

    def _enqueue_nowait(self, payload: bytes) -> None:
        if not self._link_ready():
            return
        try:
            self._tx_q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug("tx queue full; dropping payload (%d bytes)", len(payload))

    async def _write_line(self, line: str) -> None:
        await self._write_payload((line.strip() + "\n").encode("ascii", errors="replace"))

    async def _write_payload(self, payload: bytes) -> None:
        if not self.is_open:
            return
        try:
            # Keep ordering by funneling through a single writer lock.
            loop = asyncio.get_running_loop()
            async with self._tx_lock:
                await loop.run_in_executor(None, self._ser.write, payload)  # type: ignore[arg-type]
                await loop.run_in_executor(None, self._ser.flush)  # type: ignore[arg-type]
        except Exception as exc:
            logger.warning("writer error, reconnecting: %r", exc)
            await self._force_reconnect("writer_error")

    async def _reader_loop(self) -> None:
        while self._started and self.is_open:
            try:
                if self._ser is None:
                    await asyncio.sleep(0.1)
                    continue
                line = await self._read_line()
                if not line:
                    continue

                if line == "PONG":
                    self._pong_counter += 1
                    # A PONG means the link is alive
                    return_ready = not self.state.ready
                    if return_ready:
                        # don't force ready true here; READY comes from EVT/STATUS
                        pass
                    continue

                if line.startswith("STATUS "):
                    self._handle_status_line(line)
                    continue

                if line.startswith("EVT "):
                    self._handle_evt_line(line)
                    continue

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("reader error, reconnecting: %r", exc)
                await self._force_reconnect("reader_error")
                return

    async def _read_line(self) -> str:
        # simple async wrapper around serial.readline()
        if self._ser is None:
            return ""
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, self._ser.readline)  # type: ignore[arg-type]
        if not raw:
            return ""
        try:
            return raw.decode("ascii", errors="replace").strip()
        except Exception:
            return ""

    # ---------- STATUS parsing (matches ble_serial.py) ----------

    @staticmethod
    def _parse_kv_tokens(tokens: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for tok in tokens:
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            if k:
                out[k] = v
        return out

    @staticmethod
    def _to_int(v: Optional[str], default: int = 0) -> int:
        try:
            return int(v, 10) if v is not None else default
        except Exception:
            return default

    @staticmethod
    def _to_float(v: Optional[str], default: float = 0.0) -> float:
        try:
            return float(v) if v is not None else default
        except Exception:
            return default

    def _handle_status_line(self, line: str) -> None:
        parts = line.split()
        if len(parts) < 2 or parts[0] != "STATUS":
            return

        kv = self._parse_kv_tokens(parts[1:])

        self.state.advertising = (kv.get("adv") == "1")
        self.state.connected = (kv.get("conn") == "1")
        self.state.ready = (kv.get("ready") == "1")
        self.state.proto_boot = (kv.get("proto") == "1")
        self.state.error = (kv.get("err") == "1")
        self.state.sec = self._to_int(kv.get("sec"), 0)
        self.state.suspend = (kv.get("suspend") == "1")

        # Keep ready event in sync
        if self.state.ready:
            self._ready_evt.set()
        else:
            self._ready_evt.clear()

        self._maybe_log_state(src="STATUS")

        notify: Dict[str, bool] = {}
        for k in ("kb_notify", "boot_notify", "cc_notify", "batt_notify"):
            if k in kv:
                notify[k] = (kv[k] == "1")
        self.state.notify = notify if notify else None

        conn_params: Dict[str, Any] = {}
        if "interval_ms_x100" in kv:
            conn_params["interval_ms_x100"] = self._to_int(kv.get("interval_ms_x100"))
        if "interval_ms" in kv:
            interval_ms = self._to_float(kv.get("interval_ms"), 0.0)
            conn_params["interval_ms_x100"] = int(round(interval_ms * 100.0))
        if "latency" in kv:
            conn_params["latency"] = self._to_int(kv.get("latency"))
        if "timeout_ms" in kv:
            conn_params["timeout_ms"] = self._to_int(kv.get("timeout_ms"))
        if conn_params:
            self.state.conn_params = conn_params

        phy: Dict[str, Any] = {}
        if "phy_tx" in kv:
            phy["tx"] = self._to_int(kv.get("phy_tx"))
        if "phy_rx" in kv:
            phy["rx"] = self._to_int(kv.get("phy_rx"))
        if phy:
            self.state.phy = phy

        if self.state.ready:
            self._ready_evt.set()
        else:
            self._ready_evt.clear()

    # ---------- EVT parsing (subset from ble_serial.py) ----------

    def _handle_evt_line(self, line: str) -> None:
        parts = line.split()
        if len(parts) < 3:
            return
        kind = parts[1]
        rest = parts[2:]

        if kind == "READY":
            self.state.ready = (rest[0] == "1")
            if self.state.ready:
                self._ready_evt.set()
                self._request_status_resync()
            else:
                self._ready_evt.clear()
            self._maybe_log_state(src="EVT READY")
            return

        if kind == "ADV":
            self.state.advertising = (rest[0] == "1")
            self._maybe_log_state(src="EVT ADV")
            return

        if kind == "CONN":
            self.state.connected = (rest[0] == "1")
            if not self.state.connected:
                self.state.ready = False
                self._ready_evt.clear()
            # Force status resync after connect/disconnect to pick up conn_params/phy.
            self._request_status_resync()
            self._maybe_log_state(src="EVT CONN")
            return

        if kind == "DISC" and rest:
            try:
                self.state.last_disc_reason = int(rest[0], 0)
            except Exception:
                self.state.last_disc_reason = None
            self._maybe_log_state(src="EVT DISC")
            return

        if kind == "ERR":
            self.state.error = True
            self._maybe_log_state(src="EVT ERR")
            return

        # ignore other EVT kinds for now (PROTO/CONN_PARAMS/PHY etc. are surfaced via STATUS)

    # ---------- HID table loading / encoding ----------

    def _load_hid_tables(self) -> Tuple[Dict[str, int], Dict[str, int]]:
        identifier = "pihub.assets:hid_keymap.json"
        try:
            resource = importlib_resources.files("pihub.assets") / "hid_keymap.json"
            raw = resource.read_text(encoding="utf-8")
            data = json.loads(raw) or {}
        except Exception as exc:
            logger.warning("failed to load %s (%r); BLE key encoding will be disabled", identifier, exc)
            data = {}

        kb = {str(k): int(v) for k, v in (data.get("keyboard") or {}).items()} if isinstance(data, dict) else {}
        cc = {str(k): int(v) for k, v in (data.get("consumer") or {}).items()} if isinstance(data, dict) else {}
        return kb, cc

    def _encode_keyboard_down(self, code: str) -> Optional[bytes]:
        hid = self._kb.get(code)
        if hid is None:
            logger.warning("unknown keyboard code %s down; ignoring", code)
            return None
        if not (0 <= int(hid) <= 255):
            return None
        return bytes([0x00, 0x00, int(hid), 0x00, 0x00, 0x00, 0x00, 0x00])

    def _encode_consumer_usage(self, code: str) -> int:
        return int(self._cc.get(code) or 0)