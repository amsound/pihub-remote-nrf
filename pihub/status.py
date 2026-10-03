"""Status reporting: /api/status, for the web pages and Home Assistant.

One level of nesting, plain values, only what's useful. Each device block has a
"state" (ok / degraded / disabled); the reasons behind any "degraded" are
collected in "problems".
"""

from __future__ import annotations

import os
import re
import socket
import time
from typing import Any

from .tv import BACKEND_FRAME

THROTTLED_SYSFS = "/sys/devices/platform/soc/soc:firmware/get_throttled"
HWMON_DIR = "/sys/class/hwmon"
POWER_SUPPLY_DIR = "/sys/class/power_supply"
BUILD_DATE_FILE = "/app/BUILD_DATE"
# These barely change, and status is asked for every couple of seconds per open page.
SLOW_CACHE_S = 60.0

_SECRET_RE = re.compile(r"(token=)[^&\s'\"]+", re.IGNORECASE)
_REMOTE_UNPLUGGED = {"device_disconnected errno=19", "device_disconnected errno=5"}


def _read_rpi_volt_alarm() -> bool | None:
    """Undervoltage alarm from the rpi_volt hwmon sensor, if present."""
    try:
        for name in os.listdir(HWMON_DIR):
            base = os.path.join(HWMON_DIR, name)
            try:
                with open(os.path.join(base, "name"), encoding="utf-8") as f:
                    if f.read().strip() != "rpi_volt":
                        continue
                with open(os.path.join(base, "in0_lcrit_alarm"), encoding="utf-8") as f:
                    return f.read().strip() == "1"
            except OSError:
                continue
    except OSError:
        pass
    return None


def _read_throttled() -> bool | None:
    """Pi power trouble, from sysfs (vcgencmd isn't in the container). None if it can't be read.

    Newer firmware exposes the full throttling bitmask (any bit set counts,
    including "has happened since boot"); otherwise the rpi_volt sensor's alarm
    still reports undervoltage, the one that matters most.
    """
    try:
        with open(THROTTLED_SYSFS, encoding="utf-8") as f:
            return bool(int(f.read().strip(), 16))
    except (OSError, ValueError):
        return _read_rpi_volt_alarm()


def _read_remote_battery_level() -> str | None:
    """The Harmony remote's battery level (Logitech HID++): full/high/normal/low/critical.

    None while the remote sleeps (the kernel then reports "Unknown").
    """
    try:
        names = [n for n in os.listdir(POWER_SUPPLY_DIR) if n.startswith("hidpp_battery")]
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(POWER_SUPPLY_DIR, name, "capacity_level"), encoding="utf-8") as f:
                level = f.read().strip().lower()
        except OSError:
            continue
        if level and level != "unknown":
            return level
    return None


def _read_uptime_s() -> int | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _read_cpu_temp_c() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as f:
            value = int(f.read().strip())
    except (OSError, ValueError):
        return None
    # Usually millidegrees C.
    return round(value / 1000.0, 1) if value > 1000 else round(float(value), 1)


def _read_memory_used_pct() -> int | None:
    total = available = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        return None
    if not total or available is None:
        return None
    return round(100.0 * (total - available) / total)


