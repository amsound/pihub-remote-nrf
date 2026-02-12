# Copyright (c) 2025 PiHub
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .ble_serial import SerialDongleTransport
from .hid_client import HIDClient

logger = logging.getLogger(__name__)


@dataclass
class BTLEControllerStatus:
    transport: str = "serial"
    device: str = "/dev/ttyACM0"
    baud: int = 115200
    adapter_present: bool = False
    connected: bool = False
    advertising: bool = False
    link_ready: bool = False
    proto_boot: Optional[bool] = None
    error: bool = False
    last_disconnect_reason: Optional[int] = None

    # Negotiated link details (best-effort telemetry)
    conn_interval_ms: Optional[float] = None
    conn_latency: Optional[int] = None
    conn_timeout_ms: Optional[int] = None
    phy_tx: Optional[str] = None
    phy_rx: Optional[str] = None


class BTLEController:
    """PiHub-side BLE controller backed by serial CDC ACM transport."""

    def __init__(
        self,
        adapter: str = "hci0",
        device_name: str = "PiHub Remote",
        *,
        serial_device: str = "/dev/ttyACM0",
        serial_baud: int = 115200,
    ) -> None:
        self._adapter = adapter
        self._device_name = device_name
        self._serial_device = serial_device
        self._serial_baud = serial_baud

        self._hid_client = HIDClient(hid=self)

        self._transport: Optional[SerialDongleTransport] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._ready_event = asyncio.Event()
        self._pong_future: Optional[asyncio.Future[None]] = None
        self._write_lock = asyncio.Lock()

        self._retry_delay_s = 0.05
        self._max_retries = 3
        self._connect_backoff_s = 1.0

        self._preroll_sent_for_conn = False
        self._last_conn_change_ts = 0.0
        self._startup_delay_s = 0.25

        self._status = BTLEControllerStatus(device=serial_device, baud=serial_baud)

    @property
    def hid_client(self) -> HIDClient:
        return self._hid_client

    @property
    def ble_ready(self) -> bool:
        return self._status.link_ready

    @property
    def status(self) -> dict:
        s = self._status
        return {
            "transport": s.transport,
            "device": s.device,
            "baud": s.baud,
            "adapter_present": s.adapter_present,
            "connected": s.connected,
            "advertising": s.advertising,
            "link_ready": s.link_ready,
            "proto_boot": s.proto_boot,
            "error": s.error,
            "last_disconnect_reason": s.last_disconnect_reason,
        }

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="pihub-btle-controller")

    async def stop(self) -> None:
        self._running = False
        self._ready_event.clear()

        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        await self._disconnect_transport()

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        if self._status.link_ready:
            return True
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def notify_keyboard(self, report8: bytes) -> None:
        if len(report8) != 8:
            return
        self._schedule_send("kb", bytes(report8))

    def notify_consumer(self, usage_or_report, pressed: Optional[bool] = None) -> None:
        if isinstance(usage_or_report, int):
            if pressed is False:
                payload = b"\x00\x00"
            else:
                payload = int(usage_or_report).to_bytes(2, "little", signed=False)
        else:
            payload = bytes(usage_or_report)
        if len(payload) != 2:
            return
        self._schedule_send("cc", payload)

    async def _run(self) -> None:
        while self._running:
            if self._transport is None:
                await self._connect_first_available()
                if self._transport is None:
                    await asyncio.sleep(self._connect_backoff_s)
                    continue

            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BTLE controller loop error")

    async def _connect_first_available(self) -> None:
        candidates = [self._serial_device]
        if self._serial_device.endswith("ttyACM0"):
            candidates.append(self._serial_device[:-1] + "1")
        elif self._serial_device.endswith("ttyACM1"):
            candidates.append(self._serial_device[:-1] + "0")

        for dev in candidates:
            transport = SerialDongleTransport(device=dev, baud=self._serial_baud, on_event=self._handle_evt)
            try:
                await transport.start()
                self._transport = transport
                self._status.adapter_present = True
                self._status.device = dev

                if await self._await_pong(timeout=0.8):
                    logger.info("BTLE serial command port ready on %s", dev)
                    return

                logger.warning("Serial %s opened but did not answer PING", dev)
            except Exception as exc:
                logger.warning("Unable to open serial dongle on %s: %r", dev, exc)
            await transport.stop()
            if self._transport is transport:
                self._transport = None

        self._status.adapter_present = False

    async def _disconnect_transport(self) -> None:
        transport, self._transport = self._transport, None
        if transport:
            with contextlib.suppress(Exception):
                await transport.stop()

    async def _await_pong(self, timeout: float) -> bool:
        transport = self._transport
        if transport is None:
            return False

        self._pong_future = asyncio.get_running_loop().create_future()
        try:
            await transport.ping()
            await asyncio.wait_for(self._pong_future, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._pong_future = None

    def _handle_evt(self, etype: str, payload: dict) -> None:
        # Mirror dongle EVT lines into status and keep logging human-friendly.
        # Avoid spamming: only log when a value changes.
        def log_change(key: str, value: object, msg: str) -> None:
            if self._last_evt.get(key) == value:
                return
            self._last_evt[key] = value
            self._log.info(msg)

        if etype == "PONG":
            if self._pong_future and not self._pong_future.done():
                self._pong_future.set_result(None)
            return

        if etype == "READY":
            ready = bool(payload.get("ready", False))
            self._status.adapter_present = ready
            log_change("READY", ready, f"[btle] dongle ready={1 if ready else 0}")
            return

        if etype == "ADV":
            adv = bool(payload.get("advertising", False))
            self._status.advertising = adv
            log_change("ADV", adv, f"[btle] advertising={1 if adv else 0}")
            return

        if etype == "CONN":
            conn = bool(payload.get("connected", False))
            self._status.connected = conn
            self._status.link_ready = bool(conn) and (not self._status.error)

            log_change("CONN", conn, f"[btle] connected={1 if conn else 0}")

            if conn:
                try:
                    self._all_keys_up_work.schedule(self._preroll_delay_s)
                except Exception:
                    pass

            if self._status.link_ready:
                self._ready_event.set()
            else:
                self._ready_event.clear()
            return

        if etype == "PROTO":
            proto_boot = bool(payload.get("proto_boot", False))
            self._status.proto_boot = proto_boot
            log_change("PROTO", proto_boot, f"[btle] proto={'boot' if proto_boot else 'report'}")
            return

        if etype == "ERR":
            err = bool(payload.get("error", False))
            self._status.error = err
            self._status.link_ready = self._status.connected and (not err)
            log_change("ERR", err, f"[btle] error={1 if err else 0}")

            if self._status.link_ready:
                self._ready_event.set()
            else:
                self._ready_event.clear()
            return

        if etype == "DISC":
            reason = payload.get("reason")
            self._status.last_disconnect_reason = int(reason) if isinstance(reason, int) else None
            self._status.connected = False
            self._status.link_ready = False
            log_change("DISC", self._status.last_disconnect_reason, f"[btle] disconnected reason={self._status.last_disconnect_reason}")

            self._ready_event.clear()
            return

        if etype == "CONN_PARAMS":
            interval_ms = payload.get("interval_ms")
            if interval_ms is None and "interval_ms_x100" in payload:
                try:
                    interval_ms = float(payload["interval_ms_x100"]) / 100.0
                except Exception:
                    interval_ms = None

            latency = payload.get("latency")
            timeout_ms = payload.get("timeout_ms")

            self._status.conn_interval_ms = float(interval_ms) if interval_ms is not None else None
            self._status.conn_latency = int(latency) if latency is not None else None
            self._status.conn_timeout_ms = int(timeout_ms) if timeout_ms is not None else None

            log_change(
                "CONN_PARAMS",
                (self._status.conn_interval_ms, self._status.conn_latency, self._status.conn_timeout_ms),
                f"[btle] conn_params interval_ms={self._status.conn_interval_ms} latency={self._status.conn_latency} timeout_ms={self._status.conn_timeout_ms}",
            )
            return

        if etype == "PHY":
            tx = payload.get("tx")
            rx = payload.get("rx")
            self._status.phy_tx = str(tx) if tx is not None else None
            self._status.phy_rx = str(rx) if rx is not None else None
            log_change("PHY", (self._status.phy_tx, self._status.phy_rx), f"[btle] phy tx={self._status.phy_tx} rx={self._status.phy_rx}")
            return

        # Unknown EVT: keep it at debug to avoid noise
        self._log.debug(f"[btle] evt {etype} {payload!r}")


    def _schedule_send(self, kind: str, payload: bytes) -> None:
        loop = asyncio.get_running_loop()
        loop.create_task(self._send_with_retry(kind, payload))

    async def _send_with_retry(self, kind: str, payload: bytes) -> None:
        if self._transport is None or not self._status.connected:
            return

        if time.monotonic() - self._last_conn_change_ts < self._startup_delay_s:
            await asyncio.sleep(self._startup_delay_s)

        async with self._write_lock:
            if self._transport is None:
                return

            if not self._preroll_sent_for_conn:
                await self._transport.send_keyboard_report(b"\x00" * 8)
                await self._transport.send_consumer_usage_le16(b"\x00\x00")
                self._preroll_sent_for_conn = True

            for attempt in range(self._max_retries):
                try:
                    if kind == "kb":
                        result = await self._transport.send_keyboard_report(payload)
                    else:
                        result = await self._transport.send_consumer_usage_le16(payload)

                    if result != "busy":
                        return
                except Exception as exc:
                    logger.warning("Serial send failed (%s attempt %d): %r", kind, attempt + 1, exc)

                if attempt + 1 < self._max_retries:
                    await asyncio.sleep(self._retry_delay_s)
