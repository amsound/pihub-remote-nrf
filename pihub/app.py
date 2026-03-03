"""Application entry point wiring BLE, Home Assistant and USB input."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import socket

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
from .tv.dmr import dmr_up


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

async def _tv_ssdp_listener(tv):
    MCAST_GRP = "239.255.255.250"
    MCAST_PORT = 1900

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MCAST_PORT))
    mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)

    loop = asyncio.get_running_loop()

    while True:
        data, addr = await asyncio.to_thread(sock.recvfrom, 65535)
        src_ip = addr[0]
        if src_ip != tv.tv_ip:
            continue

        txt = data.decode("utf-8", errors="ignore")
        if "NOTIFY * HTTP/1.1" not in txt:
            continue

        hdr = {}
        for line in txt.split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                hdr[k.strip().upper()] = v.strip()

        nts = hdr.get("NTS", "")
        nt = hdr.get("NT", "")
        usn = hdr.get("USN", "")
        loc = hdr.get("LOCATION")

        acted = tv.notify_ssdp(nts=nts, nt=nt, usn=usn, location=loc)
        if not acted:
            continue

        # Your requirement: on positive SSDP (alive), attempt WS connect immediately
        if nts == "ssdp:alive":
            try:
                await tv.ensure_ws_connected()
            except Exception:
                pass

async def _tv_dmr_fallback_poller(tv, *, interval_s: float = 60.0):
    while True:
        await asyncio.sleep(interval_s)
        if not tv._session:
            continue
        try:
            sample = await dmr_up(tv._session, tv.tv_ip)  # True/False
        except Exception:
            continue

        cached = tv._dmr_cached
        if cached is None:
            tv.set_power_state(bool(sample), reason="fallback_init")
            if sample:
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    pass
            continue

        # If mismatch, reconcile logical power state.
        if bool(sample) != bool(cached):
            tv.set_power_state(bool(sample), reason="fallback_reconcile")
            if sample:
                # if TV is actually on, try to get WS back quickly
                try:
                    await tv.ensure_ws_connected()
                except Exception:
                    pass


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
    tv_task: asyncio.Task | None = None

    if cfg.tv_ip and cfg.tv_mac:
        tv = TvController(
            tv_ip=cfg.tv_ip,
            tv_mac=cfg.tv_mac,
            token_file=cfg.tv_token_file,
            name=cfg.tv_name,
        )
        await tv.start()

        asyncio.create_task(_tv_ssdp_listener(tv), name="tv:ssdp")
        asyncio.create_task(_tv_dmr_fallback_poller(tv, interval_s=60.0), name="tv:dmr_fallback")

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

        if tv_task and not tv_task.done():
            tv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tv_task



if __name__ == "__main__":
    asyncio.run(main())
