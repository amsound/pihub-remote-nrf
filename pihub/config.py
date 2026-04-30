"""Configuration helpers for environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def _env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)

    items: list[str] = []
    seen: set[str] = set()

    for part in raw.split(","):
        value = part.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        items.append(value)

    return items if items else list(default)

@dataclass(frozen=True)
class Config:
    # HID dongle transport (ACM)
    ble_serial_device: str
    ble_serial_baud: int

    # HTTP endpoint
    http_server_host: str
    http_server_port: int

    # Samsung TV
    tv_ip: str
    tv_mac: str
    tv_token_file: str
    tv_name: str
    tv_frame_ip: str
    tv_frame_token_file: str

    # Apple TV AirPlay session detector
    apple_tv_ip: str
    apple_tv_airplay_enabled: bool
    apple_tv_airplay_debounce_s: float

    # Speaker backend selection
    speaker_backend: str

    # Audio Pro
    speaker_ip: str
    # Audio Pro peer discovery / multiroom support
    known_speaker_ips: list[str]

    # Domain toggles
    tv_enabled: bool
    speaker_enabled: bool

    @staticmethod
    def load() -> "Config":
        tv_enabled = _env_bool("TV_ENABLED", True)
        speaker_enabled = _env_bool("SPEAKER_ENABLED", True)

        ble_serial_device = (os.getenv("BLE_SERIAL_DEVICE", "auto") or "auto").strip()
        ble_serial_baud = int(os.getenv("BLE_SERIAL_BAUD", "115200"))

        http_server_host = os.getenv("HTTP_SERVER_HOST", "0.0.0.0")
        try:
            http_server_port = int(os.getenv("HTTP_SERVER_PORT", "9123"))
        except ValueError:
            http_server_port = 9123

        tv_ip = (os.getenv("TV_IP", "") or "").strip()
        tv_mac = (os.getenv("TV_MAC", "") or "").strip()
        tv_token_file = (os.getenv("TV_TOKEN_FILE", "/data/samsungtv-token.txt") or "").strip()
        tv_name = (os.getenv("TV_NAME", "PiHub Remote") or "").strip()
        tv_frame_ip = (os.getenv("TV_FRAME_IP", "") or "").strip()
        tv_frame_token_file = (
            os.getenv("TV_FRAME_TOKEN_FILE", "/data/samsung-frame-token.txt") or ""
        ).strip()

        apple_tv_ip = (os.getenv("APPLE_TV_IP", "") or "").strip()
        apple_tv_airplay_enabled = _env_bool("APPLE_TV_AIRPLAY_ENABLED", True)
        try:
            apple_tv_airplay_debounce_s = float(
                os.getenv("APPLE_TV_AIRPLAY_DEBOUNCE_S", "2.5")
            )
        except ValueError:
            apple_tv_airplay_debounce_s = 2.5

        speaker_backend = (os.getenv("SPEAKER_BACKEND", "audiopro") or "audiopro").strip().lower()

        speaker_ip = (os.getenv("SPEAKER_IP", "") or "").strip()
        known_speaker_ips = _env_csv(
            "KNOWN_SPEAKER_IPS",
            ["192.168.70.43", "192.168.70.45", "192.168.70.46"],
        )

        return Config(
            tv_enabled=tv_enabled,
            speaker_enabled=speaker_enabled,
            ble_serial_device=ble_serial_device,
            ble_serial_baud=ble_serial_baud,
            http_server_host=http_server_host,
            http_server_port=http_server_port,
            tv_ip=tv_ip,
            tv_mac=tv_mac,
            tv_token_file=tv_token_file,
            tv_name=tv_name,
            tv_frame_ip=tv_frame_ip,
            tv_frame_token_file=tv_frame_token_file,
            apple_tv_ip=apple_tv_ip,
            apple_tv_airplay_enabled=apple_tv_airplay_enabled,
            apple_tv_airplay_debounce_s=apple_tv_airplay_debounce_s,
            speaker_backend=speaker_backend,
            speaker_ip=speaker_ip,
            known_speaker_ips=known_speaker_ips,
        )