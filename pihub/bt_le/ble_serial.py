"""
Serial transport to the nRF52840 "PiHub Remote" dongle over USB CDC ACM.

Protocol (PiHub -> dongle):
  KB <16 hex>\n      8-byte keyboard report
  CC <4 hex>\n       2-byte consumer report (little-endian 16-bit usage)
  PING\n            -> PONG
  STATUS\n          -> device-defined text dump
  UNPAIR\n          clear bonds + re-advertise

Telemetry (dongle -> PiHub), one line per event:
  EVT ADV 0|1
  EVT CONN 0|1
  EVT PROTO 0|1              (0=Report, 1=Boot)
  EVT CONN_PARAMS ...
  EVT PHY ...
  EVT DISC <reason>
  EVT ERR 0|1

This module is intentionally tolerant: unknown lines are forwarded to a log callback.
"""

from __future__ import annotations

import asyncio
import binascii
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import serial  # type: ignore
except Exception as e:  # pragma: no cover
    serial = None  # type: ignore


@dataclass
class DongleState:
    advertising: bool = False
    connected: bool = False
    proto_boot: Optional[bool] = None  # None unknown, True boot, False report
    error: bool = False
    # last-known negotiated params, if provided by dongle
    conn_params: dict = field(default_factory=dict)
    phy: dict = field(default_factory=dict)
    last_disconnect_reason: Optional[int] = None

    @property
    def link_ready(self) -> bool:
        # If dongle doesn't provide finer-grained readiness, treat "connected" as ready.
        return self.connected and not self.error


class SerialDongleTransport:
    """
    Threaded serial reader + async-friendly API.

    - Reading is done in a background thread.
    - Parsed EVT lines update `state` and optionally call `on_event`.
    - All other lines are passed to `on_log` (if provided).
    """

    def __init__(
        self,
        device: str,
        baud: int = 115200,
        *,
        on_event: Optional[Callable[[str, dict], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is required for SerialDongleTransport. Install with: pip install pyserial"
            )

        self._device = device
        self._baud = baud
        self._on_event = on_event
        self._on_log = on_log

        self.state = DongleState()

        self._ser = None
        self._rx_thread = None
        self._stop_evt = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._write_lock = threading.Lock()

    def start(self) -> None:
        if self._rx_thread is not None:
            return

        self._loop = asyncio.get_running_loop()
        self._stop_evt.clear()

        self._ser = serial.Serial(self._device, self._baud, timeout=0.5)
        self._rx_thread = threading.Thread(target=self._rx_worker, name="pihub-dongle-rx", daemon=True)
        self._rx_thread.start()

        # Ask for a status line at startup (non-fatal if unsupported)
        try:
            self.write_line("STATUS")
        except Exception:
            pass

    def stop(self) -> None:
        self._stop_evt.set()
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        self._rx_thread = None

        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def write_line(self, line: str) -> None:
        if not self._ser:
            raise RuntimeError("Serial port not open")

        data = (line.strip() + "\n").encode("ascii", errors="ignore")
        with self._write_lock:
            self._ser.write(data)

    def send_keyboard_report(self, report8: bytes) -> None:
        if len(report8) != 8:
            raise ValueError("Keyboard report must be exactly 8 bytes")
        self.write_line("KB " + binascii.hexlify(report8).decode("ascii"))

    def send_consumer_usage_le16(self, usage_le16: bytes) -> None:
        if len(usage_le16) != 2:
            raise ValueError("Consumer report must be exactly 2 bytes (LE16 usage)")
        self.write_line("CC " + binascii.hexlify(usage_le16).decode("ascii"))

    def ping(self) -> None:
        self.write_line("PING")

    def unpair(self) -> None:
        self.write_line("UNPAIR")

    # --- internal ---

    def _log(self, s: str) -> None:
        if self._on_log:
            self._on_log(s)

    def _emit_event(self, name: str, payload: dict) -> None:
        if self._on_event:
            self._on_event(name, payload)

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
                    try:
                        s = line.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if s:
                        self._handle_line(s)
            except Exception as e:
                self._log(f"[dongle] RX error: {e!r}")
                time.sleep(0.25)

    def _handle_line(self, s: str) -> None:
        # Telemetry events
        if s.startswith("EVT "):
            self._parse_evt(s)
            return

        # Common keepalive
        if s.strip() == "PONG":
            self._emit_event("PONG", {})
            return

        # Otherwise just forward to logs
        self._log(f"[dongle] {s}")

    def _parse_evt(self, s: str) -> None:
        # Basic, tolerant parser:
        # "EVT XYZ ..." -> name=XYZ, rest=...
        parts = s.split()
        if len(parts) < 2:
            return
        evt = parts[1]

        if evt == "ADV" and len(parts) >= 3:
            self.state.advertising = (parts[2] == "1")
            self._emit_event("ADV", {"advertising": self.state.advertising})
            return

        if evt == "CONN" and len(parts) >= 3:
            self.state.connected = (parts[2] == "1")
            self._emit_event("CONN", {"connected": self.state.connected})
            return

        if evt == "PROTO" and len(parts) >= 3:
            # 0=Report, 1=Boot
            if parts[2] in ("0", "1"):
                self.state.proto_boot = (parts[2] == "1")
            self._emit_event("PROTO", {"boot": self.state.proto_boot})
            return

        if evt == "ERR" and len(parts) >= 3:
            self.state.error = (parts[2] == "1")
            self._emit_event("ERR", {"error": self.state.error})
            return

        if evt == "DISC":
            # EVT DISC <reason>
            reason = None
            if len(parts) >= 3:
                try:
                    reason = int(parts[2], 0)
                except Exception:
                    reason = None
            self.state.last_disconnect_reason = reason
            self.state.connected = False
            self._emit_event("DISC", {"reason": reason})
            return

        if evt == "CONN_PARAMS":
            # Let dongle decide formatting; store tail as raw tokens.
            self.state.conn_params = {"raw": parts[2:]}
            self._emit_event("CONN_PARAMS", {"raw": parts[2:]})
            return

        if evt == "PHY":
            self.state.phy = {"raw": parts[2:]}
            self._emit_event("PHY", {"raw": parts[2:]})
            return

        # Unknown EVT
        self._emit_event(evt, {"raw": parts[2:]})
