"""Application entry point wiring Bluetooth HID Dongle, Unifying USB Dongle, Samsung TV and Audio Pro Speaker."""

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

from typing import Any
from .config import Config
from .dispatcher import Dispatcher
from .http_server import HttpServer
from .runtime import RuntimeEngine

from .unifying_reader import UnifyingReader
from .ble_dongle import BleDongleLink
from .samsung_tv import TvController, ssdp_listener, start_discovery_tasks, stop_discovery_tasks
from .audiopro_speaker import AudioProSpeaker
from .samsung_soundbar_local import SamsungSoundbarLocal
from .speaker import SpeakerLike
from .settings import SettingsStore
from .history import HistoryStore


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

    settings = SettingsStore()
    settings.load()

    history = HistoryStore()
    history.load()

    cleanup_hooks = []

    tv: TvController | None = None
    tv_discovery_tasks: list[asyncio.Task] = []

    if cfg.tv_enabled and cfg.tv_ip and cfg.tv_mac:
        tv = TvController(
            tv_ip=cfg.tv_ip,
            tv_mac=cfg.tv_mac,
            token_file=cfg.tv_token_file,
            name=cfg.tv_name,
        )
        await tv.start()

        shutdown_event = asyncio.Event()

        def _monitor_tv_task(task: asyncio.Task) -> None:
            if shutdown_event.is_set() or tv is None:
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

        # One-shot active presence reconcile for startup.
        # Run in the background so TV bootstrap does not block overall app startup.
        tv_reconcile_task = asyncio.create_task(
            tv.reconcile_presence(),
            name="tv:reconcile_startup",
        )

        def _monitor_tv_reconcile(task: asyncio.Task) -> None:
            if shutdown_event.is_set():
                return
            try:
                task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("tv:reconcile_startup crashed")

        tv_reconcile_task.add_done_callback(_monitor_tv_reconcile)

        async def _stop_tv_reconcile() -> None:
            if not tv_reconcile_task.done():
                tv_reconcile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await tv_reconcile_task

        async def _stop_tv_discovery() -> None:
            await stop_discovery_tasks(tv_discovery_tasks)

        cleanup_hooks.append(("tv_reconcile", _stop_tv_reconcile))
        cleanup_hooks.append(("tv_discovery", _stop_tv_discovery))
        cleanup_hooks.append(("tv", tv.stop))
    else:
        shutdown_event = asyncio.Event()

    speaker: SpeakerLike | None = None

    if cfg.speaker_enabled:
        if cfg.speaker_backend == "audiopro":
            if cfg.speaker_ip:
                speaker = AudioProSpeaker(
                    speaker_ip=cfg.speaker_ip,
                )
        elif cfg.speaker_backend == "samsung_soundbar_local":
            if cfg.speaker_ip:
                speaker = SamsungSoundbarLocal(
                    speaker_ip=cfg.speaker_ip,
                    tv=tv,
                )
        else:
            raise ValueError(f"unsupported SPEAKER_BACKEND={cfg.speaker_backend!r}")

    if speaker is not None:
        await speaker.start()
        cleanup_hooks.append(("speaker", speaker.stop))

    ble = BleDongleLink(
        serial_port=cfg.ble_serial_device,
        baud=cfg.ble_serial_baud,
    )

    runtime = RuntimeEngine(
        tv=tv,
        speaker=speaker,
        ble=ble,
        settings=settings,
        history=history,
        initial_mode="power_off",
    )

    async def _on_domain_state_change(name: str, payload: dict[str, Any]) -> None:
        result = await runtime.on_device_state_change(name, payload)
        if not result.get("ok") and result.get("reason"):
            logger.debug(
                "device state change deferred name=%s reason=%s payload=%s",
                name,
                result.get("reason"),
                payload,
            )

    if tv is not None:
        tv._state_change_callback = _on_domain_state_change

    if speaker is not None:
        speaker._state_change_callback = _on_domain_state_change

    dispatcher = Dispatcher(
        cfg=cfg,
        ble=ble,
        tv=tv,
        speaker=speaker,
        settings=settings,
        run_flow=runtime.run_flow,
    )

    runtime.attach_dispatcher(dispatcher)

    await runtime.start()

    reader = UnifyingReader(
        scancode_map=dispatcher.scancode_map,
        on_edge=dispatcher.on_usb_edge,
        on_disconnect=dispatcher.on_usb_disconnect,
    )

    http_server = HttpServer(
        host=cfg.http_server_host,
        port=cfg.http_server_port,
        ble=ble,
        reader=reader,
        tv=tv,
        speaker=speaker,
        settings=settings,
        runtime=runtime,
        history=history,
        speaker_backend=cfg.speaker_backend,
        dispatcher=dispatcher,
    )

    try:
        await ble.start()
        cleanup_hooks.append(("ble", ble.stop))

        await reader.start()
        cleanup_hooks.append(("reader", reader.stop))

        await http_server.start()
        cleanup_hooks.append(("http_server", http_server.stop))

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)

        await shutdown_event.wait()

    finally:
        shutdown_event.set()

        for _name, stopper in reversed(cleanup_hooks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stopper()


if __name__ == "__main__":
    asyncio.run(main())
