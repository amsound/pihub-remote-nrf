"""Serial transport to the nRF52840 "PiHub Remote" dongle over USB CDC ACM."""

from __future__ import annotations

import asyncio
import binascii
import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None  # type: ignore


@dataclass
class DongleState:
    ready: bool = False
    advertising: bool = False
    connected: bool = False
    proto_boot: Optional[bool] = None
    error: bool = False
    conn_params: dict = field(default_factory=dict)
    phy: dict = field(default_factory=dict)
    last_disconnect_reason: Optional[int] = None

    @property
    def link_ready(self) -> bool:
        return self.connected and not self.error


class SerialDongleTransport:
    """Threaded serial reader with async-friendly write helpers."""

    def __init__(
        self,
        device: str,
        baud: int = 115200,
        *,
        on_event: Optional[Callable[[str, dict], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        if serial is None:
            raise RuntimeError("pyserial is required for SerialDongleTransport")

        self._device = device
        self._baud = baud
        self._on_event = on_event
        self._on_log = on_log

        self.state = DongleState()

        self._ser = None
        self._rx_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._write_lock = threading.Lock()
        self._lines: Optional[asyncio.Queue[str]] = None

    async def start(self) -> None:
        if self._rx_thread is not None:
            return

        self._loop = asyncio.get_running_loop()
        self._lines = asyncio.Queue()
        self._stop_evt.clear()

        self._ser = await asyncio.to_thread(serial.Serial, self._device, self._baud, timeout=0.5)
        self._rx_thread = threading.Thread(target=self._rx_worker, name="pihub-dongle-rx", daemon=True)
        self._rx_thread.start()

        with contextlib.suppress(Exception):
            await self.write_line("STATUS")

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._rx_thread:
            await asyncio.to_thread(self._rx_thread.join, 1.0)
        self._rx_thread = None

        ser, self._ser = self._ser, None
        if ser:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(ser.close)

    async def write_line(self, line: str) -> None:
        if not self._ser:
            raise RuntimeError("Serial port not open")

        data = (line.strip() + "\n").encode("ascii", errors="ignore")

        def _write() -> None:
            with self._write_lock:
                assert self._ser is not None
                self._ser.write(data)

        await asyncio.to_thread(_write)

    async def send_keyboard_report(self, report8: bytes) -> str:
        if len(report8) != 8:
            raise ValueError("Keyboard report must be exactly 8 bytes")
        return await self._send_with_reply("KB " + binascii.hexlify(report8).decode("ascii"))

    async def send_consumer_usage_le16(self, usage_le16: bytes) -> str:
        if len(usage_le16) != 2:
            raise ValueError("Consumer report must be exactly 2 bytes")
        return await self._send_with_reply("CC " + binascii.hexlify(usage_le16).decode("ascii"))

    async def ping(self) -> None:
        await self.write_line("PING")

    async def unpair(self) -> None:
        await self.write_line("UNPAIR")

    async def _send_with_reply(self, cmd: str, timeout: float = 0.15) -> str:
        await self.write_line(cmd)
        if self._lines is None:
            return "sent"
        try:
            line = await asyncio.wait_for(self._lines.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return "sent"

        upper = line.strip().upper()
        if "BUSY" in upper:
            return "busy"
        if upper in {"OK", "ACK"}:
            return "ok"
        return "sent"

    def _log(self, s: str) -> None:
        if self._on_log:
            self._on_log(s)

    def _emit_event(self, name: str, payload: dict) -> None:
        if self._on_event:
            self._on_event(name, payload)

    def _enqueue_line(self, s: str) -> None:
        if self._loop is None or self._lines is None:
            return
        self._loop.call_soon_threadsafe(self._lines.put_nowait, s)

    def _rx_worker(self) -> None:
        assert self._ser is not None
        buf = b""

        while not self._stop_evt.is_set():
            try:
                chunk = self._ser.read(256)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", errors="replace").strip()
                    if s:
                        self._handle_line(s)
            except Exception as exc:
                self._log(f"[dongle] RX error: {exc!r}")
                time.sleep(0.25)

    def _handle_line(self, s: str) -> None:
        s = s.strip("\r\n")
        if not s:
            return

        # Keep hot-path protocol chatter off the logs.
        if s == "PONG":
            self._emit_event("PONG")
            return
        if s == "PING":
            return

        if s.startswith("EVT "):
            self._parse_evt(s)
            return

        # Anything else is treated as a plain firmware log line.
        self._enqueue_line(s)
        self._log(f"[dongle] {s}")

    def _parse_evt(self, line: str) -> None:
        # Format:
        #   EVT <TYPE> <PAYLOAD...>
        parts = line.split()
        if len(parts) < 2:
            return

        etype = parts[1]
        payload = parts[2:]

        if etype == "READY":
            v = payload[0] if payload else "0"
            self.state.ready = (v == "1")
            self._emit_event("READY", {"ready": self.state.ready})
            return

        if etype == "ADV":
            v = payload[0] if payload else "0"
            self.state.advertising = (v == "1")
            self._emit_event("ADV", {"advertising": self.state.advertising})
            return

        if etype == "CONN":
            v = payload[0] if payload else "0"
            self.state.connected = (v == "1")
            self._emit_event("CONN", {"connected": self.state.connected})
            return

        if etype == "PROTO":
            v = payload[0] if payload else "0"
            self.state.proto_boot = (v == "1")
            self._emit_event("PROTO", {"proto_boot": self.state.proto_boot})
            return

        if etype == "ERR":
            v = payload[0] if payload else "0"
            self.state.error = (v == "1")
            self._emit_event("ERR", {"error": self.state.error})
            return

        if etype == "DISC":
            reason = -1
            if payload:
                try:
                    reason = int(payload[0], 0)
                except ValueError:
                    reason = -1
            self.state.last_disc_reason = reason
            self._emit_event("DISC", {"reason": reason})
            return

        if etype == "CONN_PARAMS":
            # Example:
            #   EVT CONN_PARAMS interval_ms_x100=3000 latency=0 timeout_ms=720
            kv: dict[str, int] = {}
            for tok in payload:
                if "=" not in tok:
                    continue
                k, v = tok.split("=", 1)
                try:
                    kv[k] = int(v, 0)
                except ValueError:
                    continue

            out: dict[str, object] = dict(kv)
            if "interval_ms_x100" in kv:
                out["interval_ms"] = kv["interval_ms_x100"] / 100.0
            self._emit_event("CONN_PARAMS", out)
            return

        if etype == "PHY":
            # Example:
            #   EVT PHY tx=2M rx=2M
            tx = None
            rx = None
            for tok in payload:
                if tok.startswith("tx="):
                    tx = tok[3:]
                elif tok.startswith("rx="):
                    rx = tok[3:]
            self._emit_event("PHY", {"tx": tx, "rx": rx})
            return

        # Unknown event type: pass through.
        self._emit_event(etype, {"raw": payload})
