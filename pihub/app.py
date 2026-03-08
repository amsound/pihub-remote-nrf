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
from .dispatcher import Dispatcher
from .input_unifying import UnifyingReader
from .input_ble_dongle import BleDongleLink
from .health import HealthServer
from .tv import TvController
from .speaker_linkplay_tcp import LinkPlaySpeaker
from .tv.ssdp import ssdp_listener, start_discovery_tasks, stop_discovery_tasks
from .runtime import RuntimeEngine
from .overrides import OverrideEngine


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

    started = []

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

        stop = asyncio.Event()

        def _monitor_tv_task(task: asyncio.Task) -> None:
            if stop.is_set() or tv is None:
                return

            name = task.get_name()

            try:
                task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("%s crashed", name)
            else:
                if name != "tv:ssdp":
                    return
                logger.warning("%s exited unexpectedly", name)

            if name != "tv:ssdp":
                return

            replacement = asyncio.create_task(ssdp_listener(tv), name="tv:ssdp")
            replacement.add_done_callback(_monitor_tv_task)

            for idx, existing in enumerate(tv_discovery_tasks):
                if existing is task:
                    tv_discovery_tasks[idx] = replacement
                    break
            else:
                tv_discovery_tasks.append(replacement)
            logger.warning("restarted tv:ssdp task")

        tv_discovery_tasks = start_discovery_tasks(tv)
        for task in tv_discovery_tasks:
            task.add_done_callback(_monitor_tv_task)

        async def _stop_tv_discovery() -> None:
            await stop_discovery_tasks(tv_discovery_tasks)

        started.append(("tv_discovery", _stop_tv_discovery))
        started.append(("tv", tv.stop))
    else:
        stop = asyncio.Event()

    speaker: LinkPlaySpeaker | None = None
    if cfg.speaker_host:
        speaker = LinkPlaySpeaker(
            host=cfg.speaker_host,
            http_scheme=cfg.speaker_http_scheme,
            volume_step_pct=cfg.speaker_volume_step_pct,
        )
        await speaker.start()
        started.append(("speaker", speaker.stop))

    bt = BleDongleLink(
        serial_port=cfg.ble_serial_device,
        baud=cfg.ble_serial_baud,
    )

    runtime = RuntimeEngine(
        tv=tv,
        speaker=speaker,
        ble=bt,
        initial_mode="power_off",
    )

    DispatcherRef = Dispatcher(
        cfg=cfg,
        bt_le=bt,
        tv=tv,
        speaker=speaker,
        run_flow=runtime.run_flow,
    )

    runtime.attach_dispatcher(DispatcherRef)

    overrides = OverrideEngine(
    runtime=runtime,
    tv=tv,
    speaker=speaker,
    apply_mode=cfg.override_apply_mode,
    )

    await runtime.start()

    reader = UnifyingReader(
        scancode_map=DispatcherRef.scancode_map,
        on_edge=DispatcherRef.on_usb_edge,
        on_disconnect=DispatcherRef.on_usb_disconnect,
    )

    health = HealthServer(
        host=cfg.health_host,
        port=cfg.health_port,
        bt=bt,
        reader=reader,
        tv=tv,
        speaker=speaker,
        runtime=runtime,
    )

    try:
        await bt.start()
        started.append(("bt", bt.stop))

        await reader.start()
        started.append(("reader", reader.stop))

        await health.start()
        started.append(("health", health.stop))

        await overrides.start()
        started.append(("overrides", overrides.stop))

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)

        await stop.wait()

    finally:
        stop.set()

        for _name, stopper in reversed(started):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stopper()


if __name__ == "__main__":
    asyncio.run(main())
