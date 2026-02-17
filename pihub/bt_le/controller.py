import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional, Sequence

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
    """
    Serial-backed BTLE controller.

    NOTE: key_* methods intentionally never raise if dongle/link isn't ready.
    """

    def __init__(
        self,
        serial_port: str = "auto",
        baud: int = 115200,
        name: str = "PiHub Remote",
        *,
        adapter: Optional[str] = None,
        fallback_ports: Optional[Sequence[str]] = None,
        status_poll_s: float = 30.0,  # 0 disables slow periodic STATUS polling
        **_ignored: object,
    ):
        self._serial_port = serial_port
        self._adapter = adapter  # ignored for serial transport
        self._baud = baud
        self._name = name

        ports = []
        env_port = os.getenv("BLE_SERIAL_DEVICE", "").strip()
        requested_port = env_port or (serial_port or "").strip()
        if requested_port and requested_port.lower() != "auto":
            ports.append(requested_port)
        else:
            ports.extend(discover_cmd_ports())
        if fallback_ports:
            for p in fallback_ports:
                if p not in ports:
                    ports.append(p)

        if not ports:
            # Last-resort scan order if no devices are currently visible.
            ports.extend(["/dev/ttyACM1", "/dev/ttyACM0"])

        self._state = BTLEState()

        self._serial = BleSerial(
            ports=ports,
            baud=baud,
            status_poll_s=status_poll_s,
            on_event=self._on_dongle_event,
        )

        # HIDClient encodes keys -> keyboard/consumer reports and calls our notify_*.
        self._hid_client = HIDClient(hid=self)

        self._started = False
        self._ready_evt = asyncio.Event()

        log.info("BTLEController initialized (ports=%s baud=%s, name=%s)", ports, baud, name)

    # ---- lifecycle ----

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._serial.start()
        # Don't block startup if dongle isn't enumerated yet; connect loop runs in background.

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self._serial.stop()

    # ---- state ----

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        """Wait for the dongle command channel to be ready (EVT READY 1)."""
        if getattr(self._state, "ready", False):
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
        """Return a JSON-serializable BLE status snapshot."""
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

    def _fmt_short(self) -> str:
        cp = self._conn_params_for_status() or {}
        interval_ms = cp.get("interval_ms")
        latency = cp.get("latency")
        timeout_ms = cp.get("timeout_ms")
        phy = self._state.phy or {}
        return (
            f"ready={int(self._state.ready)} adv={int(self._state.advertising)} conn={int(self._state.connected)} "
            f"sec={self._state.sec} proto_boot={int(self._state.proto_boot)} err={int(self._state.error)} "
            f"interval_ms={interval_ms} latency={latency} timeout_ms={timeout_ms} phy={phy} disc={self._state.last_disc_reason}"
        )

    def _on_dongle_event(self, event: str, st: DongleState) -> None:
        # Update state from dongle snapshot
        prev_ready = self._state.ready
        prev_conn = self._state.connected

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

        # Logging policy:
        # - INFO: significant transitions (ready, connect, disconnect, link lost)
        # - DEBUG: everything else, including STATUS snapshots
        if event == "ready" and prev_ready != st.ready:
            log.info("BTLE READY=%s (%s)", 1 if st.ready else 0, self._fmt_short())
        elif event == "conn" and prev_conn != st.connected:
            log.info("BTLE connected=%s (%s)", 1 if st.connected else 0, self._fmt_short())
        elif event == "disc":
            log.info("BTLE disconnected reason=%s (%s)", st.last_disc_reason, self._fmt_short())
        elif event == "link_lost":
            log.warning("BTLE serial link lost; reconnecting")
        elif event == "err" and st.error:
            log.warning("BTLE dongle ERR=1 (%s)", self._fmt_short())
        elif event == "status":
            log.debug("BTLE STATUS (%s)", self._fmt_short())
        else:
            log.debug("BTLE evt=%s (%s)", event, self._fmt_short())

    def _link_ready(self) -> bool:
        return bool(self._serial.serial_ready and self._serial.state.connected)

    # ---- HIDClient transport hooks ----

    def notify_keyboard(self, report: bytes) -> None:
        # Drop silently if link isn't up.
        if not self._link_ready():
            return
        asyncio.create_task(self._serial.send_kb(report))

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        if not self._link_ready():
            return
        usage = usage_id if pressed else 0
        asyncio.create_task(self._serial.send_cc_usage(usage))

    # ---- Dispatcher-facing convenience methods (sync) ----

    def key_down(self, *, usage: str, code: str) -> None:
        try:
            self._hid_client.key_down(usage=usage, code=code)
        except Exception:
            log.debug("key_down(usage=%s, code=%s) failed", usage, code, exc_info=True)

    def key_up(self, *, usage: str, code: str) -> None:
        try:
            self._hid_client.key_up(usage=usage, code=code)
        except Exception:
            log.debug("key_up(usage=%s, code=%s) failed", usage, code, exc_info=True)

    async def send_key(self, *, usage: str, code: str, hold_ms: int = 40) -> None:
        try:
            await self._hid_client.send_key(usage=usage, code=code, hold_ms=hold_ms)
        except Exception:
            log.debug(
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
            log.debug(
                "run_macro(default_hold_ms=%s, inter_delay_ms=%s) failed",
                default_hold_ms,
                inter_delay_ms,
                exc_info=True,
            )

    def consumer_down(self, usage_id: int) -> None:
        try:
            self._hid_client.consumer_down(usage_id)
        except Exception:
            log.debug("consumer_down(%s) failed", usage_id, exc_info=True)

    def consumer_up(self, usage_id: int) -> None:
        try:
            self._hid_client.consumer_up(usage_id)
        except Exception:
            log.debug("consumer_up(%s) failed", usage_id, exc_info=True)
