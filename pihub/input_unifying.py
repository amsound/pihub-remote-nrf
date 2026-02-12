"""Handle Logitech Unifying keyboards with automatic reconnection."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import random
import time
from typing import Awaitable, Callable, Dict, Optional

from evdev import InputDevice, ecodes

EdgeCallback = Callable[[str, str], Awaitable[None]] | Callable[[str, str], None]
DisconnectCallback = Callable[[], Awaitable[None]] | Callable[[], None]

logger = logging.getLogger(__name__)

def _jittered(t: float) -> float:
    """±25% jitter, capped to 10s."""
    jittered = t * (0.75 + random.random() * 0.5)
    return min(10.0, jittered)

class UnifyingReader:
    """
    Reads a Logitech Unifying (or generic event-kbd) device and emits logical key edges.

    • scancode_map maps either:
        - "KEY_LEFT" style names → "rem_*"
        - numeric scan codes as strings (e.g. "786924") → "rem_*"
    • Emits only 'down' and 'up' edges (ignores auto-repeat).
    • Survives hot-unplug/replug: reopens device with jittered backoff.
    """

    def __init__(
        self,
        scancode_map: Dict[str, str],
        on_edge: EdgeCallback,
        *,
        edge_queue_maxsize: int = 512,
        on_disconnect: Optional[DisconnectCallback] = None,
    ) -> None:
        self._map = scancode_map
        self._on_edge = on_edge
        self._edge_queue_maxsize = edge_queue_maxsize
        self._on_disconnect = on_disconnect

        self._task: Optional[asyncio.Task] = None
        self._edge_worker: Optional[asyncio.Task] = None
        self._edge_queue: Optional[asyncio.Queue[tuple[str, str] | None]] = None
        self._stop = asyncio.Event()
        self._dropped_edges = 0
        self._last_drop_log = 0.0
        self._last_input_path: Optional[str] = None
        self._last_grabbed = False
        self._input_open = False
        self._receiver_present = False
        self._paired_remote = False
        self._disconnect_notified = False

    # ── Public API ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Begin watching the configured input device."""
        if self._task is None:
            self._stop.clear()
            self._edge_queue = asyncio.Queue(maxsize=self._edge_queue_maxsize)
            self._edge_worker = asyncio.create_task(
                self._drain_edges(), name="unifying_edge_worker"
            )
            self._task = asyncio.create_task(self._run(), name="unifying_reader")

    async def stop(self) -> None:
        """Stop watching the input device and wait for shutdown."""
        if self._task:
            self._stop.set()
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._edge_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._edge_queue.put_nowait(None)
        if self._edge_worker:
            self._edge_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._edge_worker
            self._edge_worker = None
        self._edge_queue = None

    @property
    def device_path(self) -> str:
        # best-effort autodetect for display
        path = _autodetect_or_none()
        return path or "<auto>"

    @property
    def is_running(self) -> bool:
        """Return True while the reader loop is active."""

        t = self._task
        return bool(t and not t.done())

    @property
    def status(self) -> dict:
        """Return a snapshot of the current USB reader status."""

        return {
            "receiver_present": self._receiver_present,
            "paired_remote": self._paired_remote,
            "reader_running": self.is_running,
            "input_open": self._input_open,
            "input_path": self._last_input_path,
            "grabbed": self._last_grabbed,
        }

    # ── Internals ───────────────────────────────────────────────────────────
    def _resolve_device_path(self) -> Optional[str]:
        return _autodetect_or_none()

    def _open_device(self, path: str) -> tuple[InputDevice, bool]:
        dev = InputDevice(path)
        grabbed = False
        try:
            dev.grab()
            grabbed = True
        except Exception as exc:
            logger.warning("[usb] failed to grab input device %s: %s", path, exc)
        return dev, grabbed

    async def _run(self) -> None:
        backoff = 1.0  # seconds, exponential up to 10s
        no_device_failures = 0
        open_failures = 0
        wait_state: Optional[str] = None
        last_wait_log = 0.0
        warn_every = 5
        while not self._stop.is_set():
            receiver_present = _unifying_receiver_present()
            self._receiver_present = receiver_present
            path = self._resolve_device_path()
            self._paired_remote = bool(path)
            if not path:
                # device not present; wait and retry (jittered)
                self._input_open = False
                no_device_failures += 1
                open_failures = 0
                sleep_for = _jittered(backoff)
                now = time.monotonic()
                next_state = "present_no_input" if receiver_present else "absent"
                log_msg = (
                    "[usb] receiver present; waiting for paired device "
                    "(attempt %d); retry in %.2fs"
                    if receiver_present
                    else "[usb] receiver not detected "
                    "(attempt %d); retry in %.2fs"
                )
                if wait_state != next_state:
                    wait_state = next_state
                    last_wait_log = now
                    logger.warning(
                        log_msg,
                        no_device_failures,
                        sleep_for,
                    )
                elif (
                    no_device_failures == 1
                    or no_device_failures % warn_every == 0
                    or (now - last_wait_log) >= 60.0
                ):
                    last_wait_log = now
                    logger.warning(
                        log_msg,
                        no_device_failures,
                        sleep_for,
                    )
                await asyncio.sleep(sleep_for)
                backoff = min(backoff * 2, 10.0)
                continue
            no_device_failures = 0
            # Try to open the device
            try:
                dev, grabbed = self._open_device(path)
            except Exception:
                self._input_open = False
                open_failures += 1
                sleep_for = _jittered(backoff)
                if open_failures == 1 or open_failures % warn_every == 0:
                    logger.warning(
                        "[usb] open failed (attempt %d, path=%s); retry in %.2fs",
                        open_failures,
                        path,
                        sleep_for,
                    )
                await asyncio.sleep(sleep_for)
                backoff = min(backoff * 2, 10.0)
                continue
            open_failures = 0
            self._input_open = True
            self._last_input_path = path
            self._last_grabbed = grabbed
            self._disconnect_notified = False
    
            if wait_state != "ready":
                wait_state = "ready"
                logger.info("[usb] input device opened: %s grabbed=%s", path, str(grabbed).lower())
    
            # We have an open device; reset backoff
            backoff = 1.0
            
            last_msc_scan: Optional[int] = None
            pressed = set()
            
            _EV_MSC = ecodes.EV_MSC
            _MSC_SCAN = ecodes.MSC_SCAN
            _EV_KEY = ecodes.EV_KEY
            
            _map = self._map
            _resolve = self._resolve_logical_key
            _emit = self._emit
            _debug_unknown = logger.isEnabledFor(logging.DEBUG)
            
            disconnect_seen = False
            try:
                async for ev in dev.async_read_loop():
                    t = ev.type
            
                    if t == _EV_MSC and ev.code == _MSC_SCAN:
                        last_msc_scan = int(ev.value)
                        continue
            
                    if t != _EV_KEY:
                        continue
            
                    logical = _resolve(ev.code, last_msc_scan)
                    last_msc_scan = None  # single-use
            
                    if not logical:
                        if _debug_unknown:
                            # only compute name when actually logging
                            try:
                                kname = ecodes.KEY[ev.code]
                            except Exception:
                                kname = f"KEY_{ev.code}"
                            logger.debug("[usb] unmapped key: msc=None name=%s", kname)
                        continue
            
                    val = ev.value
                    if val == 2:  # auto-repeat from kernel
                        continue
            
                    key_id = (logical, ev.code)
                    if val == 1:  # down
                        if key_id in pressed:
                            continue
                        pressed.add(key_id)
                        await _emit(logical, "down")
                    else:  # up
                        pressed.discard(key_id)
                        await _emit(logical, "up")
            
            except (OSError, IOError) as e:
                err = getattr(e, "errno", None)
                if err in (19, 5):  # ENODEV/EIO
                    disconnect_seen = True
                    await self._notify_disconnect()
                    await asyncio.sleep(_jittered(backoff))
                    backoff = min(backoff * 2, 10.0)
                else:
                    await asyncio.sleep(_jittered(1.0))

            except asyncio.CancelledError:
                # Shutting down
                break

            except Exception as exc:
                logger.warning("[usb] reader error: %r", exc)
                await asyncio.sleep(_jittered(1.0))

            finally:
                with contextlib.suppress(Exception):
                    if grabbed:
                        dev.ungrab()
                with contextlib.suppress(Exception):
                    dev.close()
                self._input_open = False
                if not disconnect_seen and not self._stop.is_set():
                    await self._notify_disconnect()
    
        # exit: ensure stop flag remains set
        self._stop.set()

    def _resolve_logical_key(self, key_code: int, msc_scan: Optional[int]) -> Optional[str]:
        # Prefer explicit MSC numeric mapping
        if msc_scan is not None:
            mapped = self._map.get(str(msc_scan))
            if mapped:
                return mapped

        # Else KEY_* name mapping
        name = _key_name_from_code(key_code)
        if name:
            mapped = self._map.get(name)
            if mapped:
                return mapped

        return None

    async def _emit(self, rem_key: str, edge: str) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[usb] %s %s", rem_key, edge)
        queue = self._edge_queue
        if queue is None:
            return
        try:
            queue.put_nowait((rem_key, edge))
        except asyncio.QueueFull:
            self._dropped_edges += 1
            now = time.monotonic()
            if now - self._last_drop_log >= 10.0:
                self._last_drop_log = now
                logger.warning(
                    "[usb] edge queue full; dropped=%d",
                    self._dropped_edges,
                )

    async def _drain_edges(self) -> None:
        queue = self._edge_queue
        if queue is None:
            return
        try:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                rem_key, edge = item
                try:
                    res = self._on_edge(rem_key, edge)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    logger.warning("[usb] dispatch error: %r", exc)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            while not queue.empty():
                with contextlib.suppress(Exception):
                    queue.get_nowait()
                    queue.task_done()

    async def _notify_disconnect(self) -> None:
        if self._disconnect_notified:
            return
        self._disconnect_notified = True
        callback = self._on_disconnect
        if callback is None:
            return
        try:
            res = callback()
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            logger.warning("[usb] disconnect handler error: %r", exc)


