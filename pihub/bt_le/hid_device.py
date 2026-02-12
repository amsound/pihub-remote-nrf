#!/usr/bin/env python3
"""BlueZ HID service definitions and lifecycle helpers."""

import asyncio
import os
import contextlib
import logging
import time

from bluez_peripheral.util import get_message_bus, Adapter, is_bluez_available
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.gatt.service import Service, ServiceCollection
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.gatt.descriptor import DescriptorFlags as DescFlags

from dbus_fast.constants import MessageType
from dbus_fast import Variant
from dataclasses import dataclass, field
from dbus_fast.errors import DBusError

logger = logging.getLogger(__name__)

async def ensure_controller_baseline(bus, adapter_name: str, *, adapter_proxy=None) -> None:
    """Re-apply the minimum controller state we need for reliable (re)pair + reconnect.

    Why: toggling Powered (or restarting bluetoothd) can silently reset Pairable/Discoverable
    and timeouts. Apple TV is very sensitive to this during reconnect.
    """
    try:
        from dbus_fast import Variant  # already a dependency elsewhere in this file
    except Exception:
        Variant = None  # type: ignore

    path = f"/org/bluez/{adapter_name}"

    # Build a proxy if caller didn't pass one
    if adapter_proxy is None:
        try:
            xml = await bus.introspect("org.bluez", path)
            adapter_proxy = bus.get_proxy_object("org.bluez", path, xml)
        except Exception as exc:
            logger.warning("[hid] Baseline: couldn't introspect %s: %s", path, exc)
            return

    props = adapter_proxy.get_interface("org.freedesktop.DBus.Properties")

    async def _set(prop: str, sig: str, val):
        if Variant is None:
            return
        try:
            await props.call_set("org.bluez.Adapter1", prop, Variant(sig, val))
        except Exception as exc:
            # Some properties may be read-only depending on controller/BlueZ build
            logger.debug("[hid] Baseline: set %s=%r failed: %s", prop, val, exc)

    # Keep these "sticky" across restarts and power cycles
    await _set("Powered", "b", True)
    await _set("PairableTimeout", "u", 0)
    await _set("DiscoverableTimeout", "u", 0)
    await _set("Pairable", "b", True)
    await _set("Discoverable", "b", True)


_hid_service_singleton = None  # set inside start_hid()
_advertising_state = False

# --------------------------
# Device identity / advert
# --------------------------

APPEARANCE   = 0x03C1  # Keyboard

# Report IDs
RID_KEYBOARD = 0x01
RID_CONSUMER = 0x02

# --------------------------
# HID Report Map (Keyboard + Consumer bitfield)
# --------------------------
REPORT_MAP = bytes([
    # Keyboard (Boot) – Report ID 1
    0x05,0x01, 0x09,0x06, 0xA1,0x01,
      0x85,0x01,              # REPORT_ID (1)
      0x05,0x07,              #   USAGE_PAGE (Keyboard)
      0x19,0xE0, 0x29,0xE7,   #   USAGE_MIN/MAX (modifiers)
      0x15,0x00, 0x25,0x01,   #   LOGICAL_MIN 0 / MAX 1
      0x75,0x01, 0x95,0x08,   #   REPORT_SIZE 1, COUNT 8  (mod bits)
      0x81,0x02,              #   INPUT (Data,Var,Abs)
      0x95,0x01, 0x75,0x08,   #   reserved byte
      0x81,0x01,              #   INPUT (Const,Array,Abs)
      0x95,0x06, 0x75,0x08,   #   6 keys
      0x15,0x00, 0x25,0x65,   #   key range 0..0x65
      0x19,0x00, 0x29,0x65,   #   USAGE_MIN/MAX (keys)
      0x81,0x00,              #   INPUT (Data,Array,Abs)
    0xC0,

    # Consumer Control – 16‑bit *array* usage (Report ID 2) — 2‑byte value
    0x05,0x0C, 0x09,0x01, 0xA1,0x01,
      0x85,0x02,              # REPORT_ID (2)
      0x15,0x00,              # LOGICAL_MIN 0
      0x26,0xFF,0x03,         # LOGICAL_MAX 0x03FF
      0x19,0x00,              # USAGE_MIN 0x0000
      0x2A,0xFF,0x03,         # USAGE_MAX 0x03FF
      0x75,0x10,              # REPORT_SIZE 16
      0x95,0x01,              # REPORT_COUNT 1 (one slot)
      0x81,0x00,              # INPUT (Data,Array,Abs)
    0xC0,
])
    

async def _cleanup_stale_adverts(bus, adapter_name: str, base_path: str = "/com/spacecheese/bluez_peripheral/advert", max_ids: int = 8) -> None:
    """Best-effort cleanup for advertisements that can be left registered if we crashed mid-startup."""
    from contextlib import suppress

    try:
        mgr = await _get_adv_manager(bus, adapter_name)
    except Exception:
        return

    for i in range(max_ids):
        path = f"{base_path}{i}"
        with suppress(Exception):
            await mgr.call_unregister_advertisement(path)

async def _get_adv_manager(bus, adapter_name: str):
    """Return LEAdvertisingManager1 proxy for the given adapter."""
    obj = await bus.introspect("org.bluez", f"/org/bluez/{adapter_name}")
    proxy = bus.get_proxy_object("org.bluez", f"/org/bluez/{adapter_name}", obj)
    return proxy.get_interface("org.bluez.LEAdvertisingManager1")

