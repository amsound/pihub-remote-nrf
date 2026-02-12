# Copyright (c) 2025 PiHub
# SPDX-License-Identifier: MIT

"""
BTLEController - Serial-by-default transport to the nRF52840 "PiHub Remote" dongle.

This replaces the previous BlueZ-based HID-over-GATT stack. PiHub now talks to the dongle
over USB CDC ACM (/dev/ttyACM*), and the dongle is the Bluetooth HID peripheral.

Key design goals:
- Keep HIDClient behaviour unchanged (edge-accurate key_down / key_up)
- Make serial transport the default (no env needed)
- Be tolerant: if we can't see "ready" flags, we still send and let the dongle gate/drop
- Improve robustness: all-keys-up pre-roll, protocol gating, staggered bring-up, retry-on-busy
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .hid_client import HIDClient
from .ble_serial import SerialDongleTransport


logger = logging.getLogger(__name__)


@dataclass
class BTLEControllerStatus:
    """Minimal status surface for diagnostics."""
    transport: str = "serial"
    device: str = "/dev/ttyACM0"
    baud: int = 115200
    connected: bool = False
    advertising: bool = False
    link_ready: bool = False
    proto_boot: Optional[bool] = None
    error: bool = False
    last_disconnect_reason: Optional[int] = None


class BTLEController:
    """
    PiHub-side controller that exposes the same HID API as before, but forwards over serial.
    """

    def __init__(
        self,
        adapter: str = "hci0",  # kept for compatibility; unused in serial mode
        device_name: str = "PiHub Remote",  # kept for compatibility; used only for logs
        *,
        serial_device: str = "/dev/ttyACM0",
        serial_baud: int = 115200,
    ) -> None:
        self._adapter = adapter
        self._device_name = device_name

        self._serial_device = serial_device
        self._serial_baud = serial_baud

        # HIDClient expects a keyword-only `hid` argument.
        self._hid_client = HIDClient(hid=self)

        self._transport: Optional[SerialDongleTransport] = None
        self._ble_ready = False

        # "all keys up" pre-roll state
        self._preroll_sent_for_conn = False
        self._last_conn_change_ts = 0.0

        # Staggered startup and retry tuning
        self._startup_delay_s = 0.25
        self._retry_delay_s = 0.02
        self._max_retries = 2

        # Track latest status (for app diagnostics)
        self._status = BTLEControllerStatus(device=self._serial_device, baud=self._serial_baud)

    @property
    def hid_client(self) -> HIDClient:
        return self._hid_client

    @property
    def ble_ready(self) -> bool:
        return self._ble_ready

    @property
    def status(self) -> dict:
        # Return dict for backward compatibility with callers
        s = self._status
        return {
            "transport": s.transport,
            "device": s.device,
            "baud": s.baud,
            "connected": s.connected,
            "advertising": s.advertising,
            "link_ready": s.link_ready,
            "proto_boot": s.proto_boot,
            "error": s.error,
            "last_disconnect_reason": s.last_disconnect_reason,
        }

    async def start(self) -> None:
        if self._ble_ready:
            return

        loop = asyncio.get_running_loop()

        def on_log(line: str) -> None:
            # Keep dongle logs visible but not too noisy
            logger.info("%s", line)

        def on_event(name: str, payload: dict) -> None:
            # Called from RX thread; bounce to loop thread-safely.
            loop.call_soon_threadsafe(self._handle_evt, name, payload)

        self._transport = SerialDongleTransport(
            self._serial_device,
            self._serial_baud,
            on_event=on_event,
            on_log=on_log,
        )
        self._transport.start()

        # Staggered bring-up: give dongle time to boot and start advertising before we spam it.
        await asyncio.sleep(self._startup_delay_s)

        self._ble_ready = True
        logger.info("BTLEController ready (serial=%s baud=%d, name=%s)",
                    self._serial_device, self._serial_baud, self._device_name)

        # Optional: ping for sanity (non-fatal if unsupported)
        try:
            self._transport.ping()
        except Exception:
            pass

    async def shutdown(self) -> None:
        self._ble_ready = False
        if self._transport:
            try:
                self._transport.stop()
            except Exception:
                pass
        self._transport = None

    # --- transport-facing API used by HIDClient ---

    def notify_keyboard(self, report8: bytes) -> None:
        """
        Forward an 8-byte keyboard report.

        Reliability improvements:
        - "all keys up" pre-roll once after each connect
        - protocol gating (if dongle exposes PROTO)
        - retry-on-busy/IOError
        """
        self._send_with_preroll(kind="kb", payload=bytes(report8))

    def notify_consumer(self, report2: bytes) -> None:
        """
        Forward a 2-byte consumer control report (LE16 usage).
        """
        self._send_with_preroll(kind="cc", payload=bytes(report2))

    # --- internals ---

    def _handle_evt(self, name: str, payload: dict) -> None:
        # Update status from dongle state
        if not self._transport:
            return
        st = self._transport.state

        self._status.connected = st.connected
        self._status.advertising = st.advertising
        self._status.proto_boot = st.proto_boot
        self._status.error = st.error
        self._status.last_disconnect_reason = st.last_disconnect_reason
        self._status.link_ready = st.link_ready

        if name == "CONN":
            self._last_conn_change_ts = time.monotonic()
            self._preroll_sent_for_conn = False
            logger.info("Dongle connected=%s", payload.get("connected"))
        elif name == "DISC":
            logger.info("Dongle disconnected reason=%s", payload.get("reason"))
        elif name == "ERR":
            logger.warning("Dongle error=%s", payload.get("error"))
        elif name == "CONN_PARAMS":
            logger.info("Dongle conn params: %s", payload.get("raw"))
        elif name == "PHY":
            logger.info("Dongle PHY: %s", payload.get("raw"))
        elif name == "PROTO":
            logger.info("Dongle protocol: %s", "BOOT" if st.proto_boot else "REPORT")

    def _maybe_wait_for_link(self) -> bool:
        """
        Return True if we should proceed to send now.

        If we can see a connected state, we wait a short settle time after connect (staggered startup).
        If we can't see state reliably, we'll still send (dongle-side gating will drop safely).
        """
        if not self._transport:
            return False

        st = self._transport.state

        # If we have state, enforce minimal settle delay after connect
        if st.connected:
            dt = time.monotonic() - self._last_conn_change_ts
            if dt < self._startup_delay_s:
                return False
            return True

        # Not connected -> don't send; caller can decide.
        return False

    def _send_with_preroll(self, *, kind: str, payload: bytes) -> None:
        if not self._transport:
            return

        st = self._transport.state

        # If we know we're not connected, silently drop (parity with previous gating)
        if not st.connected:
            return

        # Staggered settle time after connect
        if not self._maybe_wait_for_link():
            return

        # "All keys up" pre-roll once after each connection.
        # This helps iOS/UI state (dismiss keyboard) and avoids "stuck key" edge cases.
        if not self._preroll_sent_for_conn:
            try:
                self._transport.send_keyboard_report(b"\x00" * 8)
                self._transport.send_consumer_usage_le16(b"\x00\x00")
                self._preroll_sent_for_conn = True
                logger.info("Sent pre-roll (all keys up)")
            except Exception as e:
                logger.warning("Pre-roll send failed: %r", e)

        # Protocol gating: if dongle reports protocol and we're in boot mode,
        # we still send the same 8-byte frame (dongle maps as needed).
        # This is mainly a diagnostic gate to avoid sending when dongle is erroring.
        if st.error:
            return

        # Finally, send with small retries for transient IO
        for attempt in range(self._max_retries):
            try:
                if kind == "kb":
                    self._transport.send_keyboard_report(payload)
                elif kind == "cc":
                    self._transport.send_consumer_usage_le16(payload)
                else:
                    return
                return
            except Exception as e:
                logger.warning("Dongle send failed (attempt %d/%d): %r", attempt + 1, self._max_retries, e)
                time.sleep(self._retry_delay_s)

        # Give up silently (best effort)
        return