def _autodetect_or_none() -> Optional[str]:
    """Best-effort find a keyboard-like event device via by-id/by-path; return None if absent."""
    cand = (
        sorted(glob.glob("/dev/input/by-id/*Logitech*USB_Receiver*event-kbd"))
        or sorted(glob.glob("/dev/input/by-id/*event-kbd"))
        or sorted(glob.glob("/dev/input/by-path/*event-kbd"))
    )
    return cand[0] if cand else None


def _unifying_receiver_present() -> bool:
    """Return True if a Logitech Unifying receiver is present via sysfs."""
    for dev_path in glob.glob("/sys/bus/usb/devices/*"):
        vendor_path = os.path.join(dev_path, "idVendor")
        product_path = os.path.join(dev_path, "idProduct")
        try:
            with open(vendor_path, "r", encoding="utf-8") as vendor_file:
                vendor = vendor_file.read().strip().lower()
            with open(product_path, "r", encoding="utf-8") as product_file:
                product = product_file.read().strip().lower()
        except OSError:
            continue
        if vendor == "046d" and product == "c52b":
            return True
    return False


def _key_name_from_code(code: int) -> Optional[str]:
    try:
        name = ecodes.KEY[code]  # e.g. 'KEY_LEFT'
        return name if isinstance(name, str) else None
    except Exception:
        return None
