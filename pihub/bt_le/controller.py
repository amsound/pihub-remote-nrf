import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from .ble_serial import BleSerial, DongleState
from .hid_client import HIDClient


log = logging.getLogger(__name__)


@dataclass
class BTLEState:
    ready: bool = False
    advertising: bool = False
    connected: bool = False
    proto_boot: bool = False
    error: bool = False
    conn_params: Optional[dict] = None
    phy: Optional[dict] = None
    last_disc_reason: Optional[int] = None


class BTLEController:
    """
    Serial-backed BTLE controller.

    Public API expectations (used by dispatcher/input pipeline):
      - start/stop (async)
      - key_down(key: str), key_up(key: str), send_key(key: str) (sync wrappers)
      - consumer_down(usage: int), consumer_up(usage: int) (sync wrappers)

    NOTE: key_* methods intentionally never raise if dongle/link isn't ready.
    """

    def __init__(
        self,
        serial_port: str = "/dev/ttyACM0",
        baud: int = 115200,
        name: str = "PiHub Remote",
        *,
        adapter: Optional[str] = None,
        fallback_ports: Optional[Sequence[str]] = None,
        **_ignored: object,
    ):
        self._serial_port = serial_port
        self._adapter = adapter  # ignored for serial transport
        self._baud = baud
        self._name = name

        ports = [serial_port]
        if fallback_ports:
            for p in fallback_ports:
                if p not in ports:
                    ports.append(p)

        self._state = BTLEState()

        self._serial = BleSerial(
            ports=ports,
            baud=baud,
            on_event=self._on_dongle_event,
        )

        # HIDClient encodes keys -> keyboard/consumer reports and calls our notify_*.
        self._hid_client = HIDClient(hid=self)

        self._started = False
        self._ready_evt = asyncio.Event()

        log.info("BTLEController ready (serial=%s baud=%s, name=%s)", serial_port, baud, name)

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
        """Wait for the dongle command channel to be ready (EVT READY 1).

        Returns True if ready within timeout, else False.
        """
        # Fast-path if already ready.
        if getattr(self._state, 'ready', False):
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
        """Return a JSON-serializable BLE status snapshot.

        The health endpoint relies on adapter/advertising/connected while
        keeping additional low-cost details available for diagnostics.
        """

        return {
            "adapter_present": self._serial.is_open,
            "ready": self._state.ready,
            "advertising": self._state.advertising,
            "connected": self._state.connected,
            "proto_boot": self._state.proto_boot,
            "error": self._state.error,
            "conn_params": dict(self._state.conn_params) if self._state.conn_params else None,
            "phy": dict(self._state.phy) if self._state.phy else None,
            "last_disc_reason": self._state.last_disc_reason,
        }

    def _on_dongle_event(self, event: str, st: DongleState) -> None:
        self._state.ready = st.ready
        if st.ready:
            self._ready_evt.set()
        else:
            self._ready_evt.clear()
        self._state.advertising = st.advertising
        self._state.connected = st.connected
        self._state.proto_boot = st.proto_boot
        self._state.error = st.error
        self._state.conn_params = st.conn_params
        self._state.phy = st.phy
        self._state.last_disc_reason = st.last_disc_reason

        # Friendly, low-volume logs.
        if event == "ready":
            log.info("BTLE dongle READY=%s", 1 if st.ready else 0)
        elif event == "conn":
            log.info("BTLE connected=%s", 1 if st.connected else 0)
        elif event == "adv":
            log.debug("BTLE advertising=%s", 1 if st.advertising else 0)
        elif event == "conn_params":
            if st.conn_params:
                log.info(
                    "BTLE conn params: %s",
                    " ".join(f"{k}={v}" for k, v in st.conn_params.items()),
                )
        elif event == "phy":
            if st.phy:
                log.info("BTLE phy: tx=%s rx=%s", st.phy.get("tx"), st.phy.get("rx"))
        elif event == "disc":
            log.info("BTLE disconnected reason=%s", st.last_disc_reason)
        elif event == "err":
            if st.error:
                log.warning("BTLE dongle ERR=1")
        elif event == "link_lost":
            log.warning("BTLE serial link lost; reconnecting")

    # ---- HIDClient transport hooks ----

    def notify_keyboard(self, report: bytes) -> None:
        # Drop silently if link isn't up.
        if not self._serial.state.connected:
            return
        asyncio.create_task(self._serial.send_kb(report))

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        if not self._serial.state.connected:
            return
        usage = usage_id if pressed else 0
        asyncio.create_task(self._serial.send_cc_usage(usage))

    # ---- Dispatcher-facing convenience methods (sync) ----

    def key_down(self, *, usage: str, code: str) -> None:
        try:
            self._hid_client.key_down(usage=usage, code=code)
        except Exception:
            # Never crash input loop.
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
