"""Configuration helpers for environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

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

    # Audio Pro/Arylic/LinkPlay/WiiM Speaker
    speaker_ip: str
    speaker_http_scheme: str  # https (self-signed)
    speaker_volume_step_pct: int  # MUST be 2%

    # Domain toggles
    ble_enabled: bool
    usb_enabled: bool
    tv_enabled: bool
    speaker_enabled: bool


    @staticmethod
    def load() -> "Config":
        """Build a Config from environment (compose env)."""
        # Domain toggles
        ble_enabled = _env_bool("BLE_ENABLED", True)
        usb_enabled = _env_bool("USB_ENABLED", True)
        tv_enabled = _env_bool("TV_ENABLED", True)
        speaker_enabled = _env_bool("SPEAKER_ENABLED", True)

        # HID dongle
        ble_serial_device = (os.getenv("BLE_SERIAL_DEVICE", "auto") or "auto").strip()
        ble_serial_baud = int(os.getenv("BLE_SERIAL_BAUD", "115200"))

        http_server_host = os.getenv("http_server_host", "0.0.0.0")
        try:
            http_server_port = int(os.getenv("http_server_port", "9123"))
        except ValueError:
            http_server_port = 9123

        tv_ip = (os.getenv("TV_IP", "") or "").strip()
        tv_mac = (os.getenv("TV_MAC", "") or "").strip()
        tv_token_file = (os.getenv("TV_TOKEN_FILE", "/data/samsungtv-token.txt") or "").strip()
        tv_name = (os.getenv("TV_NAME", "PiHub Remote") or "").strip()

        # Speaker
        speaker_ip = (os.getenv("SPEAKER_IP", "") or "").strip()
        speaker_http_scheme = (os.getenv("SPEAKER_HTTP_SCHEME", "https") or "https").strip().lower()
        if speaker_http_scheme not in {"http", "https"}:
            speaker_http_scheme = "https"

        speaker_volume_step_pct = 2

        return Config(
            ble_enabled=ble_enabled,
            usb_enabled=usb_enabled,
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
            speaker_ip=speaker_ip,
            speaker_http_scheme=speaker_http_scheme,
            speaker_volume_step_pct=speaker_volume_step_pct,
        )
