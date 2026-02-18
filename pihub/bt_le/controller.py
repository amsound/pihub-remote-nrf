import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .ble_serial import BleSerial, DongleState, discover_cmd_ports
from .hid_client import HIDClient

log = logging.getLogger(__name__)


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

        log.info("ble connection initialised port=%s baud=%s, name=%s", ports, baud, name)

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
        interval_x100 = params.get("interval_ms_x100")
        if isinstance(interval_x100, int):
            params["interval_ms"] = interval_x100 / 100.0
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
        log.debug("evt=%s status=%s", event, self.status)

    def _link_ready(self) -> bool:
        return bool(self._serial.serial_ready and self._serial.state.connected)

    # HIDClient transport hooks

    def notify_keyboard(self, report: bytes) -> None:
        if not self._link_ready():
            return
        asyncio.create_task(self._serial.send_kb(report))

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        if not self._link_ready():
            return
        usage = usage_id if pressed else 0
        asyncio.create_task(self._serial.send_cc_usage(usage))

    # Dispatcher-facing

    async def send_key(self, *, usage: str, code: str, hold_ms: int = 40) -> None:
        await self._hid_client.send_key(usage=usage, code=code, hold_ms=hold_ms)

    async def run_macro(self, steps: list[dict], *, default_hold_ms: int = 40, inter_delay_ms: int = 400) -> None:
        await self._hid_client.run_macro(steps, default_hold_ms=default_hold_ms, inter_delay_ms=inter_delay_ms)