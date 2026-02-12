"""Predefined macro sequences for BLE HID playback."""

from __future__ import annotations
from typing import TypedDict, Literal, List, Dict, Union

Usage = Literal["keyboard", "consumer"]

class KeyStep(TypedDict, total=False):
    usage: Usage         # "keyboard" or "consumer"
    code: str            # e.g. "power", "home", "menu", "stop"
    hold_ms: int         # default provided by runner

class WaitStep(TypedDict):
    wait_ms: int         # idle delay without sending a key

Step = Union[KeyStep, WaitStep]

# Default named macros (adjust codes to match your HID map)
MACROS: Dict[str, List[Step]] = {
    "power_on": [
        {"usage": "consumer", "code": "power", "hold_ms": 40},
        {"wait_ms": 3000},
        {"usage": "consumer", "code": "menu", "hold_ms": 40},
    ],
    "power_off": [
        {"usage": "consumer", "code": "stop", "hold_ms": 40},
        {"usage": "consumer", "code": "ac_home", "hold_ms": 40},
        {"usage": "consumer", "code": "ac_home", "hold_ms": 40},
        {"usage": "consumer", "code": "menu", "hold_ms": 40},
        {"usage": "consumer", "code": "menu", "hold_ms": 40},
        {"usage": "consumer", "code": "power", "hold_ms": 2000},
    ],
    "return_home": [
        {"usage": "consumer", "code": "stop", "hold_ms": 40},
        {"usage": "consumer", "code": "ac_home", "hold_ms": 40},
        {"usage": "consumer", "code": "ac_home", "hold_ms": 40},
        {"usage": "consumer", "code": "menu", "hold_ms": 40},
        {"usage": "consumer", "code": "menu", "hold_ms": 40},
    ],
}