def _make_advert(cfg, runtime) -> Advertisement:
    """Create the single BLE advertisement instance for the HID service."""
    device_name = getattr(cfg, "device_name", None) or os.uname().nodename
    appearance = int(getattr(cfg, "appearance", APPEARANCE))
    return Advertisement(
        localName=device_name,
        serviceUUIDs=["1812"],
        appearance=appearance,
    )

def _default_advert_path() -> str:
    return f"/com/spacecheese/bluez_peripheral/advert_{os.getpid()}"

async def _adv_register(runtime, cfg) -> bool:
    async with runtime.advert_lock:
        if runtime.advertising:
            return False
        if runtime.advert is None:
            runtime.advert = _make_advert(cfg, runtime)
        if runtime.advert_path is None:
            runtime.advert_path = _default_advert_path()
        await runtime.advert.register(runtime.bus, adapter=runtime.adapter, path=runtime.advert_path)
        runtime.advertising = True
        _set_advertising_state(True)
        return True

async def _adv_unregister(runtime) -> bool:
    async with runtime.advert_lock:
        if not runtime.advertising:
            return False
        try:
            if runtime.advert is not None:
                await runtime.advert.unregister()
            elif runtime.advert_path:
                mgr = await _get_adv_manager(runtime.bus, runtime.adapter_name)
                await mgr.call_unregister_advertisement(runtime.advert_path)
        except DBusError as exc:
            if "Does Not Exist" not in str(exc) and "does not exist" not in str(exc):
                raise
        runtime.advertising = False
        _set_advertising_state(False)
        return True

# --------------------------
# BlueZ object manager helpers
# --------------------------
def _get_bool(v):  # unwrap dbus_next.Variant or use raw bool
    return bool(v.value) if isinstance(v, Variant) else bool(v)

def _get_str(v):  # unwrap dbus_next.Variant or use raw str
    if v is None:
        return ""
    return str(v.value) if isinstance(v, Variant) else str(v)


def _set_advertising_state(active: bool) -> None:
    global _advertising_state
    _advertising_state = bool(active)

def advertising_active() -> bool:
    return _advertising_state


_connected_state = {"connected": False, "device_path": None, "address": None}

def _set_connected(connected: bool, device_path: str | None = None, address: str | None = None) -> None:
    _connected_state["connected"] = bool(connected)
    _connected_state["device_path"] = device_path if connected else None
    _connected_state["address"] = address if connected else None

def connected_active() -> bool:
    return bool(_connected_state.get("connected"))

def connected_address() -> str | None:
    return _connected_state.get("address")

async def trust_device(bus, device_path, *, log=None, fail_logged: set[str] | None = None) -> bool:
    """Set org.bluez.Device1.Trusted = True for the connected peer."""
    try:
        root_xml = await bus.introspect("org.bluez", device_path)
        dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
        return True
    except Exception as exc:
        if log is not None and fail_logged is not None and device_path not in fail_logged:
            fail_logged.add(device_path)
            log.debug("[hid] trust failed for %s: %s", device_path, exc)
        return False
        
async def _get_managed_objects(bus):
    root_xml = await bus.introspect("org.bluez", "/")
    root = bus.get_proxy_object("org.bluez", "/", root_xml)
    om = root.get_interface("org.freedesktop.DBus.ObjectManager")
    return await om.call_get_managed_objects()

async def wait_for_any_connection(
    bus,
    adapter_name: str,
    timeout_s: float | None = None,
) -> str | None:
    """Return the DBus object path of the first connected Device1 on this adapter.

    Centrals (including Apple TV) will often connect *before* the user confirms the pairing popup,
    just to browse GATT and begin the bonding flow. That is expected.

    If timeout_s is None, wait forever.
    """
    adapter_path = f"/org/bluez/{adapter_name}"
    bluez_root = "/org/bluez"
    deadline = None if timeout_s is None else (time.monotonic() + float(timeout_s))

    while True:
        managed = await _get_managed_objects(bus)
        for path, ifaces in managed.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            if _get_str(dev.get("Adapter")) != adapter_path:
                continue
            if _get_bool(dev.get("Connected", False)):
                return path

        if deadline is not None and time.monotonic() >= deadline:
            return None

        await asyncio.sleep(0.2)
async def wait_until_services_resolved(bus, device_path, timeout_s=30, poll_interval=0.25):
    """Wait for Device1.ServicesResolved == True for this device."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        objs = await _get_managed_objects(bus)
        dev = objs.get(device_path, {}).get("org.bluez.Device1")
        if dev and _get_bool(dev.get("ServicesResolved", False)):
            return True
        await asyncio.sleep(poll_interval)
    return False

async def wait_for_disconnect(bus, device_path, poll_interval=0.5):
    """Block until this device disconnects."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def handler(msg):
        if msg.message_type is not MessageType.SIGNAL:
            return
        if msg.member != "PropertiesChanged" or msg.path != device_path:
            return
        iface, changed, _ = msg.body
        if iface == "org.bluez.Device1" and "Connected" in changed and not _get_bool(changed["Connected"]) and not fut.done():
            fut.set_result(None)

    bus.add_message_handler(handler)
    try:
        while True:
            if fut.done():
                await fut
                return
            objs = await _get_managed_objects(bus)
            dev = objs.get(device_path, {}).get("org.bluez.Device1")
            if not dev or not _get_bool(dev.get("Connected", False)):
                return
            await asyncio.sleep(poll_interval)
    finally:
        bus.remove_message_handler(handler)
        
