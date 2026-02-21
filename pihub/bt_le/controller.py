"""BLE controller orchestrating serial transport and HID key delivery."""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .ble_serial import BleSerial, DongleState, discover_cmd_ports
from .hid_client import HIDClient
from .compiled_frames import CompiledBleFrames

logger = logging.getLogger(__name__)


@dataclass
class BTLEState:
    ready: bool = False
    advertising: bool = False
    connected: bool = False
    proto_boot: bool = False
    error: bool = False
    sec: int = 0
    suspend: bool = False
    notify: Optional[dict] = None
    conn_params: Optional[dict] = None
    phy: Optional[dict] = None
    last_disc_reason: Optional[int] = None


class BTLEController:
    def __init__(
        self,
        serial_port: str = "auto",
        baud: int = 115200,
        *,
        status_poll_s: float = 30.0,
        **_ignored: object,
    ):
        self._serial_port = serial_port
        self._baud = baud

        env_port = os.getenv("BLE_SERIAL_DEVICE", "").strip()
        requested = (env_port or (serial_port or "")).strip()

        if requested and requested.lower() != "auto":
            ports = [requested]
        else:
            ports = discover_cmd_ports() or ["/dev/ttyACM0"]

        self._state = BTLEState()
        self._serial = BleSerial(
            ports=ports,
            baud=baud,
            status_poll_s=status_poll_s,
            on_event=self._on_dongle_event,
        )

        self._hid_client = HIDClient(hid=self)

        self._started = False
        self._ready_evt = asyncio.Event()

        logger.info("initialised port=%s baud=%s", ports[0] if ports else None, baud)

    # Dispatcher-facing

    def key_down(self, *, usage: str, code: str) -> None:
        try:
            self._hid_client.key_down(usage=usage, code=code)
        except Exception:
            logger.debug("key_down(usage=%s, code=%s) failed", usage, code, exc_info=True)

    def key_up(self, *, usage: str, code: str) -> None:
        try:
            self._hid_client.key_up(usage=usage, code=code)
        except Exception:
            logger.debug("key_up(usage=%s, code=%s) failed", usage, code, exc_info=True)

    async def send_key(self, *, usage: str, code: str, hold_ms: int = 40) -> None:
        try:
            await self._hid_client.send_key(usage=usage, code=code, hold_ms=hold_ms)
        except Exception:
            logger.debug(
                "send_key(usage=%s, code=%s, hold_ms=%s) failed",
                usage,
                code,
                hold_ms,
                exc_info=True,
            )

    async def run_macro(
        self,
        steps: list[dict],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        try:
            await self._hid_client.run_macro(
                steps,
                default_hold_ms=default_hold_ms,
                inter_delay_ms=inter_delay_ms,
            )
        except Exception:
            logger.debug(
                "run_macro(default_hold_ms=%s, inter_delay_ms=%s) failed",
                default_hold_ms,
                inter_delay_ms,
                exc_info=True,
            )

    def consumer_down(self, usage_id: int) -> None:
        try:
            self._hid_client.consumer_down(usage_id)
        except Exception:
            logger.debug("consumer_down(%s) failed", usage_id, exc_info=True)

    def consumer_up(self, usage_id: int) -> None:
        try:
            self._hid_client.consumer_up(usage_id)
        except Exception:
            logger.debug("consumer_up(%s) failed", usage_id, exc_info=True)

    def compile_ble_frames(self, *, usage: str, code: str) -> CompiledBleFrames | None:
        # usage is a str in dispatcher; HIDClient expects Usage but it’s compatible.
        return self._hid_client.compile_ble_frames(usage=usage, code=code)

    def compiled_key_down(self, frames: CompiledBleFrames) -> None:
        self._serial.enqueue_bin_frame(frames.down)

    def compiled_key_up(self, frames: CompiledBleFrames) -> None:
        self._serial.enqueue_bin_frame(frames.up)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._serial.start()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self._serial.stop()

    async def unpair(self) -> None:
        # keep it human-readable on the wire
        await self._serial.unpair()

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        if self._state.ready:
            return True
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def state(self) -> BTLEState:
        return self._state

    @property
    def status(self) -> dict:
        return {
            "adapter_present": self._serial.is_open,
            "active_port": self._serial.active_port,
            "ready": self._state.ready,
            "advertising": self._state.advertising,
            "connected": self._state.connected,
            "proto_boot": self._state.proto_boot,
            "sec": self._state.sec,
            "suspend": self._state.suspend,
            "notify": dict(self._state.notify) if self._state.notify else None,
            "error": self._state.error,
            "conn_params": self._conn_params_for_status(),
            "phy": dict(self._state.phy) if self._state.phy else None,
            "last_disc_reason": self._state.last_disc_reason,
        }

    def _conn_params_for_status(self) -> Optional[dict]:
        if not self._state.conn_params:
            return None
        params = dict(self._state.conn_params)
        # Normalize interval_ms and hide the internal x100 form from health/status.
        interval_ms = params.get("interval_ms")
        if interval_ms is not None:
            # EVT CONN_PARAMS provides interval_ms as a string like "15.00"
            try:
                params["interval_ms"] = float(interval_ms)
            except Exception:
                pass
        else:
            interval_x100 = params.get("interval_ms_x100")
            if isinstance(interval_x100, int):
                params["interval_ms"] = interval_x100 / 100.0

        # Don't leak the internal representation
        params.pop("interval_ms_x100", None)
        return params

    def _on_dongle_event(self, event: str, st: DongleState) -> None:
        self._state.ready = st.ready
        self._state.advertising = st.advertising
        self._state.connected = st.connected
        self._state.proto_boot = st.proto_boot
        self._state.error = st.error
        self._state.conn_params = st.conn_params
        self._state.phy = st.phy
        self._state.last_disc_reason = st.last_disc_reason
        self._state.sec = getattr(st, "sec", 0)
        self._state.suspend = getattr(st, "suspend", False)
        self._state.notify = getattr(st, "notify", None)

        if st.ready:
            self._ready_evt.set()
        else:
            self._ready_evt.clear()

        # Controller keeps debug-level details; ble_serial now logs the human-friendly INFO lines.
        logger.debug("evt=%s status=%s", event, self.status)

    def _link_ready(self) -> bool:
        return bool(self._serial.serial_ready and self._serial.state.connected)

    # HIDClient transport hooks

    def notify_keyboard(self, report: bytes) -> None:
        if not self._link_ready():
            return
        if len(report) != 8:
            return
        self._serial.enqueue_bin_frame(b"\x01" + report)

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        if not self._link_ready():
            return
        usage = (usage_id & 0xFFFF) if pressed else 0
        le = bytes((usage & 0xFF, (usage >> 8) & 0xFF))
        self._serial.enqueue_bin_frame(b"\x02" + le)