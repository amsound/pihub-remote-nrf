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

    # HID dongle transport (HID-only)
    ble_hid_vid: int
    ble_hid_pid: int
    ble_hid_product: str

    # Health endpoint
    health_host: str
    health_port: int

    @staticmethod
    def load() -> "Config":
        """Build a Config from environment (compose env)."""
        ha_ws_url     = os.getenv("HA_WS_URL", "ws://127.0.0.1:8123/api/websocket")
        ha_token_file = os.getenv("HA_TOKEN_FILE", "/run/secrets/ha_token")
        ha_activity   = os.getenv("HA_ACTIVITY", "input_select.activity")
        ha_cmd_event  = os.getenv("HA_CMD_EVENT", "pihub.cmd")

        # HID dongle
        # Defaults match Zephyr’s defaults you observed: VID 0x2FE3, PID 0x0100.
        ble_hid_vid = int(os.getenv("BLE_HID_VID", "0x2FE3"), 0)
        ble_hid_pid = int(os.getenv("BLE_HID_PID", "0x0100"), 0)
        ble_hid_product = os.getenv("BLE_HID_PRODUCT", "PiHub Dongle")

        health_host   = os.getenv("HEALTH_HOST", "0.0.0.0")
        try:
            health_port = int(os.getenv("HEALTH_PORT", "9123"))
        except ValueError:
            health_port = 9123

        return Config(
            ha_ws_url=ha_ws_url,
            ha_token_file=ha_token_file,
            ha_activity=ha_activity,
            ha_cmd_event=ha_cmd_event,
            ble_hid_vid=ble_hid_vid,
            ble_hid_pid=ble_hid_pid,
            ble_hid_product=ble_hid_product,
            health_host=health_host,
            health_port=health_port,
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