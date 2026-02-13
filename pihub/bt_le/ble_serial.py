import asyncio
import glob
import logging
import os
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
    conn_params: Optional[Dict[str, Any]] = None
    phy: Optional[Dict[str, Any]] = None
    last_disc_reason: Optional[int] = None


class BleSerial:
    """
    Serial link to the nRF dongle.

    - Line-oriented ASCII protocol.
    - CMD port carries both host commands (KB/CC/PING/STATUS/UNPAIR) and dongle EVT lines.
    """

    def __init__(
        self,
        ports: Sequence[str],
        baud: int = 115200,
        *,
        ping_timeout_s: float = 3.0,
        reconnect_delay_s: float = 1.0,
        on_event: Optional[Callable[[str, DongleState], None]] = None,
    ) -> None:
        self._ports = list(ports)
        self._baud = baud
        self._ping_timeout_s = ping_timeout_s
        self._reconnect_delay_s = reconnect_delay_s
        self._on_event = on_event

        self._ser: Optional[serial.Serial] = None
        self._active_port: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._connect_task: Optional[asyncio.Task] = None

        self.state = DongleState()

        # tx lock so KB/CC writes don't interleave
        self._tx_lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    @property
    def active_port(self) -> Optional[str]:
        return self._active_port

    async def start(self) -> None:
        if self._connect_task is None:
            self._connect_task = asyncio.create_task(self._connect_loop(), name="ble-serial-connect")

    async def stop(self) -> None:
        if self._connect_task is not None:
            self._connect_task.cancel()
            self._connect_task = None

        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

        self._close()

    def _close(self) -> None:
        try:
            if self._ser is not None:
                self._ser.close()
        finally:
            self._ser = None
            self._active_port = None

    async def _connect_loop(self) -> None:
        # Keep trying forever; app should not crash if dongle is absent at startup.
        while True:
            if self.is_open:
                await asyncio.sleep(self._reconnect_delay_s)
                continue

            for port in list(self._ports):
                try:
                    if await self._try_open_and_handshake(port):
                        log.info("BTLE serial command port ready on %s", port)
                        self._reader_task = asyncio.create_task(self._reader_loop(), name="ble-serial-reader")
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("serial open/handshake failed on %s: %r", port, e)

            await asyncio.sleep(self._reconnect_delay_s)

    async def _try_open_and_handshake(self, port: str) -> bool:
        # Open in non-blocking-ish mode; we'll read using executor.
        ser = serial.Serial(
            port=port,
            baudrate=self._baud,
            timeout=0.1,
            write_timeout=0.5,
            exclusive=True,
        )

        # Some CDC ACM implementations need DTR asserted.
        try:
            ser.setDTR(True)
            ser.setRTS(True)
        except Exception:
            pass

        # Flush any stale bytes.
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass

        self._ser = ser
        self._active_port = port

        # Give firmware a moment to attach endpoints, especially after container restart.
        await asyncio.sleep(0.25)

        # Send a PING; accept either an immediate PONG or an EVT READY line.
        await self._write_line("PING")

        deadline = time.monotonic() + self._ping_timeout_s
        while time.monotonic() < deadline:
            line = await self._read_line()
            if not line:
                continue
            if line == "PONG":
                self.state.ready = True
                self._emit("ready")
                return True
            if line.startswith("EVT "):
                self._handle_evt_line(line)
                if self.state.ready:
                    return True

        # Some dongle firmware builds can take a little longer to emit READY/PONG
        # after the CDC ACM port opens. Keep the link open and let the background
        # reader observe EVT lines, rather than churning reconnects.
        log.debug("serial open on %s, waiting for asynchronous READY/PONG", port)
        return True

    async def _reader_loop(self) -> None:
        while True:
            try:
                line = await self._read_line()
                if not line:
                    continue

                if line.startswith("EVT "):
                    log.debug("[serial-rx %s] %s", self._active_port, line)
                    self._handle_evt_line(line)
                    continue

                # Keep non-EVT lines visible (e.g., BUSY/ERR/PONG echoes/boot banners).
                log.info("[serial-rx %s] %s", self._active_port, line)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Serial reader error, will reconnect: %r", e)
                self._close()
                # Clear state on link loss; dongle will re-emit after reconnect.
                self.state = DongleState()
                self._emit("link_lost")
                await asyncio.sleep(self._reconnect_delay_s)

    def _emit(self, event: str) -> None:
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
            s = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
        return s

    @staticmethod
    def _sanitize_line_for_log(line: str) -> str:
        printable = []
        for ch in line:
            code = ord(ch)
            if 32 <= code <= 126:
                printable.append(ch)
            else:
                printable.append(f"\\x{code:02x}")
        return "".join(printable)

    async def _write_line(self, line: str) -> None:
        if not self.is_open:
            return
        loop = asyncio.get_running_loop()
        framed = line.strip() + "\n"
        data = framed.encode("ascii")
        async with self._tx_lock:
            sanitized = self._sanitize_line_for_log(framed)
            log.info("[serial-tx %s] %s", self._active_port, sanitized)
            wrote = await loop.run_in_executor(None, self._ser.write, data)  # type: ignore[arg-type]
            log.info("[serial-tx %s] write()=%s bytes", self._active_port, wrote)
            await loop.run_in_executor(None, self._ser.flush)  # type: ignore[arg-type]
            log.info("[serial-tx %s] flush() complete", self._active_port)

    # ---------- Public command helpers ----------

    async def ping(self) -> bool:
        if not self.is_open:
            return False
        await self._write_line("PING")
        deadline = time.monotonic() + self._ping_timeout_s
        while time.monotonic() < deadline:
            line = await self._read_line()
            if line == "PONG":
                return True
            if line.startswith("EVT "):
                self._handle_evt_line(line)
                if self.state.ready:
                    return True
        return False

    async def send_kb(self, report8: bytes) -> None:
        # Hot path: if not connected, drop silently.
        if not (self.is_open and (self.state.connected or self.state.ready)):
            return
        if len(report8) != 8:
            raise ValueError("keyboard report must be 8 bytes")
        await self._write_line("KB " + report8.hex().upper())

    async def send_cc_usage(self, usage: int) -> None:
        if not (self.is_open and (self.state.connected or self.state.ready)):
            return
        usage &= 0xFFFF
        # little-endian 2 bytes -> 4 hex chars
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

    # ---------- EVT parsing ----------

    def _handle_evt_line(self, line: str) -> None:
        # Examples:
        # EVT READY 1
        # EVT ADV 0|1
        # EVT CONN 0|1
        # EVT PROTO 0|1
        # EVT CONN_PARAMS interval_ms_x100=3000 latency=0 timeout_ms=720
        # EVT PHY tx=2M rx=2M
        # EVT DISC reason=19
        # EVT ERR 0|1
        parts = line.split()
        if len(parts) < 3:
            return

        kind = parts[1]
        rest = parts[2:]

        if kind == "READY":
            self.state.ready = (rest[0] == "1")
            self._emit("ready")
            return

        if kind == "ADV":
            self.state.advertising = (rest[0] == "1")
            self._emit("adv")
            return

        if kind == "CONN":
            self.state.connected = (rest[0] == "1")
            self._emit("conn")
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
            # allow "reason=19" or "19"
            r = rest[0]
            if r.startswith("reason="):
                r = r.split("=", 1)[1]
            try:
                self.state.last_disc_reason = int(r)
            except Exception:
                self.state.last_disc_reason = None
            self._emit("disc")
            return

        if kind == "CONN_PARAMS":
            kv = {}
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
            kv = {}
            for tok in rest:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k] = v
            self.state.phy = kv
            self._emit("phy")
            return


def discover_cmd_ports() -> List[str]:
    """Best-effort command-port discovery.

    Prefer /dev/serial/by-id symlinks ending with interface suffixes known to
    carry command traffic on the nRF52840 dongle:
      - if02 (preferred)
      - if00 (fallback on alternate firmware builds)
    """
    candidates: List[str] = []
    for suffix in ("if02", "if00"):
        pattern = f"/dev/serial/by-id/*{suffix}*"
        for path in sorted(glob.glob(pattern)):
            if os.path.exists(path) and path not in candidates:
                candidates.append(path)

    for tty in ("/dev/ttyACM1", "/dev/ttyACM0"):
        if os.path.exists(tty) and tty not in candidates:
            candidates.append(tty)
    return candidates
