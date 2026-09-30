"""Status reporting: the /health payload and the compact /api/status.

/health keeps its long-standing shape (existing consumers read it).
/api/status is the compact one for the dashboard and Home Assistant: one level
of nesting, plain values, only what's useful at a glance.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import time
from typing import Any

THROTTLED_SYSFS = "/sys/devices/platform/soc/soc:firmware/get_throttled"
THROTTLING_CACHE_S = 60.0
HWMON_DIR = "/sys/class/hwmon"
POWER_SUPPLY_DIR = "/sys/class/power_supply"
BUILD_DATE_FILE = "/app/BUILD_DATE"

_SECRET_RE = re.compile(r"(token=)[^&\s'\"]+", re.IGNORECASE)


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


class StatusReporter:
    """Builds status from each domain's own snapshot/status."""

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
        self._room_name = room_name or socket.gethostname()
        self._rooms = [
            {"name": name, "url": f"http://{host}:{http_port}"} for name, host in (rooms or [])
        ]
        self._ble = ble
        self._reader = reader
        self._tv = tv
        self._speaker = speaker
        self._runtime = runtime
        self._dispatcher = dispatcher
        self._process_start_monotonic = time.monotonic()
        self._throttling_cache: tuple[float, dict[str, Any]] | None = None
        self._build_date = build_date()
        # Last level seen: the remote only reports while it's awake.
        self._remote_battery: str | None = None

    @staticmethod
    def _domain_status(*, configured: bool, enabled: bool, degraded: bool) -> str:
        if not configured or not enabled:
            return "disabled"
        return "degraded" if degraded else "ok"

    def _mk_domain_state(
        self,
        *,
        status: str,
        configured: bool,
        enabled: bool,
        reasons: list[str],
        present: bool,
        link_up: bool,
        link_ready: bool,
        error: bool,
        last_error: str | None,
        details: dict[str, Any],
        path: str | None = None,
    ) -> dict[str, Any]:
        state = {
            "status": status,
            "configured": configured,
            "enabled": enabled,
            "reasons": reasons,
            "present": present,
            "link_up": link_up,
            "link_ready": link_ready,
            "error": error,
            "last_error": last_error,
            "details": details,
        }
        if path is not None:
            state["path"] = path
        return state

    def _usb_health_state(self) -> dict[str, Any]:
        if self._reader is None:
            return self._mk_domain_state(
                status="disabled",
                configured=False,
                enabled=False,
                reasons=[],
                present=False,
                link_up=False,
                link_ready=False,
                error=False,
                last_error=None,
                details={},
                path=None,
            )

        usb_raw = self._reader.status
        usb_configured = True
        usb_enabled = True
        usb_present = bool(usb_raw.get("receiver_present"))
        usb_path = usb_raw.get("input_path")
        usb_link_up = bool(usb_raw.get("input_open"))
        usb_link_ready = bool(
            usb_raw.get("input_open")
            and usb_raw.get("reader_running")
            and usb_raw.get("grabbed")
            and usb_raw.get("paired_remote")
        )

        usb_last_error = _norm_error(usb_raw.get("last_error"))
        usb_display_error = None if usb_last_error == "no_input_device" else usb_last_error
        usb_error = bool(usb_raw.get("error")) if "error" in usb_raw else False

        if usb_last_error in {"device_disconnected errno=19", "device_disconnected errno=5"}:
            usb_error = False

        usb_reasons: list[str] = []
        if not usb_present:
            usb_reasons.append("usb.receiver_missing")
        else:
            if not bool(usb_raw.get("paired_remote")):
                usb_reasons.append("usb.no_paired_remote")
            if not bool(usb_raw.get("reader_running")):
                usb_reasons.append("usb.reader_not_running")
            if not bool(usb_raw.get("input_open")):
                usb_reasons.append("usb.input_not_open")
            if not bool(usb_raw.get("grabbed")):
                usb_reasons.append("usb.not_grabbed")

        if usb_error and usb_last_error:
            usb_reasons.append("usb.error")

        return self._mk_domain_state(
            status=self._domain_status(
                configured=usb_configured,
                enabled=usb_enabled,
                degraded=bool(usb_reasons),
            ),
            configured=usb_configured,
            enabled=usb_enabled,
            reasons=usb_reasons,
            present=usb_present,
            link_up=usb_link_up,
            link_ready=usb_link_ready,
            error=usb_error,
            last_error=usb_display_error,
            details={
                "paired_remote": bool(usb_raw.get("paired_remote")),
                "reader_running": bool(usb_raw.get("reader_running")),
                "input_open": bool(usb_raw.get("input_open")),
                "grabbed": bool(usb_raw.get("grabbed")),
            },
            path=usb_path,
        )

    def _ble_health_state(self) -> dict[str, Any]:
        if self._ble is None:
            return self._mk_domain_state(
                status="disabled",
                configured=False,
                enabled=False,
                reasons=[],
                present=False,
                link_up=False,
                link_ready=False,
                error=False,
                last_error=None,
                details={},
                path=None,
            )

        ble_raw = self._ble.status
        conn_params = ble_raw.get("conn_params") or {}

        ble_configured = True
        ble_enabled = True
        ble_present = bool(ble_raw.get("adapter_present"))
        ble_path = ble_raw.get("active_port")
        ble_transport_up = bool(ble_raw.get("transport_open"))
        ble_connected = bool(ble_raw.get("connected"))
        ble_advertising = bool(ble_raw.get("advertising"))
        ble_link_ready = bool(ble_raw.get("ready"))
        ble_link_up = bool(ble_transport_up or ble_connected or ble_link_ready)
        ble_last_error = _norm_error(ble_raw.get("last_error"))
        ble_display_error = ble_last_error
        ble_error = bool(ble_raw.get("error")) if "error" in ble_raw else False

        if not ble_present and ble_last_error:
            ble_display_error = None
            ble_error = False

        ble_reasons: list[str] = []
        if not ble_present:
            ble_reasons.append("ble.dongle_missing")
        else:
            if ble_link_ready:
                pass
            elif not ble_transport_up:
                ble_reasons.append("ble.transport_down")
            elif ble_connected:
                ble_reasons.append("ble.connected_not_ready")
            elif ble_advertising:
                ble_reasons.append("ble.advertising")
            else:
                ble_reasons.append("ble.idle")

        if ble_error and ble_last_error:
            ble_reasons.append("ble.error")

        return self._mk_domain_state(
            status=self._domain_status(
                configured=ble_configured,
                enabled=ble_enabled,
                degraded=bool(ble_reasons),
            ),
            configured=ble_configured,
            enabled=ble_enabled,
            reasons=ble_reasons,
            present=ble_present,
            link_up=ble_link_up,
            link_ready=ble_link_ready,
            error=ble_error,
            last_error=ble_display_error,
            details={
                "transport_open": ble_transport_up,
                "advertising": ble_advertising,
                "connected": ble_connected,
                "proto_report": ble_raw.get("proto_report"),
                "last_disc_reason": ble_raw.get("last_disc_reason"),
                "conn_params": conn_params or None,
            },
            path=ble_path,
        )

    def _tv_health_state(self) -> dict[str, Any]:
        if self._tv is None:
            return self._mk_domain_state(
                status="disabled",
                configured=False,
                enabled=False,
                reasons=[],
                present=False,
                link_up=False,
                link_ready=False,
                error=False,
                last_error=None,
                details={},
            )

        s = self._tv.snapshot()

        tv_configured = True
        tv_enabled = True
        tv_present = s.presence_on is not None
        tv_link_up = bool(s.presence_on is True)
        is_frame = s.backend == "samsung_frame_ip"
        # Frame: IP control is always reachable, so a token is all it needs.
        # Legacy: the websocket is the control path.
        control_ready = bool(s.token_present) if is_frame else bool(s.ws_connected)
        tv_link_ready = bool(control_ready and s.presence_on is True)
        tv_last_error = _mask_secrets(_norm_error(s.last_error))
        # A legacy websocket that can't connect while the TV is off is expected.
        tv_error = bool(tv_last_error) and (is_frame or s.presence_on is True)

        tv_reasons: list[str] = []
        if not bool(s.token_present):
            tv_reasons.append("tv.token_missing")
        if not tv_present:
            tv_reasons.append("tv.presence_unknown")
        elif tv_link_up and not tv_link_ready and not is_frame:
            tv_reasons.append("tv.ws_not_ready")
        if tv_error:
            tv_reasons.append("tv.error")

        return self._mk_domain_state(
            status=self._domain_status(
                configured=tv_configured,
                enabled=tv_enabled,
                degraded=bool(tv_reasons),
            ),
            configured=tv_configured,
            enabled=tv_enabled,
            reasons=tv_reasons,
            present=tv_present,
            link_up=tv_link_up,
            link_ready=tv_link_ready,
            error=tv_error,
            last_error=tv_last_error,
            details={
                "backend": "samsung_frame_ip" if is_frame else "samsung_ws",
                "initialised": bool(s.initialised),
                "presence_on": s.presence_on,
                "presence_source": s.presence_source,
                "last_change_age_s": s.last_change_age_s,
                "changed_at": s.changed_at,
                "ws_connected": bool(s.ws_connected),
                "token_present": bool(s.token_present),
            },
        )

    def _speaker_health_state(self) -> dict[str, Any]:
        if self._speaker is None or not self._speaker.enabled:
            return self._mk_domain_state(
                status="disabled",
                configured=bool(self._speaker is not None),
                enabled=False,
                reasons=[],
                present=False,
                link_up=False,
                link_ready=False,
                error=False,
                last_error=None,
                details={},
            )

        snap = self._speaker.snapshot()
        sstate = self._speaker.state

        reachable = bool(sstate.reachable)
        connected = bool(sstate.connected)
        ready = bool(sstate.ready)
        speaker_last_error = _norm_error(sstate.last_error)
        speaker_error = bool(speaker_last_error)

        speaker_configured = True
        speaker_enabled = True
        sp_present = reachable
        sp_link_up = connected
        sp_link_ready = ready

        sp_reasons: list[str] = []
        if not reachable:
            sp_reasons.append("speaker.not_reachable")
        elif not connected:
            sp_reasons.append("speaker.not_connected")
        elif not ready:
            sp_reasons.append("speaker.not_ready")
        if speaker_error:
            sp_reasons.append("speaker.error")

        return self._mk_domain_state(
            status=self._domain_status(
                configured=speaker_configured,
                enabled=speaker_enabled,
                degraded=bool(sp_reasons),
            ),
            configured=speaker_configured,
            enabled=speaker_enabled,
            reasons=sp_reasons,
            present=sp_present,
            link_up=sp_link_up,
            link_ready=sp_link_ready,
            error=speaker_error,
            last_error=speaker_last_error,
            details=dict(snap),
        )

    @staticmethod
    def _read_system_uptime_s() -> float | None:
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                first = f.read().strip().split()[0]
            return float(first)
        except Exception:
            return None

    @staticmethod
    def _format_duration(seconds: float | int | None) -> str | None:
        if seconds is None:
            return None
        total = int(seconds)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        if days:
            return f"{days}d {hours:02}:{minutes:02}:{secs:02}"
        return f"{hours:02}:{minutes:02}:{secs:02}"

    @staticmethod
    def _read_meminfo() -> dict[str, int]:
        out: dict[str, int] = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    key, rest = line.split(":", 1)
                    parts = rest.strip().split()
                    if not parts:
                        continue
                    try:
                        out[key] = int(parts[0]) * 1024  # kB -> bytes
                    except Exception:
                        continue
        except Exception:
            pass
        return out

    @staticmethod
    def _fmt_bytes(n: int | None) -> str | None:
        if n is None:
            return None
        value = float(n)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024.0 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024.0
        return None
    
    @staticmethod
    def _read_cpu_temp_c() -> float | None:
        candidates = [
            "/sys/class/thermal/thermal_zone0/temp",
        ]

        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                value = int(raw)
                # Raspberry Pi thermal_zone temp is usually millidegrees C
                if value > 1000:
                    return round(value / 1000.0, 1)
                return round(float(value), 1)
            except Exception:
                continue

        return None
    
    @staticmethod
    def _primary_ip() -> str | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return str(s.getsockname()[0])
            finally:
                s.close()
        except Exception:
            try:
                ip = socket.gethostbyname(socket.gethostname())
                return None if ip.startswith("127.") else ip
            except Exception:
                return None

    def _read_throttling(self) -> dict:
        """Pi power health, from sysfs (vcgencmd isn't in the container).

        Newer firmware exposes the full throttling bitmask; otherwise the
        rpi_volt sensor's alarm still reports undervoltage, the one that
        matters most. Cached for a minute: this runs on every status poll.
        """
        now = time.monotonic()
        if self._throttling_cache is not None and now - self._throttling_cache[0] < THROTTLING_CACHE_S:
            return self._throttling_cache[1]

        data: dict[str, Any] = {"available": False, "raw": None, "status": "unknown"}
        try:
            with open(THROTTLED_SYSFS, "r", encoding="utf-8") as f:
                value = int(f.read().strip(), 16)
            data = {
                "available": True,
                "raw": hex(value),
                "undervoltage_now": bool(value & (1 << 0)),
                "freq_capped_now": bool(value & (1 << 1)),
                "throttled_now": bool(value & (1 << 2)),
                "temp_limit_now": bool(value & (1 << 3)),
                "undervoltage_occurred": bool(value & (1 << 16)),
                "freq_capped_occurred": bool(value & (1 << 17)),
                "throttled_occurred": bool(value & (1 << 18)),
                "temp_limit_occurred": bool(value & (1 << 19)),
                "status": "warning" if value else "ok",
            }
        except (OSError, ValueError):
            alarm = _read_rpi_volt_alarm()
            if alarm is not None:
                data = {
                    "available": True,
                    "raw": None,
                    "undervoltage_now": alarm,
                    "status": "warning" if alarm else "ok",
                }

        self._throttling_cache = (now, data)
        return data

    def _system_snapshot(self) -> dict:
        hostname = socket.gethostname()
        primary_ip = self._primary_ip()
        
        system_uptime_s = self._read_system_uptime_s()
        process_uptime_s = time.monotonic() - self._process_start_monotonic
        cpu_temp_c = self._read_cpu_temp_c()
        throttling = self._read_throttling()

        try:
            load1, load5, load15 = os.getloadavg()
            load = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
        except Exception:
            load = {"1m": None, "5m": None, "15m": None}

        meminfo = self._read_meminfo()
        mem_total = meminfo.get("MemTotal")
        mem_available = meminfo.get("MemAvailable")
        mem_used = (mem_total - mem_available) if (mem_total is not None and mem_available is not None) else None

        try:
            disk = shutil.disk_usage("/")
            disk_total = disk.total
            disk_used = disk.used
            disk_free = disk.free
        except Exception:
            disk_total = disk_used = disk_free = None

        return {
            "hostname": hostname,
            "primary_ip": primary_ip,
            "system_uptime_s": int(system_uptime_s) if system_uptime_s is not None else None,
            "system_uptime_human": self._format_duration(system_uptime_s),
            "process_uptime_s": int(process_uptime_s),
            "process_uptime_human": self._format_duration(process_uptime_s),
            "cpu_temp_c": cpu_temp_c,
            "throttling": throttling,
            "load": load,
            "memory": {
                "total_bytes": mem_total,
                "available_bytes": mem_available,
                "used_bytes": mem_used,
                "total_human": self._fmt_bytes(mem_total),
                "available_human": self._fmt_bytes(mem_available),
                "used_human": self._fmt_bytes(mem_used),
            },
            "disk": {
                "path": "/",
                "total_bytes": disk_total,
                "used_bytes": disk_used,
                "free_bytes": disk_free,
                "total_human": self._fmt_bytes(disk_total),
                "used_human": self._fmt_bytes(disk_used),
                "free_human": self._fmt_bytes(disk_free),
            },
        }

    def health(self) -> dict:
        """The full /health payload (shape kept stable for existing consumers)."""
        pihub_id = socket.gethostname()
        system_state = self._system_snapshot()

        runtime_state = (
            self._runtime.snapshot()
            if self._runtime is not None
            else {
                "mode": None,
                "last_flow": None,
                "flow_running": False,
                "last_trigger": None,
                "error": False,
                "last_error": None,
                "last_result": None,
            }
        )

        usb_state = self._usb_health_state()
        ble_state = self._ble_health_state()
        tv_state = self._tv_health_state()
        speaker_state = self._speaker_health_state()

        degraded_reasons: list[str] = []

        if usb_state["status"] == "degraded":
            degraded_reasons.extend(usb_state["reasons"])

        if ble_state["status"] == "degraded":
            degraded_reasons.extend(ble_state["reasons"])

        if tv_state["status"] == "degraded":
            degraded_reasons.extend(
                reason
                for reason in tv_state["reasons"]
                if reason != "tv.presence_unknown"
            )

        if speaker_state["status"] == "degraded":
            degraded_reasons.extend(speaker_state["reasons"])

        runtime_error = _norm_error(runtime_state.get("last_error") or runtime_state.get("error"))
        if runtime_state.get("error"):
            degraded_reasons.append(
                f"runtime.error:{runtime_error}" if runtime_error else "runtime.error"
            )

        status = "ok" if not degraded_reasons else "degraded"

        domains = {
            "usb": usb_state["status"],
            "ble": ble_state["status"],
            "tv": tv_state["status"],
            "speaker": speaker_state["status"],
        }

        ha = {
            "overall_status": status,
            "current_mode": runtime_state.get("mode"),
            "last_flow": runtime_state.get("last_flow"),
            "flow_running": runtime_state.get("flow_running"),
            "last_result": runtime_state.get("last_result"),
            "last_error": runtime_state.get("last_error"),
            "degraded_reasons": degraded_reasons,
            "binary_sensors": {
                "runtime_error": bool(runtime_state.get("error")),
                "flow_running": bool(runtime_state.get("flow_running")),
                "ble_ready": bool(ble_state.get("link_ready")),
                "tv_on": (tv_state.get("details") or {}).get("presence_on"),
                "speaker_ready": bool(speaker_state.get("link_ready")),
            },
            "domains": {
                "usb": usb_state.get("status"),
                "ble": ble_state.get("status"),
                "tv": tv_state.get("status"),
                "speaker": speaker_state.get("status"),
            },
        }

        return {
            "pihub_id": pihub_id,
            "status": status,
            "degraded_reasons": degraded_reasons,
            "domains": domains,
            "runtime": runtime_state,
            "usb": usb_state,
            "ble": ble_state,
            "tv": tv_state,
            "speaker": speaker_state,
            "system": system_state,
            "ha": ha,
        }

    def _remote_battery_level(self) -> str | None:
        level = _read_remote_battery_level()
        if level is not None:
            self._remote_battery = level
        return self._remote_battery

    def api_status(self) -> dict[str, Any]:
        """Compact status for the dashboard and Home Assistant.

        Mode and triggers at the top level; one small block per device; system
        vitals. Values are plain (numbers, strings, booleans, null). Times
        (*_at) are Unix seconds, set only when that thing actually changed.
        """
        h = self.health()
        rt = h["runtime"]
        tv, sp, ble, usb, sysd = h["tv"], h["speaker"], h["ble"], h["usb"], h["system"]
        tvd, spd = tv.get("details") or {}, sp.get("details") or {}
        bled, usbd = ble.get("details") or {}, usb.get("details") or {}
        mem = sysd.get("memory") or {}
        mem_pct = None
        if mem.get("total_bytes") and mem.get("used_bytes") is not None:
            mem_pct = round(100.0 * mem["used_bytes"] / mem["total_bytes"])
        throttling = sysd.get("throttling") or {}

        return {
            "name": self._room_name,
            "rooms": self._rooms,
            "host": sysd.get("hostname"),
            "ip": sysd.get("primary_ip"),
            "built": self._build_date,
            "status": h["status"],
            "problems": h["degraded_reasons"],
            "mode": rt.get("mode"),
            "flow_running": bool(rt.get("flow_running")),
            "last_flow": rt.get("last_flow"),
            "last_trigger": rt.get("last_trigger"),
            "last_trigger_source": _trigger_source(rt.get("last_trigger")),
            "last_trigger_at": rt.get("last_trigger_at"),
            "last_result": rt.get("last_result"),
            "last_error": rt.get("last_error"),
            "tv": {
                "backend": "frame" if tvd.get("backend") == "samsung_frame_ip" else ("samsung" if tvd else None),
                "state": tv["status"],
                "on": tvd.get("presence_on"),
                "on_via": tvd.get("presence_source"),
                "changed_at": tvd.get("changed_at"),
                # Can PiHub send commands right now, whether or not the TV is on.
                # Legacy: the websocket is up (a key over it also wakes a TV that is
                # still lingering after switch-off). Frame: IP control has its token.
                "control_ready": bool(
                    tvd.get("token_present") if tvd.get("backend") == "samsung_frame_ip" else tvd.get("ws_connected")
                ),
                "error": tv.get("last_error") if tv.get("error") else None,
            },
            "speaker": {
                "backend": spd.get("backend"),
                "state": sp["status"],
                "source": spd.get("source"),
                "playback": spd.get("playback_status"),
                "volume": spd.get("volume_pct"),
                "muted": spd.get("muted"),
                "detail": spd.get("source_detail"),
                "changed_at": spd.get("changed_at"),
                "error": sp.get("last_error"),
                # The last speaker key press that failed (not a speaker fault).
                "key_error": self._dispatcher.last_key_error if self._dispatcher is not None else None,
            },
            "apple_tv": {
                "state": ble["status"],
                "connected": bool(ble.get("link_ready")),
                "advertising": bool(bled.get("advertising")),
                "dongle": bool(ble.get("present")),
                "interval_ms": (bled.get("conn_params") or {}).get("interval_ms"),
                "last_disconnect_reason": bled.get("last_disc_reason"),
            },
            "remote": {
                "state": usb["status"],
                "receiver": bool(usb.get("present")),
                "paired": bool(usbd.get("paired_remote")),
                "input_open": bool(usbd.get("input_open")),
                "grabbed": bool(usbd.get("grabbed")),
                "battery": self._remote_battery_level(),
            },
            "system": {
                "cpu_temp_c": sysd.get("cpu_temp_c"),
                "load_1m": (sysd.get("load") or {}).get("1m"),
                "memory_used_pct": mem_pct,
                "uptime_s": sysd.get("system_uptime_s"),
                "pihub_uptime_s": sysd.get("process_uptime_s"),
                "throttled": (throttling.get("status") == "warning") if throttling.get("available") else None,
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
