"""Configuration helpers for environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # Home Assistant
    ha_ws_url: str
    ha_token_file: str
    ha_activity: str
    ha_cmd_event: str

    # HID dongle transport (ACM)
    ble_serial_device: str
    ble_serial_baud: int

    # Health endpoint
    health_host: str
    health_port: int

    # Samsung TV
    tv_ip: str
    tv_mac: str
    tv_token_file: str
    tv_name: str

    # LinkPlay/WiiM Speaker (static host)
    speaker_host: str
    speaker_http_scheme: str  # https (self-signed)
    speaker_volume_step_pct: int  # MUST be 2%

    @staticmethod
    def load() -> "Config":
        """Build a Config from environment (compose env)."""
        ha_ws_url = os.getenv("HA_WS_URL", "ws://127.0.0.1:8123/api/websocket")
        ha_token_file = os.getenv("HA_TOKEN_FILE", "/run/secrets/ha_token")
        ha_activity = os.getenv("HA_ACTIVITY", "input_select.activity")
        ha_cmd_event = os.getenv("HA_CMD_EVENT", "pihub.cmd")

        # HID dongle
        ble_serial_device = os.getenv("BLE_SERIAL_DEVICE", "/dev/ttyACM0")
        ble_serial_baud = int(os.getenv("BLE_SERIAL_BAUD", "115200"))

        health_host = os.getenv("HEALTH_HOST", "0.0.0.0")
        try:
            health_port = int(os.getenv("HEALTH_PORT", "9123"))
        except ValueError:
            health_port = 9123

        tv_ip = (os.getenv("TV_IP", "") or "").strip()
        tv_mac = (os.getenv("TV_MAC", "") or "").strip()
        tv_token_file = (os.getenv("TV_TOKEN_FILE", "/data/samsungtv-token.txt") or "").strip()
        tv_name = (os.getenv("TV_NAME", "PiHub Remote") or "").strip()

        # Speaker
        speaker_host = (os.getenv("SPEAKER_HOST", "") or "").strip()
        speaker_http_scheme = (os.getenv("SPEAKER_HTTP_SCHEME", "https") or "https").strip().lower()
        if speaker_http_scheme not in {"http", "https"}:
            speaker_http_scheme = "https"
        
        speaker_volume_step_pct = 2

        return Config(
            ha_ws_url=ha_ws_url,
            ha_token_file=ha_token_file,
            ha_activity=ha_activity,
            ha_cmd_event=ha_cmd_event,
            ble_serial_device=ble_serial_device,
            ble_serial_baud=ble_serial_baud,
            health_host=health_host,
            health_port=health_port,
            tv_ip=tv_ip,
            tv_mac=tv_mac,
            tv_token_file=tv_token_file,
            tv_name=tv_name,
            speaker_host=speaker_host,
            speaker_http_scheme=speaker_http_scheme,
            speaker_volume_step_pct=speaker_volume_step_pct,
        )

    def load_token(self) -> str:
        """Return the HA token from environment or configured file."""
        env_tok = (os.getenv("HA_TOKEN") or "").strip()
        if env_tok:
            return env_tok

        path = (self.ha_token_file or "").strip()
        if not path:
            raise RuntimeError("HA token unavailable: set HA_TOKEN or provide HA_TOKEN_FILE")

        try:
            with open(path, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"HA token file not found: {path}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read HA token file {path}: {exc}") from exc

        if not token:
            raise RuntimeError(f"HA token file {path} is empty")

        return token