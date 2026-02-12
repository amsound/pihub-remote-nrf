"""Route remote key events to BLE or Home Assistant actions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

try:
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover - fallback for older Python
    import importlib_resources

from .validation import parse_ms

# Global repeat knobs (WS only; BLE never repeats)
REPEAT_INITIAL_MS = 400
REPEAT_RATE_MS = 400

EdgeCB = Callable[[str, str], Awaitable[None]] | Callable[[str, str], None]

logger = logging.getLogger(__name__)


class Dispatcher:
    """
    Routes remote key edges to actions defined per-activity in keymap.json:
      - { "do": "emit", "text": "<pihub.cmd text>", ...extras,
          "when"?: "down"|"up" (default "down"),
          "repeat"?: true,
          "min_hold_ms"?: <int>   # keymap ms values are permissive (bounded)
        }
      - { "do": "ble",  "usage": "keyboard"|"consumer", "code": "<hid-name>" }
      - { "do": "noop" }  # explicit no-op action
    """

    def __init__(self, cfg: Any, send_cmd: Callable[..., Awaitable[bool]], bt_le: Any) -> None:
        self._cfg = cfg
        self._send_cmd = send_cmd
        self._bt = bt_le
        self._last_cmd_fail_log = 0.0

        # Load full keymap document, then split into parts we use
        km = self._load_keymap()
        try:
            self._validate_keymap(km)
            self._scancode_map: Dict[str, str] = dict(km["scancode_map"])
            self._bindings: Dict[str, Dict[str, List[Dict[str, Any]]]] = dict(km["activities"])
            if not isinstance(self._scancode_map, dict) or not isinstance(self._bindings, dict):
                raise TypeError
        except Exception as e:
            raise ValueError(
                "keymap.json schema invalid: expected 'scancode_map' (dict) and 'activities' (dict)."
            ) from e

        self._activity: Optional[str] = None
        self._activity_none_logged = False

        # Active repeat tasks keyed by rem_* (per-key)
        self._repeat_tasks: Dict[str, asyncio.Task] = {}

        # Press timing (seconds from loop.time()) keyed by rem_*
        self._pressed_at: Dict[str, float] = {}

        # Delayed hold triggers: (rem_key, action_index) -> task
        self._hold_tasks: Dict[Tuple[str, int], asyncio.Task] = {}

        # Summary: count activities and scancodes
        acts = len(self._bindings)
        scan_total = len(self._scancode_map)
        logger.info("[dispatcher] keymap loaded: %s activities, %s scancodes", acts, scan_total)

    @property
    def scancode_map(self) -> Dict[str, str]:
        """Public accessor for the logical rem_* scancode map."""
        return self._scancode_map

    # Activity comes from HA (ha_ws)
    async def on_activity(self, text: Optional[str]) -> None:
        """Record the current activity reported by Home Assistant."""
        prior = self._activity
        self._activity = text
        if (prior is None) != (text is None):
            self._activity_none_logged = False
    # USB edges come from UnifyingReader
    async def on_usb_edge(self, rem_key: str, edge: str) -> None:
        """Handle a key edge originating from the USB receiver."""
        loop = asyncio.get_running_loop()

        if self._activity is None and not self._activity_none_logged:
            logger.info(
                "[dispatcher] activity not set yet; ignoring input until HA activity arrives"
            )
            self._activity_none_logged = True

        if not await self._update_press_state(rem_key, edge, loop):
            return

        actions = (self._bindings.get(self._activity, {}) or {}).get(rem_key, [])
        # enumerate actions so we can key per-action hold tasks
        for idx, a in enumerate(actions):
            await self._do_action(a, edge, rem_key=rem_key, action_index=idx)

        # clear press timestamp on full release
        if edge == "up":
            self._pressed_at.pop(rem_key, None)

    async def _update_press_state(
        self,
        rem_key: str,
        edge: str,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Update per-key edge state (press timing + repeat/hold cancellation)."""
        if edge == "down":
            # start timing window for this key
            self._pressed_at[rem_key] = loop.time()
            # cancel any stale hold-tasks from previous cycles
            await self._cancel_hold_tasks(rem_key)
            return True

        if edge == "up":
            # stop any repeat and cancel pending hold triggers
            await self._stop_repeat(rem_key)
            await self._cancel_hold_tasks(rem_key)
            return True

        return False


    async def on_usb_disconnect(self) -> None:
        """Handle USB disconnects to prevent stuck repeats."""
        await self._cancel_all_repeat_tasks()
        await self._cancel_all_hold_tasks()
        self._pressed_at.clear()

    # ---- Repeat helpers (WS only) ----
    async def _start_repeat(self, rem_key: str, text: str, extras: dict) -> None:
        if rem_key in self._repeat_tasks:
            return

        async def _runner():
            try:
                await asyncio.sleep(REPEAT_INITIAL_MS / 1000.0)
                while True:
                    await self._send_with_log(text=text, **extras)
                    await asyncio.sleep(REPEAT_RATE_MS / 1000.0)
            except asyncio.CancelledError:
                pass

        self._repeat_tasks[rem_key] = asyncio.create_task(_runner(), name=f"repeat:{rem_key}")

    async def _stop_repeat(self, rem_key: str) -> None:
        t = self._repeat_tasks.pop(rem_key, None)
        if t:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t

    async def _cancel_all_repeat_tasks(self) -> None:
        tasks = list(self._repeat_tasks.values())
        self._repeat_tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t

    # ---- Hold-trigger helpers (HA emit only) ----
    async def _schedule_hold_emit(
        self,
        rem_key: str,
        action_index: int,
        min_hold_ms: int,
        text: str,
        extras: dict,
        want_repeat: bool,
    ) -> None:
        """
        Schedule a delayed fire for 'when=down' + min_hold_ms. If key is released
        before the delay, the task is cancelled and nothing is sent.
        """
        # avoid duplicates
        key = (rem_key, action_index)
        if key in self._hold_tasks:
            return

        async def _hold_runner():
            try:
                await asyncio.sleep(max(0, min_hold_ms) / 1000.0)
                # Only fire if key is still considered down (timestamp still present)
                if rem_key in self._pressed_at:
                    await self._send_cmd(text=text, **extras)
                    if want_repeat:
                        await self._start_repeat(rem_key, text, extras)
            except asyncio.CancelledError:
                pass
            finally:
                # clean up this task entry
                self._hold_tasks.pop(key, None)

        self._hold_tasks[key] = asyncio.create_task(_hold_runner(), name=f"hold:{rem_key}:{action_index}")

    async def _cancel_hold_tasks(self, rem_key: str) -> None:
        # cancel all hold tasks for this rem_key (any action index)
        to_cancel = [k for k in self._hold_tasks if k[0] == rem_key]
        for k in to_cancel:
            t = self._hold_tasks.pop(k, None)
            if t:
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t

    async def _cancel_all_hold_tasks(self) -> None:
        tasks = list(self._hold_tasks.values())
        self._hold_tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t
    # ---- Action executor ----
    async def _do_action(
        self,
        a: dict,
        edge: str,
        *,
        rem_key: Optional[str] = None,
        action_index: int = 0,
    ) -> None:
        kind = a.get("do")

        if kind == "noop":
            return

        # Optional edge filter for non-BLE actions (defaults to 'down' in this build)
        when = a.get("when", "down")
        if kind != "ble" and edge != when:
            return

        if kind == "ble":
            await self._handle_ble_action(a, edge)
            return

        if kind == "emit":
            await self._handle_emit_action(a, edge, rem_key=rem_key, action_index=action_index)
            return

        # Unknown action -> ignore
        return

    async def _handle_ble_action(self, a: dict, edge: str) -> None:
        """Handle edge-accurate BLE actions (never repeat)."""
        usage = a.get("usage")
        code = a.get("code")
        if not (isinstance(usage, str) and isinstance(code, str)):
            return

        if edge == "down":
            self._bt.key_down(usage=usage, code=code)
        elif edge == "up":
            self._bt.key_up(usage=usage, code=code)

    async def _handle_emit_action(
        self,
        a: dict,
        edge: str,
        *,
        rem_key: Optional[str],
        action_index: int,
    ) -> None:
        """Handle Home Assistant emit actions (supports min_hold_ms + repeat)."""
        text = a.get("text")
        if not isinstance(text, str):
            return

        extras = {k: v for k, v in a.items() if k not in {"do", "when", "text", "repeat", "min_hold_ms"}}
        want_repeat = bool(a.get("repeat"))
        min_hold_ms = parse_ms(
            a.get("min_hold_ms"),
            default=0,
            min=0,
            max=5000,
            allow_none=False,
            context="keymap.min_hold_ms",
        ) or 0

        loop = asyncio.get_running_loop()
        when = a.get("when", "down")

        # when == "up": fire on release; if min_hold_ms > 0, enforce press duration
        if when == "up" and edge == "up":
            if min_hold_ms > 0 and rem_key:
                t0 = self._pressed_at.get(rem_key)
                if t0 is None:
                    return
                elapsed_ms = int((loop.time() - t0) * 1000.0)
                if elapsed_ms < min_hold_ms:
                    return
            await self._send_with_log(text=text, **extras)
            # no repeat on 'up'-triggered emits
            return

        # when == "down": fire on press; if min_hold_ms > 0, delay until threshold
        if when == "down" and edge == "down":
            if min_hold_ms > 0 and rem_key is not None:
                await self._schedule_hold_emit(
                    rem_key=rem_key,
                    action_index=action_index,
                    min_hold_ms=min_hold_ms,
                    text=text,
                    extras=extras,
                    want_repeat=want_repeat,
                )
                return
            # immediate fire + optional repeat
            await self._send_with_log(text=text, **extras)
            if want_repeat and rem_key:
                await self._start_repeat(rem_key, text, extras)
            return

        # any other combination -> ignore
        return

    # ---- Keymap loader ----

    # ---- Keymap loader ----
    def _load_keymap(self) -> dict:
        """
        Load remote key bindings.
        """
        identifier = "pihub.assets:keymap.json"
        logger.info("[dispatcher] Loading keymap from packaged assets: %s", identifier)
        try:
            resource = importlib_resources.files("pihub.assets") / "keymap.json"
            raw = resource.read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            raise FileNotFoundError(
                f"Packaged keymap missing or unreadable: {identifier}"
            ) from exc

        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Packaged keymap invalid JSON ({identifier}): {exc}") from exc

        if not isinstance(doc, dict) or "scancode_map" not in doc or "activities" not in doc:
            raise ValueError(
                f"Packaged keymap schema invalid ({identifier}): expected 'scancode_map' and 'activities'."
            )

        return doc

    async def _send_with_log(self, text: str, **extras: Any) -> None:
        success = await self._send_cmd(text=text, **extras)
        if success:
            return
        if not logger.isEnabledFor(logging.DEBUG):
            return
        now = time.monotonic()
        if now - self._last_cmd_fail_log < 5.0:
            return
        self._last_cmd_fail_log = now
        logger.warning("[dispatcher] HA command send failed: %s", text)

    @staticmethod
    def _validate_keymap(doc: dict) -> None:
        if not isinstance(doc, dict):
            raise ValueError("keymap.json must be a dict")

        activities = doc.get("activities")
        if not isinstance(activities, dict):
            raise ValueError("keymap.json 'activities' must be a dict")

        for activity, mapping in activities.items():
            if not isinstance(mapping, dict):
                raise ValueError(f"activity '{activity}' must map to a dict of actions")
            for rem_key, actions in mapping.items():
                if not isinstance(actions, list):
                    raise ValueError(f"actions for '{activity}.{rem_key}' must be a list")
                for idx, action in enumerate(actions):
                    if not isinstance(action, dict):
                        raise ValueError(f"action {activity}.{rem_key}[{idx}] must be a dict")
                    kind = action.get("do")
                    if kind not in {"emit", "ble", "noop"}:
                        raise ValueError(f"action {activity}.{rem_key}[{idx}] has unknown do={kind!r}")
