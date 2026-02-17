"""Application entry point wiring BLE, Home Assistant and USB input."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

try:
    import uvloop as _uvloop  # type: ignore
    _uvloop.install()
except Exception:
    pass

from .config import Config
from .ha_ws import HAWS
from .dispatcher import Dispatcher
from .input_unifying import UnifyingReader
from .bt_le.controller import BTLEController
from .macros import MACROS
from .health import HealthServer
from .validation import DEFAULT_MS_WHITELIST, parse_ms_whitelist


def _debug_enabled() -> bool:
    value = os.getenv("DEBUG", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


logging.basicConfig(
    level=logging.DEBUG if _debug_enabled() else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


def _make_on_cmd(bt: BTLEController):
    async def _on_cmd(data: dict) -> None:
        """
        Accept exactly two message shapes (HA → Pi):

          1) Single BLE key (tap):
             {
               "text": "ble_key",
               "usage": "keyboard" | "consumer",
               "code": "<symbolic_code>",
               "hold_ms": 40               # optional, default 40ms (whitelist)
             }

          2) Macro by name (timed sequence, local to Pi):
             {
               "text": "macro",
               "name": "<macro_name>",     # must exist in MACROS
               "tap_ms": 40,               # optional per-key hold, default 40ms (whitelist)
               "inter_delay_ms": 400       # optional gap, default 400ms (whitelist)
             }
        """
        text = (data or {}).get("text")

        if text == "ble_key":
            usage = data.get("usage")
            code = data.get("code")
            hold_ms = parse_ms_whitelist(data.get("hold_ms"), default=40, context="cmd.hold_ms")
            if isinstance(usage, str) and isinstance(code, str) and hold_ms is not None:
                # single-shot via HIDClient (macros use run_macro below)
                await bt.send_key(usage=usage, code=code, hold_ms=hold_ms)
            return

        if text == "macro":
            name = str(data.get("name") or "")
            steps = MACROS.get(name, [])
            if steps:
                tap = parse_ms_whitelist(data.get("tap_ms"), default=40, context="cmd.tap_ms")
                inter = parse_ms_whitelist(
                    data.get("inter_delay_ms"),
                    allowed=(*DEFAULT_MS_WHITELIST, 400),
                    default=400,
                    context="cmd.inter_delay_ms",
                )
                await bt.run_macro(steps, default_hold_ms=tap, inter_delay_ms=inter)
            return

        # Unknown command -> drop silently by design
        return

    return _on_cmd


async def main() -> None:
    """Run the PiHub control loop until interrupted."""
    cfg = Config.load()
    try:
        token = cfg.load_token()
    except RuntimeError as exc:
        logger.error("[app] Cannot start without Home Assistant token: %s", exc)
        raise SystemExit(1) from exc

    bt = BTLEController(
        adapter=cfg.ble_adapter,
        name=cfg.ble_device_name,
        serial_port=cfg.ble_serial_device,
        baud=cfg.ble_serial_baud,
    )

    async def _on_activity(activity: str | None) -> None:
        await DispatcherRef.on_activity(activity)  # set below

    _on_cmd = _make_on_cmd(bt)

    ws = HAWS(
        url=cfg.ha_ws_url,
        token=token,
        activity_entity=cfg.ha_activity,
        event_name=cfg.ha_cmd_event,
        on_activity=_on_activity,
        on_cmd=_on_cmd,
    )

    async def _send_cmd(text: str, **extra) -> bool:
        return await ws.send_cmd(text, **extra)

    DispatcherRef = Dispatcher(cfg=cfg, send_cmd=_send_cmd, bt_le=bt)

    reader = UnifyingReader(
        scancode_map=DispatcherRef.scancode_map,
        on_edge=DispatcherRef.on_usb_edge,
        on_disconnect=DispatcherRef.on_usb_disconnect,
    )

    health = HealthServer(
        host=cfg.health_host,
        port=cfg.health_port,
        ws=ws,
        bt=bt,
        reader=reader,
    )

    logger.info(
        "[app] ws=%s event=%s activity=%s",
        cfg.ha_ws_url,
        cfg.ha_cmd_event,
        cfg.ha_activity,
    )

    stop = asyncio.Event()

    def _monitor_ws(task: asyncio.Task) -> None:
        if stop.is_set():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("[app] Home Assistant task crashed")
        else:
            logger.warning("[app] Home Assistant task exited unexpectedly")
        stop.set()

    ws_task = asyncio.create_task(ws.start(), name="ha_ws")
    ws_task.add_done_callback(_monitor_ws)

    # Track started subsystems so we can always shut down cleanly (even if startup fails).
    # Stop order should generally be reverse of start order.
    started = []  # list[tuple[str, callable]]

    try:
        await bt.start()
        started.append(("bt", bt.stop))

        if not await bt.wait_ready(timeout=20.0):
            logger.warning(
                "[app] nrf BLE dongle not ready yet (serial_open=%s); will enable HID automatically when READY arrives",
                bt.status.get("adapter_present", False),
            )

        await reader.start()
        started.append(("reader", reader.stop))

        await health.start()
        started.append(("health", health.stop))

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)

        await stop.wait()

    finally:
        # Ensure we signal the ws monitor not to re-trigger shutdown while we're already stopping.
        stop.set()

        # Stop subsystems in reverse start order.
        for _name, stopper in reversed(started):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stopper()

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ws.stop()

        # Always await the websocket task so exceptions get surfaced in logs,
        # but don't let it hang shutdown.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            if not ws_task.done():
                ws_task.cancel()
            await ws_task



if __name__ == "__main__":
    asyncio.run(main())
