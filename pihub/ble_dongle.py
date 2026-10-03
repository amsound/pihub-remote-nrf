"""
Bluetooth-over-USB dongle link (Serial / CDC ACM).

Serial transport to the nRF52840 dongle. Payloads are BLE HID reports:
- Hot path binary frames (device parser expects):
    - b"\x01" + 8 bytes keyboard report
    - b"\x02" + 2 bytes consumer usage (little-endian)
- Control lines we send are ASCII: PING (handshake) / STATUS / INFO (newline terminated)
- Replies are ASCII lines: PONG / STATUS ... / INFO ... / OK / ERR ...
- The dongle announces every change itself as ASCII lines: EVT ...
  We ask for its state only once, when the serial port opens (it may have been
  running long before we arrived).
- Hot-path binary frames are fire-and-forget on success

This module is SERIAL ONLY (no USB HID host libraries).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

import serial  # pyserial
from serial import SerialException

from importlib import resources as importlib_resources

Usage = Literal["keyboard", "consumer"]

from .initial_status import InitialStatus

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
    interval_ms: int = 0
    latency: int = 0
    timeout_ms: int = 0


@dataclass
class DongleState:
    ready: bool = False
    advertising: bool = False
    connected: bool = False
    sec: int = 0   # link security level (2 = encrypted); for the debug log
    error: bool = False
    conn_params: Optional[ConnParams] = None
    last_disc_reason: Optional[str] = None


# ------------------------- Main link -------------------------

class BleDongleLink:
    """
    Serial dongle link.

    Public API:
      - start/stop
      - key_down/key_up (edge-level, fire and forget)
      - run_macro and the Apple TV macros (power_on/power_off/return_home)
      - compile_ble_frames + compiled_key_down/up
      - status property
    """

    def __init__(
        self,
        *,
        serial_port: str = "auto",
        baud: int = 115200,
        tx_queue: int = 512,
    ) -> None:
        self._serial_port_cfg = (serial_port or "auto").strip()
        self._baud = int(baud)

        self._ser: Optional[serial.Serial] = None
        self._port: Optional[str] = None

        self.state = DongleState()
        self._transport_evt = asyncio.Event()

        self._last_error: str | None = None

        self._writer_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None

        self._firmware: Optional[str] = None   # build stamp from the dongle's INFO reply

        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=int(tx_queue))
        # Set while the serial port is closed; the reconnect loop sleeps on it.
        self._link_down = asyncio.Event()
        self._link_down.set()

        self._rx_buf = bytearray()
        self._rx_line_max = 512

        # reconnect knobs
        self._reconnect_delay_s = 0.5
        self._reconnect_delay_max_s = 8.0

        # What the log last said about the link, so only changes are reported.
        self._logged_label: Optional[str] = None
        self._logged_params: Optional[tuple] = None

        self._missing_dongle_logged = False
        # First reading: the dongle's first STATUS reply (how its link to the Apple TV stands).
        self.initial = InitialStatus(logger)

        # HID usage maps loaded from assets/hid_keymap.json
        self._hid_kb: Dict[str, int] = {}
        self._hid_cc: Dict[str, int] = {}
        self._load_hid_keymap()

        logger.info("started serial_port_cfg=%s baud=%s", self._serial_port_cfg, self._baud)

    # ---------- lifecycle ----------

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    async def start(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name="ble-serial-reconnect")

    async def stop(self) -> None:
        for t in (self._reconnect_task, self._writer_task):
            if t and not t.done():
                t.cancel()
        for t in (self._reconnect_task, self._writer_task):
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await self._close_serial()

    # ---------- status ----------

    @property
    def status(self) -> dict:
        return {
            "adapter_present": self.is_open,
            "firmware": self._firmware,
            "transport_open": self.is_open,
            "ready": self.state.ready,
            "advertising": self.state.advertising,
            "connected": self.state.connected,
            "error": self.state.error,
            "last_error": self._last_error,
            "conn_params": vars(self.state.conn_params) if self.state.conn_params else None,
            "last_disc_reason": self.state.last_disc_reason,
        }

    # ---------- ops/control ----------

    async def status_cmd(self) -> None:
        if self.is_open:
            await self._write_line("STATUS")

    def release_all(self) -> None:
        """
        Stronger host-side failsafe to avoid stuck keys.

        This intentionally discards queued HID traffic so the zero keyboard + zero
        consumer reports win over stale presses/releases.
        """
        self._enqueue_priority_frames([
            b"\x01" + (b"\x00" * 8),
            b"\x02\x00\x00",
        ])


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

    def key_down_strict(self, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            payload = self._encode_keyboard_down(code)
        else:
            payload = self._encode_consumer_down(code)
        if not payload:
            raise RuntimeError(f"key_down_not_sent: unknown_{usage}_code:{code}")
        self._enqueue_strict(payload, action="key_down")

    def key_up_strict(self, *, usage: Usage, code: str) -> None:
        if usage == "keyboard":
            payload = self._encode_keyboard_up(code)
        else:
            payload = self._encode_consumer_up(code)
        if not payload:
            raise RuntimeError(f"key_up_not_sent: unknown_{usage}_code:{code}")
        self._enqueue_strict(payload, action="key_up")

    async def send_key_strict(self, *, usage: Usage, code: str, key_hold_ms: int = 40) -> None:
        self.key_down_strict(usage=usage, code=code)
        await asyncio.sleep(max(0, int(key_hold_ms)) / 1000.0)
        self.key_up_strict(usage=usage, code=code)

    async def run_macro(
        self,
        steps: list[dict],
        *,
        default_key_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        """Each step is a key press or a {"wait_ms": n} pause; presses are spaced by inter_delay_ms."""
        gap_s = inter_delay_ms / 1000.0
        for i, step in enumerate(steps):
            if "wait_ms" in step:
                await asyncio.sleep(step["wait_ms"] / 1000.0)
                continue

            await self.send_key_strict(
                usage=step["usage"],
                code=step["code"],
                key_hold_ms=step.get("key_hold_ms", default_key_hold_ms),
            )
            if i != len(steps) - 1:
                await asyncio.sleep(gap_s)

    async def power_on(self) -> None:
        steps = [
            {"usage": "consumer", "code": "menu", "key_hold_ms": 40},
            {"wait_ms": 1000},
            {"usage": "consumer", "code": "menu", "key_hold_ms": 40},
        ]
        await self.run_macro(steps)

    async def power_off(self) -> None:
        steps = [
            {"usage": "consumer", "code": "ac_home", "key_hold_ms": 40},
            {"usage": "consumer", "code": "ac_home", "key_hold_ms": 40},
            {"usage": "consumer", "code": "ac_home", "key_hold_ms": 40},
            {"usage": "consumer", "code": "menu", "key_hold_ms": 40},
            {"usage": "consumer", "code": "menu", "key_hold_ms": 40},
            {"usage": "consumer", "code": "power", "key_hold_ms": 2000},
        ]
        await self.run_macro(steps)

    async def return_home(self) -> None:
        steps = [
            {"usage": "consumer", "code": "ac_home", "key_hold_ms": 40},
            {"usage": "consumer", "code": "ac_home", "key_hold_ms": 40},
            {"usage": "consumer", "code": "ac_home", "key_hold_ms": 40},
            {"usage": "consumer", "code": "menu", "key_hold_ms": 40},
            {"usage": "consumer", "code": "menu", "key_hold_ms": 40},
        ]
        await self.run_macro(steps)

    # ---------- compilation (hot path) ----------

    def compile_ble_frames(self, bindings: Dict[str, List[Dict[str, Any]]]) -> CompiledBleFrames:
        kb_down: Dict[str, bytes] = {}
        kb_up: Dict[str, bytes] = {}
        cc_down: Dict[str, bytes] = {}
        cc_up: Dict[str, bytes] = {}

        for _rem_key, actions in bindings.items():
            for a in actions:
                if a.get("domain") != "ble":
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
        self._hid_kb = {str(k): int(v) for k, v in kb.items()} if isinstance(kb, dict) else {}
        self._hid_cc = {str(k): int(v) for k, v in cc.items()} if isinstance(cc, dict) else {}

    def _state_label(self) -> str:
        # canonical internal labels, lowercase for readability
        if self.state.connected:
            return "ready" if self.state.ready else "connected_not_ready"
        return "advertising" if self.state.advertising else "disconnected"

    # Why the Bluetooth link ended, for the reasons seen in practice (HCI codes).
    _DISCONNECT_REASONS = {
        "8": "link lost",
        "19": "closed by the apple tv",
        "21": "apple tv powering off",
        "22": "closed by the dongle",
    }

    def _link_params(self) -> Optional[tuple]:
        """(interval_ms, latency, timeout_ms) of the live link, when the dongle has reported them."""
        params = self.state.conn_params
        if not (self.state.connected and params):
            return None
        return int(params.interval_ms), int(params.latency), int(params.timeout_ms)

    @staticmethod
    def _fmt_params(params: tuple) -> str:
        return "interval_ms=%d latency=%d timeout_ms=%d" % params

    def _link_summary(self) -> str:
        """The link's state in plain words (the first reading at start-up)."""
        label = self._state_label()
        if label == "ready":
            params = self._link_params()
            return "apple tv link ready" + (f" ({self._fmt_params(params)})" if params else "")
        return {
            "connected_not_ready": "apple tv connected, link not ready yet",
            "advertising": "advertising, waiting for the apple tv",
        }.get(label, "apple tv not connected")

    def _log_state(self, *, source: str) -> None:
        """Log a change in the link to the Apple TV, once, in plain words.

        Only transitions are reported. The first reading after start-up is not a
        change: it is reported by the initial status line instead.
        """
        label = self._state_label()
        params = self._link_params()
        prev_label, prev_params = self._logged_label, self._logged_params
        self._logged_label, self._logged_params = label, params

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "state (%s): label=%s adv=%d conn=%d ready=%d sec=%d err=%d params=%s",
                source,
                label,
                self.state.advertising,
                self.state.connected,
                self.state.ready,
                self.state.sec,
                self.state.error,
                params,
            )

        if not self.initial.received:
            return

        connected_labels = ("ready", "connected_not_ready")
        was_connected = prev_label in connected_labels

        if was_connected and label not in connected_labels:
            reason = self.state.last_disc_reason
            why = self._DISCONNECT_REASONS.get(str(reason))
            if why:
                logger.info("apple tv disconnected (%s, reason %s)", why, reason)
            elif reason:
                logger.info("apple tv disconnected (reason %s)", reason)
            else:
                logger.info("apple tv disconnected")

        if label == "advertising":
            if prev_label != "advertising":
                logger.info("advertising, waiting for the apple tv")
        elif label == "connected_not_ready":
            if prev_label == "ready":
                logger.info("apple tv link no longer ready")
            elif not was_connected:
                logger.info("apple tv connected")
        elif label == "ready":
            if prev_label != "ready":
                logger.info("apple tv link ready%s", f" ({self._fmt_params(params)})" if params else "")
            elif params and params != prev_params:
                logger.info("link parameters changed (%s)", self._fmt_params(params))
        # "disconnected" with nothing before it worth reporting (advertising has just
        # stopped because a connection is arriving) says nothing.

    # ---------- TX/RX plumbing ----------

    def _is_hid_frame(self, payload: bytes) -> bool:
        return (
            (len(payload) == 9 and payload[:1] == b"\x01") or
            (len(payload) == 3 and payload[:1] == b"\x02")
        )

    def _drain_hid_frames(self) -> int:
        """
        Remove queued hot-path HID frames, preserving ASCII control lines
        (STATUS/INFO).
        """
        kept: list[bytes] = []
        dropped = 0

        while True:
            try:
                item = self._tx_q.get_nowait()
            except asyncio.QueueEmpty:
                break

            if self._is_hid_frame(item):
                dropped += 1
                continue
            kept.append(item)

        for item in kept:
            with contextlib.suppress(asyncio.QueueFull):
                self._tx_q.put_nowait(item)

        return dropped

    def _enqueue_priority_frames(self, frames: list[bytes]) -> None:
        """
        Best-effort priority injection for safety frames. Drop queued HID traffic
        first, then enqueue the provided frames.
        """
        if not self.is_open:
            return

        dropped = self._drain_hid_frames()

        for payload in frames:
            try:
                self._tx_q.put_nowait(payload)
            except asyncio.QueueFull:
                # If we're still full after draining HID traffic, drop one oldest item
                # and retry once. This should be rare and mostly affects queued ASCII ops.
                with contextlib.suppress(asyncio.QueueEmpty):
                    _ = self._tx_q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    self._tx_q.put_nowait(payload)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("priority release-all queued (dropped_hid=%d)", dropped)


    def _enqueue_nowait(self, payload: bytes) -> None:
        # Hot path gating: only send once dongle says READY + connected and serial is open.
        if not (self.is_open and self.state.connected and self.state.ready):
            return

        if logger.isEnabledFor(logging.DEBUG) and payload:
            if payload[0] == 0x01 and len(payload) == 9:
                logger.debug("tx kb: %s", payload.hex())
            elif payload[0] == 0x02 and len(payload) == 3:
                logger.debug("tx cc: %s", payload.hex())
            else:
                logger.debug("tx raw(%d): %s", len(payload), payload.hex())

        try:
            self._tx_q.put_nowait(payload)
        except asyncio.QueueFull:
            # Prefer dropping the oldest payload to preserve the most recent transitions
            # (e.g., don’t drop a key-up).
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._tx_q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._tx_q.put_nowait(payload)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("tx queue full; dropped oldest and kept newest (%d bytes)", len(payload))

    def _enqueue_strict(self, payload: bytes, *, action: str) -> None:
        if not self.is_open:
            raise RuntimeError(f"{action}_not_sent: transport_not_open")
        if not self.state.connected:
            raise RuntimeError(f"{action}_not_sent: not_connected")
        if not self.state.ready:
            raise RuntimeError(f"{action}_not_sent: not_ready")

        try:
            self._tx_q.put_nowait(payload)
        except asyncio.QueueFull:
            raise RuntimeError(f"{action}_not_sent: tx_queue_full")

    async def _write_line(self, line: str) -> None:
        if not self.is_open:
            return
        framed = (line.rstrip("\r\n") + "\n").encode("ascii", errors="replace")
        await self._tx_q.put(framed)

    def _note_missing_dongle_once(self) -> None:
        if self._missing_dongle_logged:
            return
        self._missing_dongle_logged = True
        logger.info("BLE dongle not found; continuing without BLE connection")

    async def _reconnect_loop(self) -> None:
        while True:
            try:
                if not self.is_open:
                    ok = await self._try_open_and_handshake()
                    if not ok:
                        if self._find_port() is None:
                            self._note_missing_dongle_once()
                            self.initial.mark_failed("dongle not found")
                        else:
                            self.initial.mark_failed("dongle did not answer")
                        await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))
                        self._reconnect_delay_s = min(self._reconnect_delay_max_s, self._reconnect_delay_s * 1.5)
                        continue
                    self._reconnect_delay_s = 0.5

                # Nothing to do while the port is open: sleep until it closes.
                await self._link_down.wait()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("reconnect loop error: %r", exc)
                await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))

    async def _try_open_and_handshake(self) -> bool:
        port = self._find_port()
        if not port:
            return False


        try:
            ser = serial.Serial(
                port=port,
                baudrate=self._baud,
                timeout=0,          # non-blocking reads
                write_timeout=0,    # writes go straight to the (non-blocking) port, see _writer_loop
                exclusive=True,
            )
        except SerialException as exc:
            logger.debug("open failed on %s: %r", port, exc)
            return False

        self._ser = ser
        self._port = port
        self._link_down.clear()
        self._missing_dongle_logged = False
        self.state = DongleState()
        self._logged_label = self._logged_params = None
        self._transport_evt.clear()
        self._last_error = None
        # Avoid stale/fragmented telemetry across reconnects.
        self._rx_buf.clear()
        with contextlib.suppress(Exception):
            ser.reset_input_buffer()
        with contextlib.suppress(Exception):
            ser.reset_output_buffer()


        # Reads are event-driven: the loop wakes us only when the dongle has sent data.
        asyncio.get_running_loop().add_reader(ser.fileno(), self._on_serial_readable)
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop(), name="ble-serial-writer")

        await asyncio.sleep(0.15)

        ok = await self._handshake_once(timeout_s=1.2)
        if ok:
            logger.info("dongle connected serial_port=%s", port)
            await self.status_cmd()
            await self._write_line("INFO")   # which firmware build the dongle runs
        else:
            await self._force_reconnect("handshake_timeout")
        return ok

    def _find_port(self) -> Optional[str]:
        cfg = (self._serial_port_cfg or "auto").strip()

        # Explicit path or glob-like pattern wins.
        if cfg and cfg.lower() != "auto":
            if any(ch in cfg for ch in "*?[]"):
                import glob
                matches = sorted(glob.glob(cfg))
                for p in matches:
                    if os.path.exists(p):
                        return p
                return None
            return cfg if os.path.exists(cfg) else None

        # Prefer stable by-id names first.
        byid = "/dev/serial/by-id"
        if os.path.isdir(byid):
            try:
                preferred: list[str] = []
                fallback: list[str] = []

                for name in sorted(os.listdir(byid)):
                    p = os.path.join(byid, name)
                    if not (os.path.islink(p) or os.path.exists(p)):
                        continue

                    upper = name.upper()
                    if "ZEPHYR" in upper and "USB-DEV" in upper:
                        preferred.append(p)
                    else:
                        fallback.append(p)

                if preferred:
                    return preferred[0]
                if fallback:
                    return fallback[0]
            except OSError:
                logger.debug("listing %s failed", byid, exc_info=True)

        # Fallback: ttyACM0..7
        for i in range(0, 8):
            p = f"/dev/ttyACM{i}"
            if os.path.exists(p):
                return p

        return None

    async def _handshake_once(self, *, timeout_s: float = 1.2) -> bool:
        await self._write_line("PING")
        try:
            await asyncio.wait_for(self._transport_evt.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return self._transport_evt.is_set()

    async def _force_reconnect(self, reason: str) -> None:
        logger.debug("forcing serial reconnect (%s)", reason)
        self._last_error = reason
        await self._close_serial()
        self.state = DongleState()
        self._transport_evt.clear()

    async def _close_serial(self) -> None:
        self._stop_reading()
        self._clear_tx_queue()
        ser, self._ser = self._ser, None
        self._port = None
        self._link_down.set()
        if ser is None:
            return
        with contextlib.suppress(Exception):
            ser.close()

    def _clear_tx_queue(self) -> None:
        while True:
            try:
                self._tx_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _writer_loop(self) -> None:
        while True:
            try:
                payload = await self._tx_q.get()
                ser = self._ser
                if ser is None:
                    continue
                # Straight to the port. It is non-blocking and a frame is a few bytes,
                # so this takes microseconds; a thread-pool hop cost ~0.4 ms per key
                # edge, sometimes 10+ ms. A port that won't take a whole frame at once
                # means the dongle has stopped reading: reconnect (handled below).
                n = os.write(ser.fileno(), payload)
                if n != len(payload):
                    raise OSError(f"short write ({n} of {len(payload)} bytes)")
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("serial wrote %s bytes", n)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"writer_error: {exc}"
                logger.warning("dongle serial write failed (%s); reconnecting", exc)
                await self._force_reconnect("writer_error")
                await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))

    def _on_serial_readable(self) -> None:
        """Called by the event loop when the serial port has data (no polling)."""
        ser = self._ser
        if ser is None:
            return
        try:
            data = ser.read(4096)  # non-blocking (timeout=0): whatever is waiting
        except Exception as exc:
            # What an unplugged (or resetting) dongle looks like from here.
            self._last_error = f"reader_error: {exc}"
            logger.warning("dongle serial port lost (unplugged?); waiting for it to return")
            logger.debug("serial read failed: %r", exc)
            self._stop_reading()
            asyncio.get_running_loop().create_task(self._force_reconnect("reader_error"))
            return
        if data:
            self._ingest_rx_bytes(data)

    def _stop_reading(self) -> None:
        ser = self._ser
        if ser is None:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(ser.fileno())

    def _ingest_rx_bytes(self, chunk: bytes) -> None:
        for b in chunk:
            if b == 0:
                continue

            self._rx_buf.append(b)

            if len(self._rx_buf) > self._rx_line_max:
                if logger.isEnabledFor(logging.WARNING):
                    logger.warning("rx line exceeded %d bytes; dropping partial line", self._rx_line_max)
                self._rx_buf.clear()
                continue

            if b == 0x0A:  # '\n'
                try:
                    line = self._rx_buf.decode("ascii", errors="replace").rstrip("\r\n")
                finally:
                    self._rx_buf.clear()

                if line:
                    self._handle_line(line)


    def _handle_line(self, line: str) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("rx: %s", line)

        if line == "PONG":
            self._transport_evt.set()
            return

        if line == "OK":
            return

        if line.startswith("BUSY"):
            return

        if line.startswith("ERR"):
            self.state.error = True
            self._last_error = line
            logger.warning("dongle error line: %s", line)
            return

        if line.startswith("EVT "):
            self._handle_evt(line)
            return

        if line.startswith("STATUS "):
            self._handle_status(line)
            return

        if line.startswith("INFO "):
            # INFO name=PiHub nrf Remote fw=20261002-2105-c56824c addr=...
            match = re.search(r"\bfw=(\S+)", line)
            firmware = match.group(1) if match else None
            if firmware and firmware != self._firmware:
                logger.info("dongle firmware %s", firmware)
            self._firmware = firmware or self._firmware
            return

    def _handle_evt(self, line: str) -> None:
        # Examples:
        #  EVT ADV 1
        #  EVT CONN 0
        #  EVT READY 1
        #  EVT ERR 0
        #  EVT DISC reason=19
        #  EVT CONN_PARAMS interval_ms=15 latency=0 timeout_ms=3000
        parts = line.split()
        if len(parts) < 3:
            return

        src = parts[1]
        before = (self.state.advertising, self.state.connected, self.state.ready, self.state.error)

        if src == "ADV" and len(parts) >= 3:
            self.state.advertising = parts[2] == "1"

        elif src == "CONN" and len(parts) >= 3:
            is_conn = parts[2] == "1"
            self.state.connected = is_conn
            if is_conn:
                self.state.last_disc_reason = None
            if not is_conn:
                self.state.ready = False
                self.state.conn_params = None

        elif src == "READY" and len(parts) >= 3:
            self.state.ready = parts[2] == "1"
            if self.state.ready:
                # Ready means connected. Firmware older than October 2026 never
                # announces the connection itself, so this is where we learn of it.
                self.state.connected = True

        elif src == "ERR" and len(parts) >= 3:
            try:
                self.state.error = int(parts[2]) == 1
                if self.state.error:
                    self._last_error = "evt_err"
                else:
                    self._last_error = None
            except ValueError:
                pass

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
                interval_ms = int(kv.get("interval_ms", "0") or 0)
                latency = int(kv.get("latency", "0") or 0)
                timeout_ms = int(kv.get("timeout_ms", "0") or 0)
                self.state.conn_params = ConnParams(
                    interval_ms=interval_ms,
                    latency=latency,
                    timeout_ms=timeout_ms,
                )
                self._log_state(source="EVT CONN_PARAMS")
            except ValueError:
                pass

        if (self.state.advertising, self.state.connected, self.state.ready, self.state.error) != before:
            self._log_state(source=f"EVT {src}")


    def _handle_status(self, line: str) -> None:
        # STATUS adv=0 conn=1 sec=2 ready=1 proto=1 err=0 ... interval_ms=15 latency=0 timeout_ms=4000
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

        self.state.advertising = _i("adv", 0) == 1
        self.state.connected = _i("conn", 0) == 1
        self.state.ready = self.state.connected and _i("ready", 0) == 1
        self.state.sec = _i("sec", 0)
        self.state.error = _i("err", 0) == 1

        interval_ms = _i("interval_ms", 0)
        latency = _i("latency", 0)
        timeout_ms = _i("timeout_ms", 0)
        if self.state.connected and (interval_ms or latency or timeout_ms):
            self.state.conn_params = ConnParams(interval_ms=interval_ms, latency=latency, timeout_ms=timeout_ms)
        else:
            self.state.conn_params = None

        # Logs only if the link's state or parameters differ from what was last reported.
        self._log_state(source="STATUS")

        self.initial.mark_received(self._link_summary())

    def _sleep_with_jitter(self, base_s: float) -> float:
        return max(0.05, base_s + random.uniform(0.0, min(0.25, base_s)))