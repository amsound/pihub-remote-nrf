"""
Bluetooth-over-USB dongle link (Serial / CDC ACM).

Serial transport to the nRF52840 dongle. Payloads are BLE HID reports:
- Hot path binary frames (device parser expects):
    - b"\x01" + 8 bytes keyboard report
    - b"\x02" + 2 bytes consumer usage (little-endian)
- Ops/control are ASCII lines: PING / STATUS / UNPAIR (newline terminated)
- Telemetry from dongle is ASCII lines: EVT ... / STATUS ... / OK / ERR ...

This module is SERIAL ONLY (no USB HID host libraries).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Literal

import serial  # pyserial
from serial import SerialException

try:
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover
    import importlib_resources

Usage = Literal["keyboard", "consumer"]

logger = logging.getLogger(__name__)


# ------------------------- Compiled frames -------------------------

@dataclass(frozen=True)
class CompiledBleFrames:
    """Precompiled binary frames for a set of (usage, code) pairs."""
    kb_down: Dict[str, bytes]          # code -> b"\x01" + report8
    kb_up: Dict[str, bytes]            # code -> b"\x01" + all-zeros report8
    cc_down: Dict[str, bytes]          # code -> b"\x02" + report2
    cc_up: Dict[str, bytes]            # code -> b"\x02" + b"\x00\x00"


# ------------------------- State -------------------------

@dataclass
class ConnParams:
    interval_ms: float = 0.0
    latency: int = 0
    timeout_ms: int = 0


@dataclass
class Phy:
    tx: int = 0
    rx: int = 0


@dataclass
class DongleState:
    ready: bool = False
    advertising: bool = False
    connected: bool = False
    # protocol mode from dongle STATUS: proto=1 => report, proto=0 => boot
    proto_report: bool = True
    sec: int = 0
    suspend: bool = False
    notify: Optional[Dict[str, int]] = None
    error: bool = False
    conn_params: Optional[ConnParams] = None
    phy: Optional[Phy] = None
    last_disc_reason: Optional[str] = None


# ------------------------- Main link -------------------------

class BleDongleLink:
    """
    Serial dongle link.

    Public API mirrors the previous controller usage:
      - start/stop
      - key_down/key_up
      - send_key/run_macro
      - ping/status_cmd/unpair
      - compile_ble_frames + compiled_key_down/up
      - status property
    """

    def __init__(
        self,
        *,
        serial_port: str = "auto",
        baud: int = 115200,
        tx_queue: int = 512,
        ping_interval_s: float = 0.0,
        status_poll_s: float = 0.0,
    ) -> None:
        self._serial_port_cfg = (serial_port or "auto").strip()
        self._baud = int(baud)
        self._ping_interval_s = float(ping_interval_s)
        self._status_poll_s = float(status_poll_s)

        self._ser: Optional[serial.Serial] = None
        self._port: Optional[str] = None

        self.state = DongleState()
        self._ready_evt = asyncio.Event()

        self._reader_task: Optional[asyncio.Task] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._resync_task: Optional[asyncio.Task] = None
        self._resync_delay_s: float = 0.15

        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=int(tx_queue))
        self._tx_lock = asyncio.Lock()

        self._rx_buf = bytearray()
        self._pong_counter = 0

        # reconnect knobs
        self._reconnect_delay_s = 0.5
        self._reconnect_delay_max_s = 8.0

        # optional tx trace
        self._tx_trace = bool(os.getenv("PIHUB_BLE_TX_TRACE", "").strip())

        # HID usage maps loaded from assets/hid_keymap.json
        self._hid_kb: Dict[str, int] = {}
        self._hid_cc: Dict[str, int] = {}
        self._load_hid_keymap()

        logger.info("initialised port=%s baud=%s hotpath=bin", self._serial_port_cfg, self._baud)

    # ---------- lifecycle ----------

    @property
    def is_open(self) -> bool:
        return self._ser is not None and bool(getattr(self._ser, "is_open", False))

    @property
    def active_path(self) -> Optional[str]:
        return self._port

    async def start(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name="ble-serial-reconnect")

    async def stop(self) -> None:
        for t in (self._poll_task, self._reconnect_task, self._reader_task, self._writer_task):
            if t and not t.done():
                t.cancel()
        for t in (self._poll_task, self._reconnect_task, self._reader_task, self._writer_task):
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await self._close_serial()

    async def wait_ready(self, timeout_s: float = 8.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- status ----------

    @property
    def status(self) -> dict:
        return {
            "adapter_present": self.is_open,
            "active_port": self._port,
            "ready": self.state.ready,
            "advertising": self.state.advertising,
            "connected": self.state.connected,
            "proto_report": self.state.proto_report,
            "sec": self.state.sec,
            "suspend": self.state.suspend,
            "notify": dict(self.state.notify) if self.state.notify else None,
            "error": self.state.error,
            "conn_params": vars(self.state.conn_params) if self.state.conn_params else None,
            "phy": vars(self.state.phy) if self.state.phy else None,
            "last_disc_reason": self.state.last_disc_reason,
        }

    # ---------- ops/control ----------

    async def ping(self) -> None:
        if self.is_open:
            await self._write_line("PING")

    async def status_cmd(self) -> None:
        if self.is_open:
            await self._write_line("STATUS")

    async def unpair(self) -> None:
        if self.is_open:
            await self._write_line("UNPAIR")

    # ---------- edge-level API ----------

    def key_down(self, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            payload = self._encode_keyboard_down(code)
        else:
            payload = self._encode_consumer_down(code)
        if payload:
            self._enqueue_nowait(payload)

    def key_up(self, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            payload = self._encode_keyboard_up(code)
        else:
            payload = self._encode_consumer_up(code)
        if payload:
            self._enqueue_nowait(payload)

    async def send_key(self, *, usage: Usage, code: str, hold_ms: int = 40) -> None:
        self.key_down(usage=usage, code=code)
        await asyncio.sleep(max(0, int(hold_ms)) / 1000.0)
        self.key_up(usage=usage, code=code)

    async def run_macro(
        self,
        steps: list[dict],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        gap_s = max(0, int(inter_delay_ms)) / 1000.0
        for i, step in enumerate(steps):
            usage = (step or {}).get("usage")
            code = (step or {}).get("code")
            hold_ms = int((step or {}).get("hold_ms", default_hold_ms))
            if isinstance(usage, str) and isinstance(code, str):
                await self.send_key(usage=usage, code=code, hold_ms=hold_ms)
                if i != len(steps) - 1:
                    await asyncio.sleep(gap_s)

    # ---------- compilation (hot path) ----------

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
                    d = self._encode_keyboard_down(code)
                    u = self._encode_keyboard_up(code)
                    if d:
                        kb_down[code] = d
                    if u:
                        kb_up[code] = u
                elif usage == "consumer":
                    d = self._encode_consumer_down(code)
                    u = self._encode_consumer_up(code)
                    if d:
                        cc_down[code] = d
                    if u:
                        cc_up[code] = u

        return CompiledBleFrames(kb_down=kb_down, kb_up=kb_up, cc_down=cc_down, cc_up=cc_up)

    def compiled_key_down(self, frames: CompiledBleFrames, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            payload = frames.kb_down.get(code)
        else:
            payload = frames.cc_down.get(code)
        if payload:
            self._enqueue_nowait(payload)

    def compiled_key_up(self, frames: CompiledBleFrames, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            payload = frames.kb_up.get(code)
        else:
            payload = frames.cc_up.get(code)
        if payload:
            self._enqueue_nowait(payload)

    # ---------- encoding (binary) ----------

    def _encode_keyboard_down(self, code: str) -> Optional[bytes]:
        report8 = self._kb_report8(code)
        if report8 is None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("unknown keyboard code %s (no hid_keymap entry)", code)
            return None
        return b"\x01" + report8

    def _encode_keyboard_up(self, code: str) -> Optional[bytes]:
        _ = code
        return b"\x01" + (b"\x00" * 8)

    def _encode_consumer_down(self, code: str) -> Optional[bytes]:
        report2 = self._cc_report2(code)
        if report2 is None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("unknown consumer code %s (no hid_keymap entry)", code)
            return None
        return b"\x02" + report2

    def _encode_consumer_up(self, code: str) -> Optional[bytes]:
        _ = code
        return b"\x02\x00\x00"

    def _kb_report8(self, code: str) -> Optional[bytes]:
        usage = self._hid_kb.get(code)
        if usage is None:
            return None
        if not (0 <= int(usage) <= 255):
            return None
        return bytes([0, 0, int(usage), 0, 0, 0, 0, 0])

    def _cc_report2(self, code: str) -> Optional[bytes]:
        usage = self._hid_cc.get(code)
        if usage is None:
            return None
        u = int(usage) & 0xFFFF
        return bytes((u & 0xFF, (u >> 8) & 0xFF))

    def _load_hid_keymap(self) -> None:
        identifier = "pihub.assets:hid_keymap.json"
        try:
            resource = importlib_resources.files("pihub.assets") / "hid_keymap.json"
            raw = resource.read_text(encoding="utf-8")
            doc = json.loads(raw)
        except Exception as exc:
            logger.warning("failed to load %s (%r); BLE hot path will be empty", identifier, exc)
            self._hid_kb = {}
            self._hid_cc = {}
            return

        kb = doc.get("keyboard") if isinstance(doc, dict) else None
        cc = doc.get("consumer") if isinstance(doc, dict) else None
        self._hid_kb = {str(k): int(v) for k, v in (kb or {}).items()} if isinstance(kb, dict) else {}
        self._hid_cc = {str(k): int(v) for k, v in (cc or {}).items()} if isinstance(cc, dict) else {}

    # ---------- TX/RX plumbing ----------

    def _enqueue_nowait(self, payload: bytes) -> None:
        # Hot path gating: only send once dongle says READY + connected and serial is open.
        if not (self.is_open and self.state.connected and self.state.ready):
            return

        if (self._tx_trace or logger.isEnabledFor(logging.DEBUG)) and payload:
            if payload[0] == 0x01 and len(payload) == 9:
                logger.debug("tx kb: %s", payload.hex())
            elif payload[0] == 0x02 and len(payload) == 3:
                logger.debug("tx cc: %s", payload.hex())
            else:
                logger.debug("tx raw(%d): %s", len(payload), payload.hex())

        try:
            self._tx_q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug("tx queue full; dropping payload (%d bytes)", len(payload))

    async def _write_line(self, line: str) -> None:
        if not self.is_open:
            return
        framed = (line.strip() + "\n").encode("ascii", errors="replace")
        await self._tx_q.put(framed)

    async def _reconnect_loop(self) -> None:
        while True:
            try:
                if not self.is_open:
                    ok = await self._try_open_and_handshake()
                    if not ok:
                        await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))
                        self._reconnect_delay_s = min(self._reconnect_delay_max_s, self._reconnect_delay_s * 1.5)
                        continue
                    self._reconnect_delay_s = 0.5

                    # start periodic ping/status if configured
                    if self._poll_task is None or self._poll_task.done():
                        self._poll_task = asyncio.create_task(self._poll_loop(), name="ble-serial-poll")

                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("reconnect loop error: %r", exc)
                await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))

    async def _poll_loop(self) -> None:
        last_ping = 0.0
        last_status = 0.0
        while True:
            try:
                now = time.monotonic()
                if self._ping_interval_s > 0 and now - last_ping >= self._ping_interval_s:
                    last_ping = now
                    await self.ping()
                if self._status_poll_s > 0 and now - last_status >= self._status_poll_s:
                    last_status = now
                    await self.status_cmd()
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.5)


def _request_status_soon(self, *, delay_s: Optional[float] = None) -> None:
    """Debounced STATUS request to resync state after transient EVT ordering."""
    if not self.is_open:
        return
    if self._resync_task is not None and not self._resync_task.done():
        return

    d = self._resync_delay_s if delay_s is None else float(delay_s)

    async def _runner() -> None:
        try:
            await asyncio.sleep(max(0.0, d))
            await self.status_cmd()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._resync_task = None

    self._resync_task = asyncio.create_task(_runner(), name="ble-serial-resync")

    async def _try_open_and_handshake(self) -> bool:
        port = self._find_port()
        if not port:
            return False

        try:
            ser = serial.Serial(
                port=port,
                baudrate=self._baud,
                timeout=0,          # non-blocking reads
                write_timeout=0.5,
            )
        except SerialException as exc:
            logger.debug("open failed on %s: %r", port, exc)
            return False

        self._ser = ser
        self._port = port
        self.state = DongleState()
        self._pong_counter = 0
        self._ready_evt.clear()

        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._reader_loop(), name="ble-serial-reader")
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop(), name="ble-serial-writer")

        await asyncio.sleep(0.15)

        ok = await self._handshake_once(timeout_s=1.2)
        if ok:
            logger.info("port ready on %s", port)
            await self.status_cmd()
        else:
            await self._force_reconnect("handshake_timeout")
        return ok

    def _find_port(self) -> Optional[str]:
        cfg = self._serial_port_cfg
        if cfg and cfg != "auto":
            return cfg if os.path.exists(cfg) else None

        # Prefer stable by-id if present
        byid = "/dev/serial/by-id"
        if os.path.isdir(byid):
            try:
                for name in sorted(os.listdir(byid)):
                    p = os.path.join(byid, name)
                    if os.path.islink(p) or os.path.exists(p):
                        # Heuristic: ZEPHYR_USB-DEV / PiHub / nrf
                        if "ZEPHYR" in name or "USB-DEV" in name or "PiHub" in name:
                            return p
            except Exception:
                pass

        # Fallback: ttyACM0..7
        for i in range(0, 8):
            p = f"/dev/ttyACM{i}"
            if os.path.exists(p):
                return p

        envp = os.getenv("BLE_SERIAL_DEVICE") or os.getenv("PIHUB_SERIAL_PORT")
        if envp and os.path.exists(envp):
            return envp
        return None

    async def _handshake_once(self, *, timeout_s: float = 1.2) -> bool:
        await self._write_line("PING")
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return self._pong_counter > 0

    async def _force_reconnect(self, reason: str) -> None:
        logger.debug("forcing serial reconnect (%s)", reason)
        await self._close_serial()
        self.state = DongleState()
        self._ready_evt.clear()

    async def _close_serial(self) -> None:
        if self._resync_task and not self._resync_task.done():
            self._resync_task.cancel()
        self._resync_task = None
        ser, self._ser = self._ser, None
        self._port = None
        if ser is None:
            return
        with contextlib.suppress(Exception):
            ser.close()

    async def _writer_loop(self) -> None:
        while True:
            try:
                payload = await self._tx_q.get()
                if self._ser is None:
                    continue
                loop = asyncio.get_running_loop()
                async with self._tx_lock:
                    n = await loop.run_in_executor(None, self._ser.write, payload)  # type: ignore[arg-type]
                    await loop.run_in_executor(None, self._ser.flush)  # type: ignore[arg-type]
                if self._tx_trace or logger.isEnabledFor(logging.DEBUG):
                    logger.debug("serial wrote %s bytes", n)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("writer error, reconnecting: %r", exc)
                await self._force_reconnect("writer_error")
                await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))

    async def _reader_loop(self) -> None:
        while True:
            try:
                if self._ser is None:
                    await asyncio.sleep(0.1)
                    continue
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, self._ser.read, 256)  # type: ignore[arg-type]
                if not data:
                    await asyncio.sleep(0.01)
                    continue
                self._ingest_rx_bytes(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("reader error, reconnecting: %r", exc)
                await self._force_reconnect("reader_error")
                await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))

    def _ingest_rx_bytes(self, chunk: bytes) -> None:
        for b in chunk:
            if b == 0:
                continue
            self._rx_buf.append(b)
            if b == 0x0A:  # '\n'
                try:
                    line = self._rx_buf.decode("ascii", errors="replace").strip()
                finally:
                    self._rx_buf.clear()
                if line:
                    self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("rx: %s", line)

        if line == "PONG":
            self._pong_counter += 1
            if not self._ready_evt.is_set():
                self._ready_evt.set()
            return

        if line.startswith("ERR"):
            self.state.error = True
            return

        if line.startswith("EVT "):
            self._handle_evt(line)
            return

        if line.startswith("STATUS "):
            self._handle_status(line)
            return

    def _handle_evt(self, line: str) -> None:
        # Examples:
        #  EVT ADV 1
        #  EVT CONN 0
        #  EVT READY 1
        #  EVT DISC reason=19
        #  EVT CONN_PARAMS interval_ms=15.00 latency=0 timeout_ms=3000
        parts = line.split()
        if len(parts) < 3:
            return

        src = parts[1]
        before = (self.state.advertising, self.state.connected, self.state.ready, self.state.sec, self.state.error)

        if src == "ADV" and len(parts) >= 3:
            self.state.advertising = parts[2] == "1"
            self._request_status_soon()
        elif src == "CONN" and len(parts) >= 3:
            is_conn = parts[2] == "1"
            self.state.connected = is_conn
            if not is_conn:
                self.state.ready = False
                self.state.conn_params = None
                self.state.phy = None
                self.state.notify = None
            self._request_status_soon()
        elif src == "READY" and len(parts) >= 3:
            self.state.ready = parts[2] == "1"
            if self.state.ready:
                self._ready_evt.set()
            self._request_status_soon()
        elif src == "DISC":
            # EVT DISC reason=19
            for tok in parts[2:]:
                if tok.startswith("reason="):
                    self.state.last_disc_reason = tok.split("=", 1)[1]
        elif src == "CONN_PARAMS":
            kv = {}
            for tok in parts[2:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k] = v
            try:
                interval_ms = float(kv.get("interval_ms", "0") or 0)
                latency = int(kv.get("latency", "0") or 0)
                timeout_ms = int(kv.get("timeout_ms", "0") or 0)
                self.state.conn_params = ConnParams(interval_ms=interval_ms, latency=latency, timeout_ms=timeout_ms)
            except ValueError:
                pass
        elif src == "PHY":
            # EVT PHY 2 2 (or tx/rx)
            if len(parts) >= 4:
                with contextlib.suppress(ValueError):
                    self.state.phy = Phy(tx=int(parts[2]), rx=int(parts[3]))

        after = (self.state.advertising, self.state.connected, self.state.ready, self.state.sec, self.state.error)
        if after != before:
            logger.info(
                "state: adv=%d conn=%d ready=%d sec=%d err=%d (EVT %s)",
                1 if self.state.advertising else 0,
                1 if self.state.connected else 0,
                1 if self.state.ready else 0,
                self.state.sec,
                1 if self.state.error else 0,
                src,
            )

    def _handle_status(self, line: str) -> None:
        # STATUS adv=0 conn=1 sec=2 ready=1 proto=1 err=0 kb_notify=1 boot_notify=0 cc_notify=1 ...
        kv: Dict[str, str] = {}
        for tok in line.split()[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k.strip()] = v.strip()

        def _i(name: str, default: int = 0) -> int:
            try:
                return int(kv.get(name, str(default)))
            except ValueError:
                return default

        def _f(name: str, default: float = 0.0) -> float:
            try:
                return float(kv.get(name, str(default)))
            except ValueError:
                return default

        before = (
            self.state.advertising,
            self.state.connected,
            self.state.ready,
            self.state.sec,
            self.state.error,
            self.state.proto_report,
            self.state.conn_params.interval_ms if self.state.conn_params else 0.0,
            self.state.conn_params.latency if self.state.conn_params else 0,
            self.state.conn_params.timeout_ms if self.state.conn_params else 0,
        )

        self.state.advertising = _i("adv", 0) == 1
        self.state.connected = _i("conn", 0) == 1
        self.state.ready = _i("ready", 0) == 1
        if not self.state.connected:
            self.state.ready = False
            self.state.conn_params = None
            self.state.phy = None
            self.state.notify = None
        self.state.sec = _i("sec", 0)
        self.state.error = _i("err", 0) == 1
        proto = _i("proto", 1)
        self.state.proto_report = (proto == 1)
        self.state.suspend = _i("suspend", 0) == 1

        notify = {
            "kb": _i("kb_notify", 0),
            "boot": _i("boot_notify", 0),
            "cc": _i("cc_notify", 0),
            "batt": _i("batt_notify", 0),
        }
        self.state.notify = notify

        interval_ms = _f("interval_ms", 0.0)
        latency = _i("latency", 0)
        timeout_ms = _i("timeout_ms", 0)
        if interval_ms or latency or timeout_ms:
            self.state.conn_params = ConnParams(interval_ms=interval_ms, latency=latency, timeout_ms=timeout_ms)

        phy_tx = _i("phy_tx", 0)
        phy_rx = _i("phy_rx", 0)
        if phy_tx or phy_rx:
            self.state.phy = Phy(tx=phy_tx, rx=phy_rx)

        if self.state.ready:
            self._ready_evt.set()

        after = (
            self.state.advertising,
            self.state.connected,
            self.state.ready,
            self.state.sec,
            self.state.error,
            self.state.proto_report,
            self.state.conn_params.interval_ms if self.state.conn_params else 0.0,
            self.state.conn_params.latency if self.state.conn_params else 0,
            self.state.conn_params.timeout_ms if self.state.conn_params else 0,
        )

        if after != before:
            logger.info(
                "state: adv=%d conn=%d ready=%d sec=%d proto=%s interval_ms=%s latency=%s timeout_ms=%s err=%d (STATUS)",
                1 if self.state.advertising else 0,
                1 if self.state.connected else 0,
                1 if self.state.ready else 0,
                self.state.sec,
                "report" if self.state.proto_report else "boot",
                f"{self.state.conn_params.interval_ms:.2f}" if (self.state.connected and self.state.conn_params) else "-",
                self.state.conn_params.latency if (self.state.connected and self.state.conn_params) else "-",
                self.state.conn_params.timeout_ms if (self.state.connected and self.state.conn_params) else "-",
                1 if self.state.error else 0,
            )

    def _sleep_with_jitter(self, base_s: float) -> float:
        return max(0.05, base_s + random.uniform(0.0, min(0.25, base_s)))

