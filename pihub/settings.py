"""Persistent user-editable PiHub settings."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from threading import RLock

from .slots import SLOT_COUNT, is_tunein_source


DEFAULT_SETTINGS_PATH = "/data/settings.json"


@dataclass
class SettingsData:
    watch_volume_pct: int = 0
    listen_volume_pct: int = 0
    listen_target_type: str = "preset"
    listen_target_preset: int = 1
    listen_target_stream: int = 1
    stream_url_1: str = ""
    stream_url_2: str = ""
    stream_url_3: str = ""
    stream_url_4: str = ""

    # Samsung soundbar: one stream per number key (slot 10 is key 0). Ticking
    # restream sends it via the local restreamer (needed for HLS and playlists).
    soundbar_stream_url_1: str = ""
    soundbar_stream_url_2: str = ""
    soundbar_stream_url_3: str = ""
    soundbar_stream_url_4: str = ""
    soundbar_stream_url_5: str = ""
    soundbar_stream_url_6: str = ""
    soundbar_stream_url_7: str = ""
    soundbar_stream_url_8: str = ""
    soundbar_stream_url_9: str = ""
    soundbar_stream_url_10: str = ""
    soundbar_restream_1: bool = False
    soundbar_restream_2: bool = False
    soundbar_restream_3: bool = False
    soundbar_restream_4: bool = False
    soundbar_restream_5: bool = False
    soundbar_restream_6: bool = False
    soundbar_restream_7: bool = False
    soundbar_restream_8: bool = False
    soundbar_restream_9: bool = False
    soundbar_restream_10: bool = False


class SettingsStore:
    def __init__(self, path: str = DEFAULT_SETTINGS_PATH) -> None:
        self._path = path
        self._lock = RLock()
        self._data = SettingsData()

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> None:
        with self._lock:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except FileNotFoundError:
                self._data = SettingsData()
                return
            except Exception:
                self._data = SettingsData()
                return

            self._data = self._from_dict(raw if isinstance(raw, dict) else {})

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self._data)

    def get_watch_volume_pct(self) -> int:
        with self._lock:
            return self._data.watch_volume_pct

    def get_listen_volume_pct(self) -> int:
        with self._lock:
            return self._data.listen_volume_pct

    def get_listen_target(self) -> dict:
        with self._lock:
            return {
                "type": self._data.listen_target_type,
                "preset": self._data.listen_target_preset,
                "stream": self._data.listen_target_stream,
            }

    def get_stream_url(self, slot: int) -> str:
        with self._lock:
            return str(getattr(self._data, f"stream_url_{slot}", "") or "").strip()

    def get_soundbar_stream_url(self, slot: int) -> str:
        with self._lock:
            return str(getattr(self._data, f"soundbar_stream_url_{slot}", "") or "").strip()

    def get_soundbar_restream(self, slot: int) -> bool:
        with self._lock:
            return bool(getattr(self._data, f"soundbar_restream_{slot}", False))

    def save_from_payload(self, payload: dict, *, speaker_backend: str | None = None) -> dict:
        with self._lock:
            base = asdict(self._data)
            base.update(payload or {})
            validated = self._validate_payload(base, speaker_backend=speaker_backend)
            self._data = SettingsData(**validated)
            self._write_locked()
            return asdict(self._data)

    def _write_locked(self) -> None:
        parent = os.path.dirname(self._path) or "."
        os.makedirs(parent, exist_ok=True)

        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self._data), f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self._path)

    @staticmethod
    def _from_dict(raw: dict) -> SettingsData:
        base = asdict(SettingsData())
        base.update({k: v for k, v in raw.items() if k in base})
        validated = SettingsStore._validate_payload(base)
        return SettingsData(**validated)

    @staticmethod
    def _validate_payload(raw: dict, *, speaker_backend: str | None = None) -> dict:
        defaults = SettingsData()

        def _int_in_range(name: str, lo: int, hi: int, default: int) -> int:
            value = raw.get(name, default)
            try:
                value = int(value)
            except Exception:
                raise ValueError(f"{name} must be an integer")
            if value < lo or value > hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
            return value

        def _str(name: str, default: str = "") -> str:
            value = str(raw.get(name, default) or "").strip()
            return value

        def _bool(name: str, default: bool = False) -> bool:
            value = raw.get(name, default)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        backend = str(speaker_backend or "").strip().lower()

        listen_target_type = _str("listen_target_type", defaults.listen_target_type).lower()
        if listen_target_type == "tunein":
            # Retired soundbar option; its stations now live in the stream slots.
            listen_target_type = "stream"
        if listen_target_type not in {"preset", "stream"}:
            raise ValueError("listen_target_type must be 'preset' or 'stream'")

        is_soundbar = backend == "samsung_soundbar"
        # Audio Pro has 4 stream URLs, the soundbar 10. Settings loaded from
        # disk (backend unknown) accept either.
        max_stream_slot = SLOT_COUNT if (is_soundbar or not backend) else 4
        if is_soundbar:
            # Every soundbar slot is a stream.
            listen_target_type = "stream"

        out = {
            "watch_volume_pct": _int_in_range(
                "watch_volume_pct", 0, 100, defaults.watch_volume_pct
            ),
            "listen_volume_pct": _int_in_range(
                "listen_volume_pct", 0, 100, defaults.listen_volume_pct
            ),
            "listen_target_type": listen_target_type,
            "listen_target_preset": _int_in_range(
                "listen_target_preset", 1, 6, defaults.listen_target_preset
            ),
            "listen_target_stream": _int_in_range(
                "listen_target_stream", 1, max_stream_slot, defaults.listen_target_stream
            ),
            "stream_url_1": _str("stream_url_1", defaults.stream_url_1),
            "stream_url_2": _str("stream_url_2", defaults.stream_url_2),
            "stream_url_3": _str("stream_url_3", defaults.stream_url_3),
            "stream_url_4": _str("stream_url_4", defaults.stream_url_4),
        }
        for n in range(1, SLOT_COUNT + 1):
            out[f"soundbar_stream_url_{n}"] = _str(f"soundbar_stream_url_{n}")
            out[f"soundbar_restream_{n}"] = _bool(f"soundbar_restream_{n}")

        for key in ("stream_url_1", "stream_url_2", "stream_url_3", "stream_url_4"):
            value = out[key]
            if value and not (value.startswith("http://") or value.startswith("https://")):
                raise ValueError(f"{key} must start with http:// or https://")

        for n in range(1, SLOT_COUNT + 1):
            value = out[f"soundbar_stream_url_{n}"]
            if value and not (
                value.startswith(("http://", "https://")) or is_tunein_source(value)
            ):
                raise ValueError(
                    f"Key {n % SLOT_COUNT} stream URL must start with http:// or https://, "
                    "or be a TuneIn station ID or tunein.com URL"
                )

        # Listen target / stream URL validation only matters for speaker backends
        # that actually use the local listen-target settings. Only checked on
        # save (backend known): a file on disk may belong to either backend.
        if backend and not is_soundbar:
            if out["listen_target_type"] == "stream":
                stream_key = f"stream_url_{out['listen_target_stream']}"
                if not out[stream_key]:
                    raise ValueError(
                        f"listen_target_stream points to empty {stream_key}; set a URL or choose preset"
                    )

        if is_soundbar:
            slot = out["listen_target_stream"]
            if not out[f"soundbar_stream_url_{slot}"]:
                raise ValueError(
                    f"Listen flow plays stream slot {slot}, but Key {slot % SLOT_COUNT} "
                    "stream URL is empty"
                )

        return out