def _read_load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def _primary_ip() -> str | None:
    """This Pi's LAN address (the source address it would use to reach the internet)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # UDP: nothing is sent
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            return None if ip.startswith("127.") else ip
        except OSError:
            return None


def build_date() -> str:
    """When this image was built (written by the Dockerfile), or 'unknown'."""
    try:
        with open(BUILD_DATE_FILE, encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _norm_error(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _mask_secrets(text: str | None) -> str | None:
    """Hide tokens that end up inside error messages (e.g. websocket URLs)."""
    return _SECRET_RE.sub(r"\1***", text) if text else text


def _state(reasons: list[str]) -> str:
    return "degraded" if reasons else "ok"


class StatusReporter:
    """Builds /api/status from each domain's own snapshot/status."""

    def __init__(
        self,
        *,
        ble: Any = None,
        reader: Any = None,
        tv: Any = None,
        speaker: Any = None,
        runtime: Any = None,
        dispatcher: Any = None,
        room_name: str = "",
        rooms: list[tuple[str, str]] | None = None,
        http_port: int = 9123,
    ) -> None:
        self._hostname = socket.gethostname()
        self._room_name = room_name or self._hostname
        self._rooms = [
            {"name": name, "url": f"http://{host}:{http_port}"} for name, host in (rooms or [])
        ]
        self._ble = ble
        self._reader = reader
        self._tv = tv
        self._speaker = speaker
        self._runtime = runtime
        self._dispatcher = dispatcher
        self._started = time.monotonic()
        self._build_date = build_date()
        # (read at, primary ip, throttled)
        self._slow: tuple[float, str | None, bool | None] | None = None
        # Last level seen: the remote only reports while it's awake.
        self._remote_battery: str | None = None

    # ---- one block per device: (block, problems) ----

    def _remote(self) -> tuple[dict[str, Any], list[str]]:
        battery = _read_remote_battery_level()
        if battery is not None:
            self._remote_battery = battery
        if self._reader is None:
            return {
                "state": "disabled", "receiver": False, "paired": False, "input_open": False,
                "grabbed": False, "battery": self._remote_battery,
            }, []

        raw = self._reader.status
        receiver = bool(raw.get("receiver_present"))
        paired = bool(raw.get("paired_remote"))
        input_open = bool(raw.get("input_open"))
        grabbed = bool(raw.get("grabbed"))
        last_error = _norm_error(raw.get("last_error"))

        reasons: list[str] = []
        if not receiver:
            reasons.append("usb.receiver_missing")
        else:
            if not paired:
                reasons.append("usb.no_paired_remote")
            if not raw.get("reader_running"):
                reasons.append("usb.reader_not_running")
            if not input_open:
                reasons.append("usb.input_not_open")
            if not grabbed:
                reasons.append("usb.not_grabbed")
        # Unplugging the receiver is not a fault in itself.
        if raw.get("error") and last_error and last_error not in _REMOTE_UNPLUGGED:
            reasons.append("usb.error")

        return {
            "state": _state(reasons), "receiver": receiver, "paired": paired, "input_open": input_open,
            "grabbed": grabbed, "battery": self._remote_battery,
        }, reasons

    def _apple_tv(self) -> tuple[dict[str, Any], list[str]]:
        if self._ble is None:
            return {
                "state": "disabled", "connected": False, "advertising": False, "dongle": False,
                "interval_ms": None, "last_disconnect_reason": None, "dongle_firmware": None,
            }, []

        raw = self._ble.status
        dongle = bool(raw.get("adapter_present"))
        ready = bool(raw.get("ready"))
        advertising = bool(raw.get("advertising"))

        reasons: list[str] = []
        if not dongle:
            reasons.append("ble.dongle_missing")
        elif not ready:
            if not raw.get("transport_open"):
                reasons.append("ble.transport_down")
            elif raw.get("connected"):
                reasons.append("ble.connected_not_ready")
            elif advertising:
                reasons.append("ble.advertising")
            else:
                reasons.append("ble.idle")
        # With no dongle there is nothing to be in error.
        if dongle and raw.get("error") and _norm_error(raw.get("last_error")):
            reasons.append("ble.error")

        return {
            "state": _state(reasons),
            "connected": ready,
            "advertising": advertising,
            "dongle": dongle,
            "interval_ms": (raw.get("conn_params") or {}).get("interval_ms"),
            "last_disconnect_reason": raw.get("last_disc_reason"),
            "dongle_firmware": raw.get("firmware"),
        }, reasons

    def _tv_block(self) -> tuple[dict[str, Any], list[str]]:
        if self._tv is None:
            return {
                "backend": None, "state": "disabled", "on": None, "on_via": None, "changed_at": None,
                "control_ready": False, "error": None,
            }, []

        s = self._tv.snapshot()
        is_frame = s.backend == BACKEND_FRAME
        on = s.presence_on is True
        # Can PiHub send commands right now, whether or not the TV is on.
        # Legacy: the websocket is up (a key over it also wakes a TV that is
        # still lingering after switch-off). Frame: IP control has its token.
        control_ready = bool(s.token_present) if is_frame else bool(s.ws_connected)
        last_error = _mask_secrets(_norm_error(s.last_error))
        # A legacy websocket that can't connect while the TV is off is expected.
        error = bool(last_error) and (is_frame or on)

        reasons: list[str] = []
        if not s.token_present:
            reasons.append("tv.token_missing")
        if s.presence_on is None:
            reasons.append("tv.presence_unknown")
        elif on and not control_ready and not is_frame:
            reasons.append("tv.ws_not_ready")
        if error:
            reasons.append("tv.error")

        block = {
            "backend": "frame" if is_frame else "samsung",
            "state": _state(reasons),
            "on": s.presence_on,
            "on_via": s.presence_source,
            "changed_at": s.changed_at,
            "control_ready": control_ready,
            "error": last_error if error else None,
        }
        # Not having seen the TV yet (fresh start) marks it degraded but is not a problem to report.
        return block, [r for r in reasons if r != "tv.presence_unknown"]

    def _speaker_block(self) -> tuple[dict[str, Any], list[str]]:
        key_error = self._dispatcher.last_key_error if self._dispatcher is not None else None
        if self._speaker is None or not self._speaker.enabled:
            return {
                "backend": None, "state": "disabled", "on": None, "on_changed_at": None,
                "source": None, "playback": None,
                "volume": None, "muted": None, "detail": None, "changed_at": None, "error": None,
                "key_error": key_error,
            }, []

        snap = self._speaker.snapshot()
        st = self._speaker.state
        last_error = _norm_error(st.last_error)

        reasons: list[str] = []
        if not st.reachable:
            reasons.append("speaker.not_reachable")
        elif not st.connected:
            reasons.append("speaker.not_connected")
        elif not st.ready:
            reasons.append("speaker.not_ready")
        if last_error:
            reasons.append("speaker.error")

        return {
            "backend": snap.get("backend"),
            "state": _state(reasons),
            # Powered on or off, where the speaker can tell us (Audio Pro); None otherwise.
            "on": snap.get("powered_on"),
            "on_changed_at": snap.get("powered_changed_at"),
            "source": snap.get("source"),
            "playback": snap.get("playback_status"),
            "volume": snap.get("volume_pct"),
            "muted": snap.get("muted"),
            "detail": snap.get("source_detail"),
            "changed_at": snap.get("changed_at"),
            "error": last_error,
            # The last speaker key press that failed (not a speaker fault).
            "key_error": key_error,
        }, reasons

    def _slow_values(self) -> tuple[str | None, bool | None]:
        now = time.monotonic()
        if self._slow is None or now - self._slow[0] >= SLOW_CACHE_S:
            self._slow = (now, _primary_ip(), _read_throttled())
        return self._slow[1], self._slow[2]

    # ---- the payload ----

    def api_status(self) -> dict[str, Any]:
        """Compact status for the web pages and Home Assistant.

        Mode and triggers at the top level; one small block per device; system
        vitals. Values are plain (numbers, strings, booleans, null). Times
        (*_at) are Unix seconds, set only when that thing actually changed.
        """
        rt = self._runtime.snapshot() if self._runtime is not None else {}
        remote, remote_problems = self._remote()
        apple_tv, apple_tv_problems = self._apple_tv()
        tv, tv_problems = self._tv_block()
        speaker, speaker_problems = self._speaker_block()

        problems = remote_problems + apple_tv_problems + tv_problems + speaker_problems
        if rt.get("error"):
            runtime_error = _norm_error(rt.get("last_error") or rt.get("error"))
            problems.append(f"runtime.error:{runtime_error}" if runtime_error else "runtime.error")

        ip, throttled = self._slow_values()
        return {
            "name": self._room_name,
            "rooms": self._rooms,
            "host": self._hostname,
            "ip": ip,
            "built": self._build_date,
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "mode": rt.get("mode"),
            "flow_running": bool(rt.get("flow_running")),
            "last_flow": rt.get("last_flow"),
            "last_trigger": rt.get("last_trigger"),
            "last_trigger_source": _trigger_source(rt.get("last_trigger")),
            "last_trigger_at": rt.get("last_trigger_at"),
            "last_result": rt.get("last_result"),
            "last_error": rt.get("last_error"),
            "tv": tv,
            "speaker": speaker,
            "apple_tv": apple_tv,
            "remote": remote,
            "system": {
                "cpu_temp_c": _read_cpu_temp_c(),
                "load_1m": _read_load_1m(),
                "memory_used_pct": _read_memory_used_pct(),
                "uptime_s": _read_uptime_s(),
                "pihub_uptime_s": int(time.monotonic() - self._started),
                "throttled": throttled,
            },
        }


def _trigger_source(trigger: object) -> str | None:
    """remote / device / web / startup / ... from a trigger like 'remote.rem_mode_2'."""
    t = str(trigger or "").strip().lower()
    if not t:
        return None
    if t.startswith("remote."):
        return "remote"
    if t.startswith("device_state_change."):
        return "device"
    if t.startswith("http."):
        return "web"
    if t.startswith("startup"):
        return "startup"
    if t.startswith("flow."):
        return "flow"
    return t