async def _get_device_alias_or_name(bus, device_path) -> str:
    try:
        root_xml = await bus.introspect("org.bluez", device_path)
        dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

        alias = await props.call_get("org.bluez.Device1", "Alias")
        name  = await props.call_get("org.bluez.Device1", "Name")

        def _unwrap(v): 
            from dbus_fast import Variant
            return v.value if isinstance(v, Variant) else v

        return _unwrap(alias) or _unwrap(name) or ""
    except Exception:
        return ""



async def wait_until_bonded(
    bus,
    device_path: str,
    timeout_s: float = 60.0,
) -> bool:
    """Wait for Device1.Bonded==True on a specific device path."""
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        managed = await _get_managed_objects(bus)
        dev = managed.get(device_path, {}).get("org.bluez.Device1")
        if not dev:
            return False
        if bool(dev.get("Bonded", False)):
            return True
        await asyncio.sleep(0.2)
    return False

async def watch_link(runtime, cfg, *, allow_pairing: bool = True):
    """Robust advertise/connect/reconnect loop for the HID peripheral.

    Key behaviors:
      - Ensure the adapter stays in a sane baseline (LE mode, powered, discoverable/pairable as required)
      - Ensure exactly one advertisement is active while waiting for a central
      - Once a central is connected AND ready (services resolved + bonded + CCCD), stop advertising
        so other devices stop seeing the peripheral.
      - On disconnect, restart advertising and repeat.
    """
    log = logger
    bus = runtime.bus
    adapter_name = runtime.adapter_name
    adapter_path = f"/org/bluez/{adapter_name}"
    bluez_root = "/org/bluez"
    dev_prefix = f"{adapter_path}/dev_"

    managed_cache: dict[str, dict] = {}
    trust_fail_logged: set[str] = set()
    trust_success_logged: set[str] = set()
    trust_retry_handles: dict[str, asyncio.Handle] = {}
    cached_fields = ("Address", "Alias", "Name")

    async def _refresh_managed_cache() -> dict[str, dict]:
        nonlocal managed_cache
        managed_cache = await _get_managed_objects(bus)
        for path, ifaces in managed_cache.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            for key in cached_fields:
                if key in dev:
                    dev[key] = _get_str(dev[key])
        return managed_cache

    def _get_cached_device_props(device_path: str) -> dict:
        return managed_cache.get(device_path, {}).get("org.bluez.Device1", {})

    def _update_device_cache(device_path: str, props: dict) -> None:
        if device_path not in managed_cache:
            managed_cache[device_path] = {"org.bluez.Device1": {}}
        dev = managed_cache[device_path].setdefault("org.bluez.Device1", {})
        for key, value in props.items():
            dev[key] = _get_str(value) if key in cached_fields else value

    def _device_label(device_path: str, *, fallback_addr: str | None = None) -> str:
        cached = _get_cached_device_props(device_path)
        alias = _get_str(cached.get("Alias"))
        name = _get_str(cached.get("Name"))
        addr = _get_str(cached.get("Address")) or (fallback_addr or "")
        return alias or name or addr or "unknown"

    async def _get_device_address(device_path: str) -> str:
        cached = _get_cached_device_props(device_path)
        if cached:
            addr = _get_str(cached.get("Address"))
            if addr:
                return addr
        try:
            root_xml = await bus.introspect("org.bluez", device_path)
            dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
            props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
            addr = await props.call_get("org.bluez.Device1", "Address")
            return _get_str(addr)
        except Exception:
            return ""

    def _connected_devices_from(managed: dict[str, dict]) -> set[str]:
        connected = set()
        for path, ifaces in managed.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            if not path.startswith(dev_prefix):
                continue
            if _get_bool(dev.get("Connected", False)):
                connected.add(path)
        return connected

    async def _get_device_props(device_path: str) -> dict:
        try:
            root_xml = await bus.introspect("org.bluez", device_path)
            dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
            props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
            dev = await props.call_get_all("org.bluez.Device1")
            _update_device_cache(device_path, dev)
            return dev
        except Exception:
            return {}

    def _schedule_trust_retry(device_path: str) -> None:
        if device_path in trust_retry_handles:
            return
        handle = loop.call_later(0.5, lambda: asyncio.create_task(_maybe_trust_device(device_path)))
        trust_retry_handles[device_path] = handle

    async def _maybe_trust_device(device_path: str) -> None:
        """
        Attempt to mark the connected peer as trusted once it has completed the
        pairing/bonding process. BlueZ requires a device to be both paired
        (or bonded) before the Trusted property may be set; an untrusted LE
        device will not automatically reconnect after a disconnect【160784031106547†L269-L279】.

        This helper inspects the live Device1 properties each time rather
        than relying solely on a cached snapshot. If the remote is not yet
        paired or bonded, a retry will be scheduled while the link remains
        connected. Once Trusted has been successfully set, a one‑off log
        message is emitted and future calls become no‑ops.
        """
        # clear any existing retry handle for this path
        trust_retry_handles.pop(device_path, None)
        # Always fetch the latest device properties because the cached view may
        # lag behind the actual DBus state. Only fall back to the cache if we
        # have recently updated it and it already contains the Paired/Bonded keys.
        dev = _get_cached_device_props(device_path)
        need_props = not dev or ("Paired" not in dev and "Bonded" not in dev) or "Trusted" not in dev
        if need_props:
            dev = await _get_device_props(device_path)
        if not dev:
            return
        # If already trusted there's nothing more to do.
        if _get_bool(dev.get("Trusted", False)):
            return
        # Consider both Paired and Bonded properties when deciding if the
        # link has completed the pairing process. Some BlueZ versions only
        # expose Bonded, so fall back accordingly【160784031106547†L269-L279】.
        paired_or_bonded = False
        # Both properties may be Variants – unwrap them with _get_bool().
        if "Paired" in dev:
            paired_or_bonded = paired_or_bonded or _get_bool(dev.get("Paired", False))
        if "Bonded" in dev:
            paired_or_bonded = paired_or_bonded or _get_bool(dev.get("Bonded", False))
        # If the device is not yet paired/bonded but still connected, schedule
        # a retry. Once the link is disconnected we will not reschedule.
        if not paired_or_bonded:
            if _get_bool(dev.get("Connected", False)):
                _schedule_trust_retry(device_path)
            return
        # Device is paired or bonded – attempt to mark it as trusted. This
        # may still fail if BlueZ has not yet persisted its state; on failure
        # the helper will quietly log debug and bail. Subsequent calls will
        # reattempt once the properties have stabilised.
        trusted = await trust_device(bus, device_path, log=log, fail_logged=trust_fail_logged)
        if trusted and device_path not in trust_success_logged:
            label = _device_label(device_path, fallback_addr=_get_str(dev.get("Address")))
            # Simplify the trusted log message: use the same label format as the
            # connected message.  If the resolved label and address are the same
            # value (e.g. when there is no alias), we avoid printing the address
            # twice.  See _device_label() for ordering of alias/name/address.
            if label:
                log.info("[hid] trusted %s", label)
            else:
                # fall back to the MAC address when label is blank
                addr = _get_str(dev.get("Address")) or await _get_device_address(device_path) or "unknown"
                log.info("[hid] trusted %s", addr)
            trust_success_logged.add(device_path)

    def _cccd_enabled() -> bool:
        return any(runtime.hid._notif_state())

    async def _maybe_ready() -> None:
        """
        Determine when a BLE connection is fully ready for operation and log a
        concise message. A connection is considered ready when:

        * Service discovery has completed (ServicesResolved==True)
        * Notification subscriptions for all required CCCDs have been enabled
        * The link has completed pairing/bonding

        The runtime.ready flag ensures this is emitted only once per
        connection. Upon disconnect the ready state is reset in
        _note_disconnected().
        """
        if runtime.ready or not runtime.connected or not runtime.device_path:
            return
        # Check if services discovery has completed.  BlueZ may reset
        # ServicesResolved to False on daemon restarts and not emit a
        # subsequent PropertiesChanged signal.  If services_ok is False but
        # the device is still connected, proactively poll until it
        # becomes True.  This helps recover readiness on stacks that
        # suppress the ServicesResolved signal after reconnects.
        services_ok = runtime.services_resolved
        if not services_ok and runtime.connected and runtime.device_path:
            try:
                # Poll for up to two seconds.  This returns True as soon as
                # BlueZ reports services have been resolved; otherwise
                # services_ok remains False and readiness will not be set.
                if await wait_until_services_resolved(
                    runtime.bus, runtime.device_path, timeout_s=2.0, poll_interval=0.25
                ):
                    runtime.services_resolved = True
                    services_ok = True
            except Exception:
                pass
        # Check if our input report CCCDs are enabled (notifications registered)
        cccd_ok = _cccd_enabled()
        # Retrieve latest device properties for pairing/bonding state
        dev = _get_cached_device_props(runtime.device_path)
        paired_ok = False
        if dev:
            if "Paired" in dev and _get_bool(dev.get("Paired", False)):
                paired_ok = True
            if "Bonded" in dev and _get_bool(dev.get("Bonded", False)):
                paired_ok = True
        # We don't block readiness solely on paired_ok; Apple TV will still
        # negotiate pairing as part of its HID handshake. However logging
        # whether the link is bonded helps diagnose trust issues.
        if not services_ok or not cccd_ok:
            return
        runtime.ready = True
        runtime.hid._link_ready = True
        log.info(
            "[hid] ready (services=%s, cccd=%s, bonded=%s)",
            services_ok,
            cccd_ok,
            paired_ok,
        )

    def _note_connected(device_path: str, addr: str | None, services_resolved: bool | None = None) -> None:
        runtime.connected = True
        runtime.device_path = device_path
        runtime.peer_mac = addr or None
        if services_resolved is not None:
            runtime.services_resolved = bool(services_resolved)
        _set_connected(True, device_path=device_path, address=addr)

    def _note_disconnected() -> None:
        runtime.connected = False
        runtime.device_path = None
        runtime.peer_mac = None
        runtime.services_resolved = False
        runtime.ready = False
        runtime.hid._link_ready = False
        _set_connected(False)

    async def _sync_advertising() -> None:
        if runtime.connected_devices:
            stopped = await _adv_unregister(runtime)
            if stopped:
                log.info("[hid] advertising stopped")
            return
        started = await _adv_register(runtime, cfg)
        if started:
            # Reassert the adapter baseline each time advertising starts.  On some
            # platforms toggling Powered or restarting bluetoothd silently resets
            # Pairable/Discoverable flags and timeouts【208768328097654†L324-L349】.
            # If we don't restore these settings the Apple TV may not see the
            # peripheral until PiHub is manually restarted.  Failure to set the
            # baseline is non‑fatal, so we ignore exceptions.
            try:
                await ensure_controller_baseline(runtime.bus, runtime.adapter_name)
            except Exception:
                pass
            log.info("[hid] advertising resumed")

    async def _handle_connected(device_path: str, *, services_resolved: bool | None = None) -> None:
        addr = await _get_device_address(device_path) or "unknown"
        label = _device_label(device_path, fallback_addr=addr)
        if device_path not in runtime.connected_devices:
            runtime.connected_devices.add(device_path)
            log.info("[hid] connected %s", label)
        if not runtime.device_path:
            runtime.ready = False
            runtime.hid._link_ready = False
            _note_connected(device_path, addr, services_resolved=services_resolved)
        asyncio.create_task(_maybe_trust_device(device_path))
        await _sync_advertising()
        await _maybe_ready()

    async def _handle_disconnected(device_path: str, *, addr: str | None = None) -> None:
        addr = addr or await _get_device_address(device_path) or "unknown"
        label = _device_label(device_path, fallback_addr=addr)
        was_connected = device_path in runtime.connected_devices
        if was_connected:
            runtime.connected_devices.discard(device_path)
            log.info("[hid] disconnected %s", label)
        if runtime.device_path == device_path:
            if runtime.connected_devices:
                new_path = sorted(runtime.connected_devices)[0]
                dev = _get_cached_device_props(new_path)
                new_addr = _get_str(dev.get("Address")) or await _get_device_address(new_path) or "unknown"
                runtime.services_resolved = _get_bool(dev.get("ServicesResolved", False))
                runtime.ready = False
                runtime.hid._link_ready = False
                _note_connected(new_path, new_addr, services_resolved=runtime.services_resolved)
            else:
                _note_disconnected()
        if not runtime.connected_devices:
            await _sync_advertising()

    async def _handle_services_resolved(device_path: str) -> None:
        if runtime.device_path and runtime.device_path != device_path:
            return
        runtime.services_resolved = True
        await _maybe_ready()

    async def _poll_ready() -> None:
        while runtime.connected and not runtime.ready:
            await _maybe_ready()
            await asyncio.sleep(0.25)

    async def _seed_existing_connection() -> None:
        """
        Seed the runtime state with any pre‑existing connection when starting
        watch_link().  This is called once before any D‑Bus signal handlers
        have processed events.  If a central is already connected when we
        register our handlers, BlueZ will not emit a new `PropertiesChanged`
        event for the `Connected` property, so we need to initialise our
        internal state and schedule readiness polling ourselves.
        """
        nonlocal ready_task
        managed = await _refresh_managed_cache()
        runtime.connected_devices = _connected_devices_from(managed)
        if runtime.connected_devices:
            # Use the first connected device as our primary link.
            path = sorted(runtime.connected_devices)[0]
            dev = _get_cached_device_props(path)
            addr = _get_str(dev.get("Address")) or await _get_device_address(path) or "unknown"
            # Capture current ServicesResolved state but reset runtime.ready to
            # False so that readiness will be evaluated again.
            runtime.services_resolved = _get_bool(dev.get("ServicesResolved", False))
            runtime.ready = False
            runtime.hid._link_ready = False
            _note_connected(path, addr, services_resolved=runtime.services_resolved)
            await _sync_advertising()
            log.info("[hid] connected %s", _device_label(path, fallback_addr=addr))
            # Since BlueZ won't send us a `Connected` signal for an existing
            # connection, we won't schedule a polling task in the signal
            # handler.  Kick off the readiness polling here to evaluate
            # services_resolved, CCCD subscription and bonding status.  If
            # a ready_task is already running (unlikely at startup) cancel it.
            if ready_task:
                ready_task.cancel()
                ready_task = None
            ready_task = loop.create_task(_poll_ready())
        else:
            _note_disconnected()
            await _sync_advertising()

    loop = asyncio.get_running_loop()
    ready_task = None
    reconcile_task = None

    async def _reconcile_loop() -> None:
        """
        Periodically reconcile the set of connected devices and re‑assert
        controller baseline settings when idle.  To reduce bus chatter,
        baseline re‑application is performed no more frequently than once
        every 30 seconds and only when no devices are connected.
        """
        # Track the last time we re‑asserted the controller baseline.  This
        # reduces redundant writes to BlueZ properties and systemd journal
        # noise when the adapter remains idle for long periods.
        last_baseline_time: float = 0.0
        baseline_interval: float = 60.0  # seconds between baseline updates
        # Use a longer sleep interval to reduce bus load.  BlueZ's signal
        # handlers catch the vast majority of connect/disconnect events,
        # so reconciliation can run less frequently without missing state
        # changes.  Increasing this to 15s (from 5s) yields a 3× reduction
        # in full ObjectManager dumps on idle systems.
        sleep_interval: float = 15.0
        while True:
            managed = await _refresh_managed_cache()
            desired = _connected_devices_from(managed)
            if desired != runtime.connected_devices:
                added = desired - runtime.connected_devices
                removed = runtime.connected_devices - desired
                for path in sorted(removed):
                    await _handle_disconnected(path)
                for path in sorted(added):
                    dev = _get_cached_device_props(path)
                    services_resolved = _get_bool(dev.get("ServicesResolved", False))
                    await _handle_connected(path, services_resolved=services_resolved)
                runtime.connected_devices = desired
                if not runtime.connected_devices:
                    _note_disconnected()
                elif runtime.device_path not in runtime.connected_devices:
                    path = sorted(runtime.connected_devices)[0]
                    dev = _get_cached_device_props(path)
                    addr = _get_str(dev.get("Address")) or await _get_device_address(path) or "unknown"
                    runtime.services_resolved = _get_bool(dev.get("ServicesResolved", False))
                    runtime.ready = False
                    runtime.hid._link_ready = False
                    _note_connected(path, addr, services_resolved=runtime.services_resolved)
            # Only re‑assert controller baseline when no devices are connected and
            # the interval has elapsed.  This prevents sending repeated
            # Pairable/Discoverable writes when the adapter remains idle but
            # nothing has changed.
            if not runtime.connected_devices:
                now = time.monotonic()
                if now - last_baseline_time >= baseline_interval:
                    try:
                        await ensure_controller_baseline(bus, adapter_name)
                    except Exception:
                        pass
                    else:
                        last_baseline_time = now
            await asyncio.sleep(sleep_interval)

    def handler(msg):
        nonlocal ready_task
        if msg.message_type is not MessageType.SIGNAL:
            return
        if msg.member == "InterfacesAdded":
            if not msg.path.startswith(bluez_root):
                return
            path, ifaces = msg.body
            dev = ifaces.get("org.bluez.Device1")
            if not dev or not path.startswith(dev_prefix):
                return
            managed_cache[path] = ifaces
            _update_device_cache(path, dev)
            if _get_bool(dev.get("Connected", False)):
                loop.create_task(_handle_connected(path, services_resolved=_get_bool(dev.get("ServicesResolved", False))))
            return
        if msg.member == "InterfacesRemoved":
            if not msg.path.startswith(bluez_root):
                return
            path, _ifaces = msg.body
            if not path.startswith(dev_prefix):
                return
            cached_addr = _get_str(_get_cached_device_props(path).get("Address"))
            loop.create_task(_handle_disconnected(path, addr=cached_addr or None))
            managed_cache.pop(path, None)
            return
        if msg.member != "PropertiesChanged":
            return
        iface, changed, _ = msg.body
        if iface != "org.bluez.Device1":
            return
        if not msg.path.startswith(dev_prefix):
            return

        _update_device_cache(msg.path, changed)

        if "Connected" in changed:
            if _get_bool(changed["Connected"]):
                if ready_task:
                    ready_task.cancel()
                ready_task = loop.create_task(_poll_ready())
                loop.create_task(_handle_connected(msg.path, services_resolved=_get_bool(changed.get("ServicesResolved", False))))
            else:
                if ready_task:
                    ready_task.cancel()
                    ready_task = None
                loop.create_task(_handle_disconnected(msg.path))
        if "ServicesResolved" in changed and _get_bool(changed["ServicesResolved"]):
            loop.create_task(_handle_services_resolved(msg.path))
        if "Paired" in changed and _get_bool(changed["Paired"]):
            loop.create_task(_maybe_trust_device(msg.path))

    bus.add_message_handler(handler)
    try:
        await _seed_existing_connection()
        reconcile_task = asyncio.create_task(_reconcile_loop())
        while True:
            await asyncio.sleep(1.0)
    finally:
        if reconcile_task:
            reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconcile_task
        if ready_task:
            ready_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_task
        bus.remove_message_handler(handler)

