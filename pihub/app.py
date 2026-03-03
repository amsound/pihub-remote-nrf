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
from .input_ble_dongle import BleDongleLink
from .health import HealthServer
from .tv import TvController
from .speaker_linkplay import LinkPlaySpeaker
from .tv.ssdp import start_discovery_tasks, stop_discovery_tasks


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

async def main() -> None:
    """Run the PiHub control loop until interrupted."""
    cfg = Config.load()
    try:
        token = cfg.load_token()
    except RuntimeError as exc:
        logger.error("cannot start without Home Assistant token: %s", exc)
        raise SystemExit(1) from exc

    # Track started subsystems so we can always shut down cleanly (even if startup fails).
    # Stop order should generally be reverse of start order.
    started = []  # list[tuple[str, callable]]

    # --- TV (create before wiring callbacks that reference it) ---
    tv: TvController | None = None
    tv_discovery_tasks: list[asyncio.Task] = []

    if cfg.tv_ip and cfg.tv_mac:
        tv = TvController(
            tv_ip=cfg.tv_ip,
            tv_mac=cfg.tv_mac,
            token_file=cfg.tv_token_file,
            name=cfg.tv_name,
        )
        await tv.start()

        tv_discovery_tasks = start_discovery_tasks(tv, interval_s=60.0)

        async def _stop_tv_discovery() -> None:
            await stop_discovery_tasks(tv_discovery_tasks)

        started.append(("tv_discovery", _stop_tv_discovery))
        started.append(("tv", tv.stop))

    # --- Speaker (LinkPlay/WiiM) ---
    speaker: LinkPlaySpeaker | None = None
    if cfg.speaker_host:
        speaker = LinkPlaySpeaker(
            host=cfg.speaker_host,
            http_scheme=cfg.speaker_http_scheme,   # default https
            volume_step_pct=cfg.speaker_volume_step_pct,
        )
        await speaker.start()
        started.append(("speaker", speaker.stop))

    # --- BLE ---
    bt = BleDongleLink(
        serial_port=cfg.ble_serial_device,
        baud=cfg.ble_serial_baud,
    )

    # Create websocket first (we'll attach it to dispatcher after)
    ws: HAWS | None = None

    async def _send_cmd(action: str, **args) -> bool:
        # Dispatcher uses this to send HA bus commands (domain="ha")
        assert ws is not None
        return await ws.send_cmd(action, **args)

    # Create Dispatcher BEFORE HAWS so HAWS can call Dispatcher.on_cmd directly.
    DispatcherRef = Dispatcher(cfg=cfg, send_cmd=_send_cmd, bt_le=bt, tv=tv, speaker=speaker)

    async def _on_activity(activity: str | None) -> None:
        await DispatcherRef.on_activity(activity)

    # HA WebSocket: breaking schema; all incoming commands go to Dispatcher.on_cmd
    ws = HAWS(
        url=cfg.ha_ws_url,
        token=token,
        activity_entity=cfg.ha_activity,
        event_name=cfg.ha_cmd_event,
        on_activity=_on_activity,
        on_cmd=DispatcherRef.on_cmd,
    )

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
        tv=tv,
        speaker=speaker,
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
            logger.exception("ws task crashed")
        else:
            logger.warning("ws task exited unexpectedly")
        stop.set()

    ws_task = asyncio.create_task(ws.start(), name="ha_ws")
    ws_task.add_done_callback(_monitor_ws)

    try:
        await bt.start()
        started.append(("bt", bt.stop))

        await reader.start()
        started.append(("reader", reader.stop))

        await health.start()
        started.append(("health", health.stop))

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)

        await stop.wait()

    finally:
        stop.set()

        # Stop subsystems in reverse start order.
        for _name, stopper in reversed(started):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stopper()

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ws.stop()

        with contextlib.suppress(asyncio.CancelledError, Exception):
            if not ws_task.done():
                ws_task.cancel()
            await ws_task


if __name__ == "__main__":
    asyncio.run(main())
