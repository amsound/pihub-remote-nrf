import asyncio
import glob
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Dict, Any, List

import serial  # pyserial

log = logging.getLogger(__name__)


@dataclass
class DongleState:
    ready: bool = False
    advertising: bool = False
    connected: bool = False
    proto_boot: bool = False  # 0=report, 1=boot
    error: bool = False

    sec: int = 0
    suspend: bool = False
    notify: Optional[Dict[str, bool]] = None

    conn_params: Optional[Dict[str, Any]] = None
    phy: Optional[Dict[str, Any]] = None
    last_disc_reason: Optional[int] = None


class BleSerial:
    def __init__(
        self,
        ports: Sequence[str],
        baud: int = 115200,
        *,
        ping_timeout_s: float = 3.0,
        reconnect_delay_s: float = 1.0,
        status_poll_s: float = 30.0,  # 0 disables periodic STATUS polling
        on_event: Optional[Callable[[str, DongleState], None]] = None,
    ) -> None:
        self._ports = list(ports)
        self._baud = baud
        self._ping_timeout_s = ping_timeout_s
        self._reconnect_delay_s = reconnect_delay_s
        self._status_poll_s = status_poll_s
        self._on_event = on_event

        self._ser: Optional[serial.Serial] = None
        self._active_port: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._status_resync_task: Optional[asyncio.Task] = None
        self._status_poll_task: Optional[asyncio.Task] = None

        self.state = DongleState()
        self._pong_counter = 0

        self._tx_lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    @property
    def active_port(self) -> Optional[str]:
        return self._active_port

    @property
    def serial_ready(self) -> bool:
        return bool(self.is_open and self.state.ready)

    async def start(self) -> None:
        if self._connect_task is None:
            self._connect_task = asyncio.create_task(self._connect_loop(), name="ble-serial-connect")

    async def stop(self) -> None:
        if self._connect_task is not None:
            self._connect_task.cancel()
            self._connect_task = None

        for task_attr in ("_reader_task", "_keepalive_task", "_status_resync_task", "_status_poll_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                setattr(self, task_attr, None)

        self._close()

    def _close(self) -> None:
        try:
            if self._ser is not None:
                self._ser.close()
        finally:
            self._ser = None
            self._active_port = None

    async def _connect_loop(self) -> None:
        backoff = self._reconnect_delay_s
        while True:
            if self.is_open:
                await asyncio.sleep(self._reconnect_delay_s)
                continue

            connected = False
            for port in list(self._ports):
                try:
                    if await self._try_open_and_handshake(port):
                        log.info("BTLE serial command port ready on %s", port)

                        if self._keepalive_task is None or self._keepalive_task.done():
                            self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="ble-serial-keepalive")

                        if self._status_poll_s > 0 and (self._status_poll_task is None or self._status_poll_task.done()):
                            self._status_poll_task = asyncio.create_task(self._status_poll_loop(), name="ble-serial-status-poll")

                        connected = True
                        backoff = self._reconnect_delay_s
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("serial open/handshake failed on %s: %r", port, e)

            if not connected:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 8.0)

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

        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._reader_loop(), name="ble-serial-reader")

        await asyncio.sleep(0.25)
        ok = await self._handshake_once(timeout_s=1.0)

        if ok:
            self._request_status_resync()

        if not ok:
            await self._force_reconnect("handshake_timeout")
        return ok

    async def _reader_loop(self) -> None:
        while True:
            try:
                line = await self._read_line()
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

                # ignore unknown lines silently
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Serial reader error, will reconnect: %r", e)
                await self._force_reconnect("reader_error")
                await asyncio.sleep(self._reconnect_delay_s)

    def _emit(self, event: str) -> None:
        # This module-level logging is what you asked for:
        # [pihub.bt_le.ble_serial] BTLE Connected / Advertising / Ready...
        if event in ("ready", "adv", "conn", "disc", "link_lost"):
            try:
                if event == "ready":
                    log.info("BTLE Ready=%s", 1 if self.state.ready else 0)
                elif event == "adv":
                    log.info("BTLE Advertising=%s", 1 if self.state.advertising else 0)
                elif event == "conn":
                    log.info("BTLE Connected=%s", 1 if self.state.connected else 0)
                elif event == "disc":
                    log.info("BTLE Disconnected reason=%s", self.state.last_disc_reason)
                elif event == "link_lost":
                    log.warning("BTLE Serial link lost; reconnecting")
            except Exception:
                pass

        if self._on_event is not None:
            try:
                self._on_event(event, self.state)
            except Exception:
                log.exception("on_event handler failed")

    async def _read_line(self) -> str:
        if not self.is_open:
            return ""
        loop = asyncio.get_running_loop()
        raw: bytes = await loop.run_in_executor(None, self._ser.readline)  # type: ignore[arg-type]
        if not raw:
            return ""
        try:
            return raw.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    async def _write_line(self, line: str) -> None:
        if not self.is_open:
            return
        loop = asyncio.get_running_loop()
        framed = line.strip() + "\n"
        data = framed.encode("ascii", errors="replace")
        async with self._tx_lock:
            await loop.run_in_executor(None, self._ser.write, data)  # type: ignore[arg-type]
            await loop.run_in_executor(None, self._ser.flush)  # type: ignore[arg-type]

    async def _handshake_once(self, timeout_s: float) -> bool:
        start_pong = self._pong_counter
        await self._write_line("PING")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self._pong_counter > start_pong:
                return True
        return False

    async def _force_reconnect(self, reason: str) -> None:
        log.warning("forcing serial reconnect (%s)", reason)
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        self._close()
        self.state = DongleState()
        self._emit("link_lost")

    async def _schedule_status_resync(self) -> None:
        await asyncio.sleep(0.2)
        if self.is_open:
            try:
                await self._write_line("STATUS")
            except Exception:
                pass

    def _request_status_resync(self) -> None:
        if self._status_resync_task is None or self._status_resync_task.done():
            self._status_resync_task = asyncio.create_task(self._schedule_status_resync())

    async def _keepalive_loop(self) -> None:
        missed = 0
        while True:
            try:
                await asyncio.sleep(2.0)
                if not self.serial_ready:
                    missed = 0
                    continue

                before = self._pong_counter
                await self._write_line("PING")
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
                pass

    async def _status_poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._status_poll_s + random.uniform(-0.5, 0.5))
                if not self.is_open:
                    continue
                if not self.state.connected:
                    continue
                await self._write_line("STATUS")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    # ---------- Public command helpers ----------

    async def send_kb(self, report8: bytes) -> None:
        if not (self.serial_ready and self.state.connected):
            return
        if len(report8) != 8:
            raise ValueError("keyboard report must be 8 bytes")
        await self._write_line("KB " + report8.hex().upper())

    async def send_cc_usage(self, usage: int) -> None:
        if not (self.serial_ready and self.state.connected):
            return
        usage &= 0xFFFF
        le = bytes((usage & 0xFF, (usage >> 8) & 0xFF))
        await self._write_line("CC " + le.hex().upper())

    async def status(self) -> None:
        if not self.is_open:
            return
        await self._write_line("STATUS")

    async def unpair(self) -> None:
        if not self.is_open:
            return
        await self._write_line("UNPAIR")

    # ---------- STATUS parsing ----------

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

        self._emit("status")

    # ---------- EVT parsing ----------

    def _handle_evt_line(self, line: str) -> None:
        parts = line.split()
        if len(parts) < 3:
            return

        kind = parts[1]
        rest = parts[2:]

        if kind == "READY":
            self.state.ready = (rest[0] == "1")
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

        if kind == "PROTO":
            self.state.proto_boot = (rest[0] == "1")
            self._emit("proto")
            return

        if kind == "ERR":
            self.state.error = (rest[0] == "1")
            self._emit("err")
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


def discover_cmd_ports() -> List[str]:
    """
    With the new firmware you now expose exactly ONE port.
    Prefer ttyACM0. Fall back to any /dev/ttyACM* if present.
    """
    if os.path.exists("/dev/ttyACM0"):
        return ["/dev/ttyACM0"]
    ports = sorted(glob.glob("/dev/ttyACM*"))
    return [p for p in ports if os.path.exists(p)]