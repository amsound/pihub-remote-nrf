"""Translate symbolic key names into HID payloads."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

Usage = Literal["keyboard", "consumer"]

logger = logging.getLogger(__name__)

class HIDClient:
    """Encode symbolic keys to HID payloads and forward to the transport."""
    def __init__(self, *, hid) -> None:
        self._hid = hid
        self._kb, self._cc = self._load_hid_tables()

    # ---------- edge-level API ----------
    def key_down(self, *, usage: Usage, code: str) -> None:
        """Send a logical key-down edge."""
        if usage == "keyboard":
            down = self._encode_keyboard_down(code)
            if down is None:
                return
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('[bt] keyboard "%s" down', code)
            self._hid.notify_keyboard(down)
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('[bt] consumer "%s" down (0x%04X)', code, usage_id)
            if usage_id:
                self._hid.notify_consumer(usage_id, True)

    def key_up(self, *, usage: Usage, code: str) -> None:
        """Send a logical key-up edge."""
        if usage == "keyboard":
            if code not in self._kb:
                logger.warning('[bt] unknown keyboard code "%s" up; ignoring', code)
                return
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('[bt] keyboard "%s" up', code)
            self._hid.notify_keyboard(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('[bt] consumer "%s" up (0x%04X)', code, usage_id)
            if usage_id:
                self._hid.notify_consumer(usage_id, False)

    # ---------- tap API (macros/WS) ----------
    async def send_key(self, *, usage: Usage, code: str, hold_ms: int = 40) -> None:
        """Tap a key by sending down → delay → up."""
        # tap = down + delay + up
        self.key_down(usage=usage, code=code)
        await asyncio.sleep(max(0, hold_ms) / 1000.0)
        self.key_up(usage=usage, code=code)

    async def run_macro(
        self,
        steps: List[Dict[str, Any]],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        """Execute a timed macro sequence."""
        for step in steps:
            if "wait_ms" in step:  # idle delay
                await asyncio.sleep(max(0, int(step["wait_ms"])) / 1000.0)
                continue
            usage = step.get("usage")
            code = step.get("code")
            hold = int(step.get("hold_ms", default_hold_ms))
            if isinstance(usage, str) and isinstance(code, str):
                await self.send_key(usage=usage, code=code, hold_ms=hold)
                await asyncio.sleep(max(0, inter_delay_ms) / 1000.0)

    # ---------- internals ----------
    def _load_hid_tables(self) -> Tuple[Dict[str, int], Dict[str, int]]:
        base = Path(__file__).resolve().parent
        yml = base / "hid_keymap.yaml"
        jsn = base / "hid_keymap.json"

        data: Dict[str, Any] = {}
        if yml.is_file():
            try:
                import yaml
                data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        elif jsn.is_file():
            try:
                data = json.loads(jsn.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}

        kb = {k: int(v) for k, v in (data.get("keyboard") or {}).items()}
        cc = {k: int(v) for k, v in (data.get("consumer") or {}).items()}
        return kb, cc

    def _encode_keyboard_down(self, code: str) -> Optional[bytes]:
        hid = self._kb.get(code)
        if hid is None:
            logger.warning('[bt] unknown keyboard code "%s" down; ignoring', code)
            return None
        # Boot Keyboard 8-byte: mods(1), reserved(1), key1..key6
        return bytes([0x00, 0x00, hid, 0x00, 0x00, 0x00, 0x00, 0x00])

    def _encode_consumer_usage(self, code: str) -> int:
        return int(self._cc.get(code) or 0)
