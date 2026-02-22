"""
Bluetooth-over-USB dongle link (HID transport).

Goal:
- One module owns: discovery, reconnect, TX hot-path, RX parsing, state, key encoding,
  precompiled frames, and ops commands (PING/STATUS/UNPAIR).
- Designed to be wired like input_unifying.py: started by app, called by dispatcher.

Assumptions:
- Firmware exposes a USB HID interface:
  - Host->device: OUT reports carrying raw bytes (binary frames or ASCII commands).
  - Device->host: IN reports carrying ASCII lines (EVT/STATUS/OK/PONG etc), newline-delimited.

This keeps your existing dongle protocol intact:
  - Hot path: b"\\x01"+8 (keyboard report), b"\\x02"+2 (consumer payload)
  - Ops: "PING\\n", "STATUS\\n", "UNPAIR\\n"
  - Telemetry: "EVT ...\\n" and "STATUS ...\\n"
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Literal

try:
    # Prefer stdlib in 3.9+
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover
    import importlib_resources  # type: ignore

import hid  # pip: hid (hidapi wrapper)

Usage = Literal["keyboard", "consumer"]

logger = logging.getLogger(__name__)


# ---- USB defaults you saw in menuconfig ----
DEFAULT_VID = 0x2FE3
DEFAULT_PID = 0x0100

# Default HID report sizes (safe for most HID endpoints)
DEFAULT_OUT_REPORT_BYTES = 64
DEFAULT_IN_REPORT_BYTES = 64


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


@dataclass(frozen=True)
class CompiledBleFrames:
    down: bytes
    up: bytes


class BleDongleLink:
    """
    HID-based link to the PiHub dongle.

    Public surface area (intentionally similar to the old BTLEController + BleSerial combo):
      - start()/stop()
      - key_down/key_up for dispatcher fallback
      - compile_ble_frames + compiled_key_down/compiled_key_up for precompiled path
      - unpair()/request_status()
      - status property for health reporting
    """

    def __init__(
        self,
        *,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        product_contains: Optional[str] = None,
        out_report_bytes: int = DEFAULT_OUT_REPORT_BYTES,
        in_report_bytes: int = DEFAULT_IN_REPORT_BYTES,
        ping_timeout_s: float = 3.0,
        reconnect_delay_s: float = 0.5,
        status_poll_s: float = 30.0,
        on_event: Optional[Callable[[str, DongleState], None]] = None,
    ) -> None:
        self._vid = vid
        self._pid = pid
        self._product_contains = product_contains
        self._out_report_bytes = int(out_report_bytes)
        self._in_report_bytes = int(in_report_bytes)
        self._ping_timeout_s = float(ping_timeout_s)
        self._reconnect_delay_s = float(reconnect_delay_s)
        self._status_poll_s = float(status_poll_s)
        self._on_event = on_event

        self.state = DongleState()
        self._pong_counter = 0

        self._dev: Optional[hid.device] = None
        self._path: Optional[bytes] = None

        # tasks
        self._connect_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._status_poll_task: Optional[asyncio.Task] = None
        self._status_resync_task: Optional[asyncio.Task] = None

        # TX queue: hot path enqueues bytes; writer task writes to HID
        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)

        # RX line reassembly
        self._rx_buf = bytearray()

        # READY event for callers that want it
        self._ready_evt = asyncio.Event()

        # HID tables (loaded from pihub.assets)
        self._kb, self._cc = self._load_hid_tables_from_assets()

        # Cache compiled frames for any (usage, code) we compile
        self._compiled: Dict[Tuple[str, str], CompiledBleFrames] = {}

    # ----------------- lifecycle -----------------

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    @property
    def active_path(self) -> Optional[str]:
        if self._path is None:
            return None
        try:
            return self._path.decode("utf-8", errors="replace")
        except Exception:
            return repr(self._path)

    @property
    def link_ready(self) -> bool:
        # mirrors the old serial_ready behavior: open + dongle says READY=1
        return bool(self.is_open and self.state.ready)

    async def start(self) -> None:
        if self._connect_task is None:
            self._connect_task = asyncio.create_task(self._connect_loop(), name="ble-dongle-connect")

    async def stop(self) -> None:
        if self._connect_task is not None:
            self._connect_task.cancel()
            self._connect_task = None

        for task_attr in (
            "_reader_task",
            "_writer_task",
            "_keepalive_task",
            "_status_poll_task",
            "_status_resync_task",
        ):
            t = getattr(self, task_attr)
            if t is not None:
                t.cancel()
                setattr(self, task_attr, None)

        self._close()
        self.state = DongleState()
        self._emit("link_lost")

    # ----------------- public: health/status -----------------

    @property
    def status(self) -> dict:
        return {
            "adapter_present": self.is_open,
            "active_path": self.active_path,
            "ready": self.state.ready,
            "advertising": self.state.advertising,
            "connected": self.state.connected,
            "proto_boot": self.state.proto_boot,
            "sec": self.state.sec,
            "suspend": self.state.suspend,
            "notify": dict(self.state.notify) if self.state.notify else None,
            "error": self.state.error,
            "conn_params": dict(self.state.conn_params) if self.state.conn_params else None,
            "phy": dict(self.state.phy) if self.state.phy else None,
            "last_disc_reason": self.state.last_disc_reason,
        }

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        if self.state.ready:
            return True
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ----------------- public: ops commands -----------------

    def request_status(self) -> None:
        # Keep human-readable and identical to your existing dongle protocol.
        self.enqueue_line("STATUS")

    async def unpair(self) -> None:
        # Keep human-readable and identical to your existing dongle protocol.
        self.enqueue_line("UNPAIR")

    # ----------------- dispatcher-facing API -----------------

    def key_down(self, *, usage: str, code: str) -> None:
        if usage == "keyboard":
            down = self._encode_keyboard_down(code)
            if down is None:
                return
            self.notify_keyboard(down)
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if usage_id:
                self.notify_consumer(usage_id, True)

    def key_up(self, *, usage: str, code: str) -> None:
        if usage == "keyboard":
            if code not in self._kb:
                logger.warning("unknown keyboard code %s up; ignoring", code)
                return
            self.notify_keyboard(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if usage_id:
                self.notify_consumer(usage_id, False)

    async def send_key(self, *, usage: str, code: str, hold_ms: int = 40) -> None:
        """HA command: single key tap."""
        # fire down immediately (hot)
        self.key_down(usage=usage, code=code)
        # hold
        await asyncio.sleep(max(0, int(hold_ms)) / 1000.0)
        # release
        self.key_up(usage=usage, code=code)

    async def run_macro(
        self,
        steps: list[dict],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        """HA command: timed local macro sequence."""
        tap_s = max(0, int(default_hold_ms)) / 1000.0
        gap_s = max(0, int(inter_delay_ms)) / 1000.0

        for i, step in enumerate(steps):
            usage = (step or {}).get("usage")
            code = (step or {}).get("code")
            hold_ms = (step or {}).get("hold_ms", default_hold_ms)

            if not (isinstance(usage, str) and isinstance(code, str)):
                continue

            await self.send_key(usage=usage, code=code, hold_ms=int(hold_ms))

            # gap between steps (not after last)
            if i != (len(steps) - 1):
                await asyncio.sleep(gap_s)

    def compile_ble_frames(self, *, usage: str, code: str) -> CompiledBleFrames | None:
        k = (usage, code)
        if k in self._compiled:
            return self._compiled[k]

        frames: Optional[CompiledBleFrames] = None

        if usage == "keyboard":
            down8 = self._encode_keyboard_down(code)
            if down8 is None:
                return None
            if code not in self._kb:
                return None
            frames = CompiledBleFrames(down=b"\x01" + down8, up=b"\x01" + (b"\x00" * 8))

        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if not usage_id:
                return None
            usage_id &= 0xFFFF
            down2 = bytes((usage_id & 0xFF, (usage_id >> 8) & 0xFF))
            frames = CompiledBleFrames(down=b"\x02" + down2, up=b"\x02\x00\x00")

        if frames is not None:
            self._compiled[k] = frames
        return frames

    def compiled_key_down(self, frames: CompiledBleFrames) -> None:
        self.enqueue_bin_frame(frames.down)

    def compiled_key_up(self, frames: CompiledBleFrames) -> None:
        self.enqueue_bin_frame(frames.up)

    # ----------------- HIDClient-style transport hooks -----------------

    def notify_keyboard(self, report8: bytes) -> None:
        if not self.link_ready:
            return
        if len(report8) != 8:
            return
        self.enqueue_bin_frame(b"\x01" + report8)

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        if not self.link_ready:
            return
        usage = (usage_id & 0xFFFF) if pressed else 0
        le = bytes((usage & 0xFF, (usage >> 8) & 0xFF))
        self.enqueue_bin_frame(b"\x02" + le)

    # ----------------- TX queue API -----------------

    def enqueue_bin_frame(self, payload: bytes) -> None:
        """
        HOT PATH: enqueue raw bytes to be sent to dongle as HID OUT report.
        """
        if not self.is_open:
            return

        if self._tx_q.full():
            try:
                _ = self._tx_q.get_nowait()
            except Exception:
                pass

        try:
            self._tx_q.put_nowait(payload)
        except Exception:
            pass

    def enqueue_line(self, line: str) -> None:
        framed = (line.strip() + "\n").encode("ascii", errors="replace")
        self.enqueue_bin_frame(framed)

    # ----------------- internals: connect/reconnect -----------------

    @staticmethod
    def _sleep_with_jitter(base_s: float, jitter_frac: float = 0.25) -> float:
        j = base_s * jitter_frac
        return max(0.05, base_s + random.uniform(-j, j))

    def _close(self) -> None:
        try:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:
                    pass
        finally:
            self._dev = None
            self._path = None
            self._rx_buf.clear()

    def _find_device_path(self) -> Optional[bytes]:
        """
        Find a hidraw device by VID/PID and optional product substring.
        Returns the 'path' bytes used by hid.device().open_path().
        """
        for d in hid.enumerate(self._vid, self._pid):
            try:
                prod = (d.get("product_string") or "") if isinstance(d, dict) else ""
                if self._product_contains and self._product_contains.lower() not in str(prod).lower():
                    continue
                path = d.get("path") if isinstance(d, dict) else None
                if isinstance(path, (bytes, bytearray)):
                    return bytes(path)
            except Exception:
                continue
        return None

    async def _connect_loop(self) -> None:
        backoff = self._reconnect_delay_s
        backoff_max = 8.0

        while True:
            if self.is_open:
                await asyncio.sleep(0.25)
                continue

            try:
                ok = await self._try_open_and_handshake()
                if ok:
                    logger.info("dongle ready via HID path=%s", self.active_path)
                    asyncio.create_task(self._log_state_once_after_open(), name="ble-dongle-state-once")

                    if self._keepalive_task is None or self._keepalive_task.done():
                        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="ble-dongle-keepalive")

                    if self._status_poll_s > 0 and (self._status_poll_task is None or self._status_poll_task.done()):
                        self._status_poll_task = asyncio.create_task(self._status_poll_loop(), name="ble-dongle-status-poll")

                    backoff = self._reconnect_delay_s
                    continue

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("open/handshake failed: %r", exc)

            await asyncio.sleep(self._sleep_with_jitter(backoff))
            backoff = min(backoff * 2.0, backoff_max)

    async def _try_open_and_handshake(self) -> bool:
        path = self._find_device_path()
        if not path:
            return False

        dev = hid.Device(path=path)
        dev.read_timeout = 50

        self._dev = dev
        self._path = path
        self.state = DongleState()
        self._pong_counter = 0
        self._ready_evt.clear()

        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._reader_loop(), name="ble-dongle-reader")
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop(), name="ble-dongle-writer")

        # small settle
        await asyncio.sleep(0.15)

        ok = await self._handshake_once(timeout_s=1.0)
        if ok:
            self._request_status_resync()
        else:
            await self._force_reconnect("handshake_timeout")
        return ok

    async def _force_reconnect(self, reason: str) -> None:
        logger.debug("forcing HID reconnect (%s)", reason)
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._writer_task is not None:
            self._writer_task.cancel()
            self._writer_task = None
        self._close()
        self.state = DongleState()
        self._ready_evt.clear()
        self._emit("link_lost")

    # ----------------- internals: reader/writer -----------------

    async def _writer_loop(self) -> None:
        while True:
            try:
                payload = await self._tx_q.get()
                if self._dev is None:
                    continue

                # HID writes usually expect: [report_id_byte] + data.
                # For report-id-less devices, report_id=0 is used.
                out = bytes([0x00]) + payload

                # Pad or truncate to the configured report size+1 (report_id included).
                target = 1 + self._out_report_bytes
                if len(out) < target:
                    out = out + (b"\x00" * (target - len(out)))
                elif len(out) > target:
                    out = out[:target]

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._dev.write, out)  # type: ignore[arg-type]

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("writer error, reconnecting: %r", exc)
                await self._force_reconnect("writer_error")
                await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))

    async def _reader_loop(self) -> None:
        while True:
            try:
                if self._dev is None:
                    await asyncio.sleep(0.1)
                    continue

                # hid.read(size) returns list[int] or bytes-like depending on wrapper;
                # the 'hid' package returns a list of ints.
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, self._dev.read, self._in_report_bytes)

                if not data:
                    await asyncio.sleep(0.01)
                    continue

                if isinstance(data, list):
                    chunk = bytes(data)
                elif isinstance(data, (bytes, bytearray)):
                    chunk = bytes(data)
                else:
                    chunk = bytes(data)

                self._ingest_rx_bytes(chunk)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("reader error, reconnecting: %r", exc)
                await self._force_reconnect("reader_error")
                await asyncio.sleep(self._sleep_with_jitter(self._reconnect_delay_s))

    def _ingest_rx_bytes(self, chunk: bytes) -> None:
        # Many HID implementations pad with zeros; strip trailing zeros but keep interior zeros.
        chunk = chunk.rstrip(b"\x00")
        if not chunk:
            return

        self._rx_buf.extend(chunk)

        while True:
            nl = self._rx_buf.find(b"\n")
            if nl < 0:
                # guard against runaway buffer
                if len(self._rx_buf) > 4096:
                    self._rx_buf.clear()
                return

            line = bytes(self._rx_buf[:nl]).decode("utf-8", errors="replace").strip()
            del self._rx_buf[: nl + 1]

            if not line:
                continue

            if line == "PONG":
                self._pong_counter += 1
                self._emit("pong")
                self._request_status_resync()
                continue

            if line.startswith("STATUS "):
                self._handle_status_line(line)
                continue

            if line.startswith("EVT "):
                self._handle_evt_line(line)
                continue

            # Allow firmware debug strings without crashing
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("dongle: %s", line)

    # ----------------- internals: handshake/keepalive/status -----------------

    async def _handshake_once(self, timeout_s: float) -> bool:
        start_pong = self._pong_counter
        self.enqueue_line("PING")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self._pong_counter > start_pong:
                return True
        return False

    async def _schedule_status_resync(self) -> None:
        await asyncio.sleep(0.2)
        if self.is_open:
            self.enqueue_line("STATUS")

    def _request_status_resync(self) -> None:
        if self._status_resync_task is None or self._status_resync_task.done():
            self._status_resync_task = asyncio.create_task(self._schedule_status_resync())

    async def _log_state_once_after_open(self) -> None:
        await asyncio.sleep(0.35)
        if not self.is_open:
            return

        if self.state.ready:
            logger.info("state: ready=True")
        elif self.state.advertising and not self.state.connected:
            logger.info("state: adv=True")
        elif self.state.connected and not self.state.ready:
            logger.info("state: conn=True ready=False")
        else:
            logger.info("state: present=True")

    async def _keepalive_loop(self) -> None:
        missed = 0
        while True:
            try:
                await asyncio.sleep(2.0)
                if not self.is_open:
                    missed = 0
                    continue

                before = self._pong_counter
                self.enqueue_line("PING")
                await asyncio.sleep(1.0)

                if self._pong_counter > before:
                    missed = 0
                else:
                    missed += 1
                    if missed >= 3:
                        missed = 0
                        await self._force_reconnect("keepalive_timeout")
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._force_reconnect("keepalive_error")

    async def _status_poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._status_poll_s + random.uniform(-0.5, 0.5))
                if not self.is_open or not self.state.connected:
                    continue
                self.enqueue_line("STATUS")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    # ----------------- parsing: STATUS / EVT -----------------

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

        notify: Dict[str, bool] = {}
        for k in ("kb_notify", "boot_notify", "cc_notify", "batt_notify"):
            if k in kv:
                notify[k] = (kv[k] == "1")
        self.state.notify = notify if notify else None

        conn_params: Dict[str, Any] = {}
        if "interval_ms" in kv:
            conn_params["interval_ms"] = self._to_float(kv.get("interval_ms"), 0.0)
        if "latency" in kv:
            conn_params["latency"] = self._to_int(kv.get("latency"))
        if "timeout_ms" in kv:
            conn_params["timeout_ms"] = self._to_int(kv.get("timeout_ms"))
        self.state.conn_params = conn_params if conn_params else None

        phy: Dict[str, Any] = {}
        if "phy_tx" in kv:
            phy["tx"] = self._to_int(kv.get("phy_tx"))
        if "phy_rx" in kv:
            phy["rx"] = self._to_int(kv.get("phy_rx"))
        self.state.phy = phy if phy else None

        self._emit("status")

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
            else:
                self._ready_evt.clear()
            self._emit("ready")
            if self.state.ready:
                self._request_status_resync()
            return

        if kind == "ADV":
            self.state.advertising = (rest[0] == "1")
            self._emit("adv")
            return

        if kind == "CONN":
            self.state.connected = (rest[0] == "1")
            self._emit("conn")
            self._request_status_resync()
            return

        if kind == "DISC":
            r = rest[0]
            if r.startswith("reason="):
                r = r.split("=", 1)[1]
            try:
                self.state.last_disc_reason = int(r)
            except Exception:
                self.state.last_disc_reason = None
            self._emit("disc")
            self._request_status_resync()
            return

        if kind == "CONN_PARAMS":
            kv: Dict[str, Any] = {}
            for tok in rest:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        kv[k] = int(v)
                    except Exception:
                        kv[k] = v
            self.state.conn_params = kv
            self._emit("conn_params")
            return

        if kind == "PHY":
            kv: Dict[str, Any] = {}
            for tok in rest:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k] = v
            self.state.phy = kv
            self._emit("phy")
            return

    def _emit(self, event: str) -> None:
        # Human-friendly INFO logs at this module, similar to BleSerial’s behavior :contentReference[oaicite:7]{index=7}
        if event in ("ready", "adv", "conn", "disc", "link_lost"):
            if event == "ready":
                logger.info("link ready=%s", 1 if self.state.ready else 0)
            elif event == "adv":
                logger.info("advertising=%s", 1 if self.state.advertising else 0)
            elif event == "conn":
                logger.info("connected=%s", 1 if self.state.connected else 0)
            elif event == "disc":
                logger.info("disconnected reason=%s", self.state.last_disc_reason)
            elif event == "link_lost":
                logger.warning("link lost; will retry")

        if self._on_event is not None:
            try:
                self._on_event(event, self.state)
            except Exception:
                logger.exception("on_event handler failed")

    # ----------------- HID tables from assets -----------------

    def _load_hid_tables_from_assets(self) -> Tuple[Dict[str, int], Dict[str, int]]:
        """
        Load hid_keymap.json from packaged assets (pihub.assets),
        matching the style used by dispatcher for keymap.json :contentReference[oaicite:8]{index=8}.
        """
        try:
            resource = importlib_resources.files("pihub.assets") / "hid_keymap.json"
            raw = resource.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("hid_keymap.json missing in pihub.assets (%r); HID codes will be empty", exc)
            return {}, {}

        try:
            import json
            data = json.loads(raw) or {}
        except Exception as exc:
            logger.warning("hid_keymap.json invalid JSON (%r); HID codes will be empty", exc)
            return {}, {}

        kb = {k: int(v) for k, v in (data.get("keyboard") or {}).items()}
        cc = {k: int(v) for k, v in (data.get("consumer") or {}).items()}
        return kb, cc

    def _encode_keyboard_down(self, code: str) -> Optional[bytes]:
        hid_code = self._kb.get(code)
        if hid_code is None:
            logger.warning("unknown keyboard code %s down; ignoring", code)
            return None
        # Boot keyboard 8 bytes: mods(1), reserved(1), key1..key6
        return bytes([0x00, 0x00, int(hid_code) & 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00])

    def _encode_consumer_usage(self, code: str) -> int:
        return int(self._cc.get(code) or 0)