class BatteryService(Service):
    def __init__(self, initial_level: int = 100):
        super().__init__("180F", True)
        lvl = max(0, min(100, int(initial_level)))
        self._level = bytearray([lvl])

    @characteristic("2A19", CharFlags.READ | CharFlags.NOTIFY)
    def battery_level(self, _):
        # 0..100
        return bytes(self._level)

    # Convenience: call this to update + notify
    def set_level(self, pct: int):
        pct = max(0, min(100, int(pct)))
        if self._level[0] != pct:
            self._level[0] = pct
            try:
                self.battery_level.changed(bytes(self._level))
            except Exception:
                pass

class DeviceInfoService(Service):
    def __init__(self, manufacturer="PiKB Labs", model="PiKB-1", vid=0xFFFF, pid=0x0001, ver=0x0100):
        super().__init__("180A", True)
        self._mfg   = manufacturer.encode("utf-8")
        self._model = model.encode("utf-8")
        self._pnp   = bytes([0x02, vid & 0xFF, (vid>>8)&0xFF, pid & 0xFF, (pid>>8)&0xFF, ver & 0xFF, (ver>>8)&0xFF])

    @characteristic("2A29", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def manufacturer_name(self, _):
        return self._mfg

    @characteristic("2A24", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def model_number(self, _):
        return self._model

    @characteristic("2A50", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def pnp_id(self, _):
        return self._pnp

class HIDService(Service):
    def __init__(self):
        super().__init__("1812", True)
        self._proto = bytearray([1])  # Report Protocol
        self._link_ready: bool = False

    # -------- subscription helpers --------
    def _is_subscribed(self, char) -> bool:
        # Supports both property and method styles found in bluez_peripheral
        for attr in ("is_notifying", "notifying"):
            if hasattr(char, attr):
                v = getattr(char, attr)
                return v() if callable(v) else bool(v)
        # If the library doesn’t expose state, assume subscribed
        return True

    def _notif_state(self) -> tuple[bool, bool, bool]:
        kb   = self._is_subscribed(self.input_keyboard)
        boot = self._is_subscribed(self.boot_keyboard_input)
        cc   = self._is_subscribed(self.input_consumer)
        return (kb, boot, cc)

    # ---------------- GATT Characteristics ----------------
    # Protocol Mode (2A4E): READ/WRITE (encrypted both)
    @characteristic("2A4E", CharFlags.READ | CharFlags.WRITE | CharFlags.ENCRYPT_READ | CharFlags.ENCRYPT_WRITE)
    def protocol_mode(self, _):
        return bytes(self._proto)
    @protocol_mode.setter
    def protocol_mode_set(self, value, _):
        self._proto[:] = value

    # HID Information (2A4A): READ (encrypted)
    @characteristic("2A4A", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def hid_info(self, _):
        return bytes([0x11, 0x01, 0x00, 0x03])  # bcdHID=0x0111, country=0, flags=0x03

    # HID Control Point (2A4C): WRITE (encrypted)
    @characteristic("2A4C", CharFlags.WRITE | CharFlags.WRITE_WITHOUT_RESPONSE | CharFlags.ENCRYPT_WRITE)
    def hid_cp(self, _):
        return b""
    @hid_cp.setter
    def hid_cp_set(self, _value, _):
        pass

    # Report Map (2A4B): READ (encrypted)
    @characteristic("2A4B", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def report_map(self, _):
        return REPORT_MAP

    # Keyboard input (Report-mode, RID 1) — 8-byte payload
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_keyboard(self, _):
        return bytes([0,0,0,0,0,0,0,0])
    @input_keyboard.descriptor("2908", DescFlags.READ)
    def input_keyboard_ref(self, _):
        return bytes([RID_KEYBOARD, 0x01])

    # Consumer input (RID 2) — 2-byte payload (16-bit usage)
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_consumer(self, _):
        return bytes([0,0])
    @input_consumer.descriptor("2908", DescFlags.READ)
    def input_consumer_ref(self, _):
        return bytes([RID_CONSUMER, 0x01])

    # Boot Keyboard Input (2A22) — 8-byte payload (no report ID)
    @characteristic("2A22", CharFlags.READ | CharFlags.NOTIFY)
    def boot_keyboard_input(self, _):
        return bytes([0,0,0,0,0,0,0,0])

    # ---------------- Send helpers ---------------- 
    @staticmethod
    def _kb_payload(keys=(), modifiers=0) -> bytes:
        keys = list(keys)[:6] + [0] * (6 - len(keys))
        return bytes([modifiers, 0] + keys)  # 8-byte boot/report keyboard frame
    
    def send_keyboard(self, payload: bytes) -> None:
        if not self._link_ready:
            return
        try:
            # Protocol Mode: 0x01 = Report (default), 0x00 = Boot
            if getattr(self, "_proto", b"\x01")[0] == 0x01:
                self.input_keyboard.changed(payload)        # Report
            else:
                self.boot_keyboard_input.changed(payload)   # Boot
        except Exception:
            pass
    
    def send_consumer(self, payload: bytes) -> None:
        if not self._link_ready:
            return
        try:
            self.input_consumer.changed(payload)
        except Exception:
            pass

    async def key_tap(self, usage, hold_ms=40, modifiers=0):
        down = self._kb_payload([usage], modifiers)
        self.send_keyboard(down)
        await asyncio.sleep(hold_ms / 1000)
        up = self._kb_payload([], 0)
        self.send_keyboard(up)

    def cc_payload_usage(self, usage_id: int) -> bytes:
        return bytes([usage_id & 0xFF, (usage_id >> 8) & 0xFF])

    async def consumer_tap(self, usage_id, hold_ms=60):
        self.send_consumer(self.cc_payload_usage(usage_id))
        await asyncio.sleep(hold_ms/1000)
        self.send_consumer(self.cc_payload_usage(0))

    def release_all(self):
        self.send_keyboard(self._kb_payload([], 0))
        self.send_consumer(self.cc_payload_usage(0))

@dataclass
class HidRuntime:
    bus: any
    adapter: any
    adapter_name: str
    advert: any = None
    advert_path: str | None = None
    hid: any = None
    tasks: list | None = None
    connected: bool = False
    advertising: bool = False
    device_path: str | None = None
    peer_mac: str | None = None
    services_resolved: bool = False
    ready: bool = False
    connected_devices: set[str] = field(default_factory=set)
    advert_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

async def start_hid(config) -> tuple[HidRuntime, callable]:
    """
    Start the BLE HID server. Returns (runtime, shutdown) where shutdown is an async callable.
    - config.device_name   : BLE local name (string)
    - config.appearance    : GAP appearance (int, default 0x03C1)
    """
    import contextlib

    device_name = getattr(config, "device_name", None) or os.uname().nodename
    appearance  = int(getattr(config, "appearance", APPEARANCE))

    bus = await get_message_bus()
    if not await is_bluez_available(bus):
        raise RuntimeError("BlueZ not available on system DBus.")

    # Adapter
    adapter_name = getattr(config, "adapter_name", getattr(config, "adapter", "hci0"))
    try:
        xml = await bus.introspect("org.bluez", f"/org/bluez/{adapter_name}")
    except Exception as exc:
        raise RuntimeError(f"Bluetooth adapter {adapter_name} not found") from exc

    proxy = bus.get_proxy_object("org.bluez", f"/org/bluez/{adapter_name}", xml)
    adapter = Adapter(proxy)
    # Establish a stable baseline BEFORE we touch name/advertising.
    await ensure_controller_baseline(bus, adapter_name, adapter_proxy=proxy)
    await adapter.set_alias(device_name)

    # Agent
    agent = NoIoAgent()
    await agent.register(bus, default=True)

    await ensure_controller_baseline(bus, adapter_name, adapter_proxy=proxy)
    # Services
    dis = DeviceInfoService()
    bas = BatteryService(initial_level=100)
    hid = HIDService()

    global _hid_service_singleton
    _hid_service_singleton = hid

    app = ServiceCollection()
    app.add_service(dis)
    app.add_service(bas)
    app.add_service(hid)

    async def _power_cycle_adapter():
        """Toggle adapter power and then re-apply our baseline settings."""
        try:
            await adapter.set_powered(False)
            await asyncio.sleep(0.4)
            await adapter.set_powered(True)
            await asyncio.sleep(0.8)
        except Exception as e:
            logger.warning("[hid] Bluetooth adapter power-cycle failed: %s", e)
        # Power-cycling can reset Pairable/Discoverable; re-assert them immediately.
        await ensure_controller_baseline(bus, adapter_name, adapter_proxy=proxy)
        # Alias sometimes gets cleared after a controller reset on some stacks.
        with contextlib.suppress(Exception):
            await adapter.set_alias(device_name)

    # --- Register GATT application (with one retry) ---
    try:
        await app.register(bus, adapter=adapter)
    except Exception as e:
        logger.warning("[hid] BTLE service register failed: %s — retrying after power-cycle", e)
        await _power_cycle_adapter()
        await adapter.set_alias(device_name)
        try:
            await app.register(bus, adapter=adapter)
        except Exception as e2:
            raise RuntimeError(f"GATT application register failed after retry: {e2}") from e2

    runtime = HidRuntime(bus=bus, adapter=adapter, adapter_name=adapter_name, hid=hid, tasks=[])

    # --- Register advertising (with one retry + fresh advert object) ---
    await _cleanup_stale_adverts(bus, adapter_name)
    try:
        started = await _adv_register(runtime, config)
    except Exception as e:
        logger.warning("[hid] advert register failed: %s — retrying after power-cycle", e)
        with contextlib.suppress(Exception):
            await _adv_unregister(runtime)

        await _power_cycle_adapter()
        await adapter.set_alias(device_name)
        runtime.advert = None
        runtime.advert_path = None

        try:
            started = await _adv_register(runtime, config)
        except Exception as e2:
            with contextlib.suppress(Exception):
                await app.unregister()
            raise RuntimeError(f"Advertising register failed after retry: {e2}") from e2

    if started:
        # Don't leak the internal advertisement path in the default info log.  The
        # path is useful for debugging but can look odd in normal output.  If
        # debug logging is enabled, include the full path; otherwise emit a
        # concise message.  This mirrors how we log other lifecycle events.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[hid] advertising started %s", runtime.advert_path)
        logger.info("[hid] advertising started")

    # Watcher / startup sanity
    try:
        link_task = asyncio.create_task(watch_link(runtime, config))
        tasks = [link_task]
        runtime.tasks = tasks
        # Make sure we start from a clean HID state (harmless on first boot)
        with contextlib.suppress(Exception):
            hid.release_all()
    except Exception:
        # If anything fails after we registered the app/advertisement, clean up so we
        # don't leak advertisements (which leads to 'Maximum advertisements reached').
        with contextlib.suppress(Exception):
            await _adv_unregister(runtime)
        with contextlib.suppress(Exception):
            await app.unregister()
        with contextlib.suppress(Exception):
            hid.release_all()
        raise

    async def shutdown():
        """Cleanly tear down the BLE HID service and notify connected peers."""
        # Cancel the link watcher and any child tasks, allowing them to exit gracefully
        for t in list(tasks):
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)

        # Best‑effort disconnect any connected Device1 peers.  If we have active
        # connections when shutting down (e.g. container restart), proactively
        # request a disconnect so the central (Apple TV) sees the link drop.
        try:
            for path in list(runtime.connected_devices):
                try:
                    # Log a clean shutdown for each connected device.  Try to
                    # resolve the alias or name first; fall back to the MAC
                    # address.  The watch_link-local _device_label helper is
                    # unavailable here, so we use _get_device_alias_or_name()
                    # which returns alias or name or empty string.
                    label = ""
                    try:
                        # alias or name may still be available even during shutdown
                        label = await _get_device_alias_or_name(bus, path) or ""
                    except Exception:
                        label = ""
                    if not label:
                        try:
                            label = await _get_device_address(bus, path) or "unknown"
                        except Exception:
                            label = "unknown"
                    logger.info("[hid] disconnected %s (clean shutdown)", label)
                    xml = await bus.introspect("org.bluez", path)
                    dev_obj = bus.get_proxy_object("org.bluez", path, xml)
                    dev_iface = dev_obj.get_interface("org.bluez.Device1")
                    await dev_iface.call_disconnect()
                except Exception:
                    pass
        except Exception:
            pass

        # Unregister advertisement and GATT services.
        with contextlib.suppress(Exception):
            await _adv_unregister(runtime)

        with contextlib.suppress(Exception):
            await app.unregister()

        # Release any active HID reports so the host sees no stuck keys
        with contextlib.suppress(Exception):
            hid.release_all()

        hid._link_ready = False

    return runtime, shutdown
