"""Route remote key events to BLE, HA, TV or Speaker methods"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .input_ble_dongle import CompiledBleFrames
from .macros import MACROS
from .validation import DEFAULT_MS_WHITELIST, parse_ms, parse_ms_whitelist

try:
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover - fallback for older Python
    import importlib_resources  # type: ignore


# Repeat knobs (volume only: rem_vol_up/rem_vol_down). BLE never repeats.
REPEAT_INITIAL_MS = 400
REPEAT_RATE_MS = 300

_REPEAT_KEYS = {"rem_vol_up", "rem_vol_down"}

_METHOD_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_DENY_CALLS = {
    # lifecycle / internal methods that should never be callable from keymap or HA bus
    "start",
    "stop",
    "_runner",
    "_connect_and_subscribe",
    "_disconnect_upnp",
    "snapshot",
}

EdgeCB = Callable[[str, str], Awaitable[None]] | Callable[[str, str], None]

logger = logging.getLogger(__name__)


class Dispatcher:
    """
    Routes remote key edges to actions defined per-activity in keymap.json:

      - HA bus action:
        { "domain": "ha", "action": "<pihub.cmd text>", ...extras,
          "when"?: "down"|"up" (default "down"),
          "min_hold_ms"?: <int>   # bounded (0..5000)
        }

      - BLE HID edge-accurate action:
        { "domain": "ble",  "usage": "keyboard"|"consumer", "code": "<hid-name>" }

      - No-op:
        { "domain": "noop" }

    Notes:
      - Key-repeat is *not* part of keymap schema. It is forced for rem_vol_up/rem_vol_down only.
      - Repeat applies to TV/Speaker volume only.
    """

    def __init__(
        self,
        cfg: Any,
        send_cmd: Callable[..., Awaitable[bool]],
        bt_le: Any,
        tv: Any = None,
        speaker: Any = None,
    ) -> None:
        self._cfg = cfg
        self._send_cmd = send_cmd
        self._bt = bt_le
        self._tv = tv
        self._speaker = speaker
        self._last_cmd_fail_log = 0.0

        # Load full keymap document, then split into parts we use
        km = self._load_keymap()
        try:
            self._validate_keymap(km)
            self._scancode_map: Dict[str, str] = dict(km["scancode_map"])
            self._bindings: Dict[str, Dict[str, List[Dict[str, Any]]]] = dict(km["activities"])
            if not isinstance(self._scancode_map, dict) or not isinstance(self._bindings, dict):
                raise TypeError
        except Exception as exc:
            raise ValueError(
                "keymap.json schema invalid: expected 'scancode_map' (dict) and 'activities' (dict)."
            ) from exc

        self._precompile_ha_actions()
        self._activity: Optional[str] = None
        self._activity_none_logged = False
        self._active_bindings: Dict[str, List[Dict[str, Any]]] = {}

        # Active repeat tasks keyed by rem_* (per-key)
        self._repeat_tasks: Dict[str, asyncio.Task] = {}

        # Press timing (seconds from loop.time()) keyed by rem_*
        self._pressed_at: Dict[str, float] = {}

        # Delayed hold triggers: (rem_key, action_index) -> task
        self._hold_tasks: Dict[Tuple[str, int], asyncio.Task] = {}

        # Precompiled BLE frames per activity (hot path)
        self._ble_frames_by_activity: Dict[str, CompiledBleFrames] = {}
        self._active_ble_frames: Optional[CompiledBleFrames] = None
        self._compile_ble_frames_once()

        # Summary: count activities and scancodes
        acts = len(self._bindings)
        scan_total = len(self._scancode_map)
        logger.info("keymap loaded: %s activities, %s scancodes", acts, scan_total)

    @property
    def scancode_map(self) -> Dict[str, str]:
        """Public accessor for the logical rem_* scancode map."""
        return self._scancode_map

    @staticmethod
    def _action_kwargs(a: dict) -> dict:
        """Extract kwargs payload from an action dict without changing keymap schema."""
        return {
            k: v
            for k, v in a.items()
            if k
            not in {
                "domain",
                "action",
                "when",
                "min_hold_ms",
                "_when",
                "_min_hold_ms",
                "_extras",
            }
        }

    async def _call_action_method(self, obj: Any, method: str, kwargs: dict) -> None:
        """Call an async method by name on obj (generic dispatch). Breaking by design."""
        if not isinstance(method, str) or not method or not _METHOD_RE.match(method):
            return
        if method.startswith("_") or method in _DENY_CALLS:
            return
        fn = getattr(obj, method, None)
        if fn is None or not callable(fn) or not inspect.iscoroutinefunction(fn):
            return
        await fn(**(kwargs or {}))

    async def on_cmd(self, data: dict) -> None:
        """
        Handle a pihub.cmd event from Home Assistant (generic, breaking schema).

        Expected payload (inside HA event data):
        {
            "dest": "pihub",
            "domain": "speaker"|"tv"|"ble"|"ha"|"macro",
            "action": "<method_name or key>",
            "args": { ... }   # optional kwargs for method
        }

        Notes:
        - "ha": forwards to Home Assistant bus
        - "ble": sends a single HID press (usage+code) with optional key_hold_ms (default 40)
        - "tv": tries method call; if not found, treats action as raw KEY_* string and sends via ws
        - "speaker": method call on speaker
        - "macro": named macro from macros.py (uses key_hold_ms default for step holds)
        """
        if not isinstance(data, dict):
            return
        if data.get("dest") != "pihub":
            return

        domain = data.get("domain")
        action = data.get("action")
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        if not isinstance(domain, str) or not isinstance(action, str):
            return

        # if domain == "ha":
        #    action = args.get("action")
        #    ha_args = args.get("args") if isinstance(args.get("args"), dict) else {}
        #    if isinstance(action, str):
        #        await self._send_with_log(action=action, **ha_args)
        #    return

        if domain == "macro":
            name = action
            steps = MACROS.get(name, [])
            if not steps:
                logger.debug("cmd macro missing: %s", name)
                return

            key_hold_ms = parse_ms_whitelist(args.get("key_hold_ms"), default=40, context="cmd.key_hold_ms")
            inter = parse_ms_whitelist(
                args.get("inter_delay_ms"),
                allowed=(*DEFAULT_MS_WHITELIST, 400),
                default=400,
                context="cmd.inter_delay_ms",
            )
            await self._bt.run_macro(steps, default_key_hold_ms=key_hold_ms, inter_delay_ms=inter)
            return

        if domain == "ble":
            if action == "unpair":
                self._bt.release_all()
                await self._bt.unpair()
                return

            if action == "press":
                usage = args.get("usage")
                code = args.get("code")
                key_hold_ms = args.get("key_hold_ms", 40)
                try:
                    key_hold_ms = int(key_hold_ms)
                except Exception:
                    key_hold_ms = 40
                key_hold_ms = max(0, min(5000, key_hold_ms))

                if not (isinstance(usage, str) and isinstance(code, str)):
                    return

                self._bt.key_down(usage=usage, code=code)
                await asyncio.sleep(key_hold_ms / 1000.0)
                self._bt.key_up(usage=usage, code=code)
                return

            return

        if domain == "tv":
            if self._tv is None:
                return
            # Prefer method on tv, else treat as raw TV key
            await self._call_action_method(self._tv, action, args)
            fn = getattr(self._tv, action, None)
            if fn is None:
                await self._tv.ws.send_key(action)
            return

        if domain == "speaker":
            sp = getattr(self, "_speaker", None)
            if sp is None:
                return
            # Hard drop if unreachable
            st = getattr(sp, "state", None)
            if st is not None and not getattr(st, "reachable", False):
                return
            await self._call_action_method(sp, action, args)
            return

    def _compile_ble_frames_once(self) -> None:
        """
        Precompile BLE actions into binary frames.

        New style: compile once per activity using the dongle link's encoder
        (which loads pihub.assets/hid_keymap.json) and store the resulting
        CompiledBleFrames keyed by activity.

        This keeps the hot path to:
        dict lookup (active frames) + enqueue bytes
        """
        compiled: Dict[str, CompiledBleFrames] = {}
        total = 0

        for activity, mapping in (self._bindings or {}).items():
            if not isinstance(mapping, dict):
                continue
            try:
                frames = self._bt.compile_ble_frames(mapping)
            except Exception:
                logger.debug("ble compile failed for activity=%s", activity, exc_info=True)
                continue
            compiled[activity] = frames
            total += (len(frames.kb_down) + len(frames.cc_down))

        self._ble_frames_by_activity = compiled
        logger.info("compiled %d ble HID codes into binary frames", total)

    def _precompile_ha_actions(self) -> None:
        """
        Precompute HA action fields once at startup so the hot path doesn't:
        - rebuild extras dict
        - re-parse min_hold_ms
        - re-derive when
        """
        for _activity, mapping in (self._bindings or {}).items():
            if not isinstance(mapping, dict):
                continue
            for _rem_key, actions in mapping.items():
                if not isinstance(actions, list):
                    continue
                for a in actions:
                    if not isinstance(a, dict) or a.get("domain") != "ha":
                        continue

                    # Store under private keys to avoid changing keymap schema.
                    a["_when"] = a.get("when", "down")

                    a["_min_hold_ms"] = (
                        parse_ms(
                            a.get("min_hold_ms"),
                            default=0,
                            min=0,
                            max=5000,
                            allow_none=False,
                            context="keymap.min_hold_ms",
                        )
                        or 0
                    )

                    a["_extras"] = {
                        k: v
                        for k, v in a.items()
                        if k
                        not in {
                            "domain",
                            "when",
                            "action",
                            "min_hold_ms",
                            "_when",
                            "_min_hold_ms",
                            "_extras",
                        }
                    }

    # Activity comes from HA (ha_ws)
    async def on_activity(self, text: Optional[str]) -> None:
        """Record the current activity reported by Home Assistant."""
        prior = self._activity
        self._activity = text

        # Cache mapping so per-edge dispatch is a single dict lookup.
        if text is None:
            self._active_bindings = {}
        else:
            self._active_bindings = self._bindings.get(text, {}) or {}

        # Cache compiled BLE frames for this activity (or None).
        self._active_ble_frames = None if text is None else self._ble_frames_by_activity.get(text)

        if (prior is None) != (text is None):
            self._activity_none_logged = False

    # USB edges come from UnifyingReader
    async def on_usb_edge(self, rem_key: str, edge: str) -> None:
        """Handle a key edge originating from the USB receiver."""
        loop = asyncio.get_running_loop()

        if self._activity is None and not self._activity_none_logged:
            logger.info("activity not set yet; ignoring input until HA activity arrives")
            self._activity_none_logged = True

        if not await self._update_press_state(rem_key, edge, loop):
            return

        actions = self._active_bindings.get(rem_key, [])
        # enumerate actions so we can key per-action hold tasks
        for idx, a in enumerate(actions):
            await self._do_action(a, edge, rem_key=rem_key, action_index=idx)

        # Forced repeat starts on down for physical volume keys only.
        if edge == "down" and rem_key in _REPEAT_KEYS:
            await self._start_repeat(rem_key)

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
            self._pressed_at[rem_key] = loop.time()
            await self._cancel_hold_tasks(rem_key)
            return True

        if edge == "up":
            await self._stop_repeat(rem_key)
            await self._cancel_hold_tasks(rem_key)
            return True

        return False

    async def on_usb_disconnect(self) -> None:
        """Handle USB disconnects to prevent stuck repeats."""
        await self._cancel_all_repeat_tasks()
        await self._cancel_all_hold_tasks()
        self._pressed_at.clear()
        with suppress(Exception):
            self._bt.release_all()

    # ---- Repeat helpers (forced by physical key: rem_vol_up/rem_vol_down) ----
    async def _start_repeat(self, rem_key: str) -> None:
        if rem_key in self._repeat_tasks:
            return

        async def _runner():
            try:
                await asyncio.sleep(REPEAT_INITIAL_MS / 1000.0)
                while True:
                    actions = self._active_bindings.get(rem_key, []) or []
                    for idx, a in enumerate(actions):
                        if not isinstance(a, dict):
                            continue
                        if a.get("domain") == "ble":
                            continue
                        when = a.get("when", "down")
                        if when != "down":
                            continue
                        await self._do_action(a, "down", rem_key=rem_key, action_index=idx)

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

    # ---- Hold-trigger helpers (HA bus only; no repeat) ----
    async def _schedule_hold_ha(
        self,
        rem_key: str,
        action_index: int,
        min_hold_ms: int,
        action: str,
        extras: dict,
    ) -> None:
        """
        Schedule a delayed fire for "when=down" + min_hold_ms. If key is released
        before the delay, the task is cancelled and nothing is sent.
        """
        key = (rem_key, action_index)
        if key in self._hold_tasks:
            return

        async def _hold_runner():
            try:
                await asyncio.sleep(max(0, min_hold_ms) / 1000.0)
                if rem_key in self._pressed_at:
                    await self._send_cmd(action=action, **extras)
            except asyncio.CancelledError:
                pass
            finally:
                self._hold_tasks.pop(key, None)

        self._hold_tasks[key] = asyncio.create_task(_hold_runner(), name=f"hold:{rem_key}:{action_index}")

    async def _cancel_hold_tasks(self, rem_key: str) -> None:
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
        domain = a.get("domain")

        if domain == "noop":
            return

        # Optional edge filter for non-BLE actions
        when = a.get("when", "down")
        if domain != "ble" and edge != when:
            return

        if domain == "ble":
            await self._handle_ble_action(a, edge)
            return

        if domain == "ha":
            await self._handle_ha_action(a, edge, rem_key=rem_key, action_index=action_index)
            return

        if domain == "tv":
            await self._handle_tv_action(a, edge, rem_key=rem_key, action_index=action_index)
            return

        if domain == "speaker":
            await self._handle_speaker_action(a, edge, rem_key=rem_key, action_index=action_index)
            return

    async def _handle_ble_action(self, a: dict, edge: str) -> None:
        """Handle edge-accurate BLE actions (never repeat)."""
        usage = a.get("usage")
        code = a.get("code")
        if not (isinstance(usage, str) and isinstance(code, str)):
            return

        frames = self._active_ble_frames
        if frames is not None:
            if edge == "down":
                self._bt.compiled_key_down(frames, usage=usage, code=code)
            elif edge == "up":
                self._bt.compiled_key_up(frames, usage=usage, code=code)
            return

        # Fallback: encode on-demand (should be rare once compiled)
        if edge == "down":
            self._bt.key_down(usage=usage, code=code)
        elif edge == "up":
            self._bt.key_up(usage=usage, code=code)

    async def _handle_speaker_action(
        self,
        a: dict,
        edge: str,
        *,
        rem_key: str | None,
        action_index: int = 0,
    ) -> None:
        """Handle speaker actions (generic method dispatch)."""
        if edge != "down":
            return

        sp = getattr(self, "_speaker", None)
        if sp is None:
            return

        # Hard drop if speaker not reachable
        try:
            st = getattr(sp, "state", None)
            if st is not None and not getattr(st, "reachable", False):
                return
        except Exception:
            return

        action = a.get("action")
        if not isinstance(action, str) or not action:
            return

        kwargs = self._action_kwargs(a)
        await self._call_action_method(sp, action, kwargs)

    async def _handle_tv_action(
        self,
        a: dict,
        edge: str,
        *,
        rem_key: str | None,
        action_index: int = 0,
    ) -> None:
        """Handle TV actions (generic method dispatch + raw-key fallback)."""
        if edge != "down":
            return
        if self._tv is None:
            return

        action = a.get("action")

        if isinstance(action, str) and action:
            kwargs = self._action_kwargs(a)
            fn = getattr(self._tv, action, None)
            if fn is not None and callable(fn) and inspect.iscoroutinefunction(fn):
                await fn(**kwargs)
                return

            # Treat as raw KEY_* string
            await self._tv.ws.send_key(action)
            return

    async def _handle_ha_action(
        self,
        a: dict,
        edge: str,
        *,
        rem_key: Optional[str],
        action_index: int,
    ) -> None:
        """Handle Home Assistant bus actions (supports min_hold_ms; no repeat)."""
        cmd = a.get("action")
        if not isinstance(cmd, str):
            return

        extras = a.get("_extras")
        if not isinstance(extras, dict):
            extras = {k: v for k, v in a.items() if k not in {"domain", "when", "action", "min_hold_ms"}}

        min_hold_ms = a.get("_min_hold_ms")
        if not isinstance(min_hold_ms, int):
            min_hold_ms = (
                parse_ms(
                    a.get("min_hold_ms"),
                    default=0,
                    min=0,
                    max=5000,
                    allow_none=False,
                    context="keymap.min_hold_ms",
                )
                or 0
            )

        loop = asyncio.get_running_loop()
        when = a.get("_when")
        if not isinstance(when, str):
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
            await self._send_with_log(action=cmd, **extras)
            return

        # when == "down": fire on press; if min_hold_ms > 0, delay until threshold
        if when == "down" and edge == "down":
            if min_hold_ms > 0 and rem_key is not None:
                await self._schedule_hold_ha(
                    rem_key=rem_key,
                    action_index=action_index,
                    min_hold_ms=min_hold_ms,
                    action=cmd,
                    extras=extras,
                )
                return
            await self._send_with_log(action=cmd, **extras)
            return

    # ---- Keymap loader ----
    def _load_keymap(self) -> dict:
        identifier = "pihub.assets:keymap.json"
        logger.info("loading keymap from packaged assets: %s", identifier)
        try:
            resource = importlib_resources.files("pihub.assets") / "keymap.json"
            raw = resource.read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            raise FileNotFoundError(f"packaged keymap missing or unreadable: {identifier}") from exc

        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"packaged keymap invalid JSON ({identifier}): {exc}") from exc

        if not isinstance(doc, dict) or "scancode_map" not in doc or "activities" not in doc:
            raise ValueError(
                f"packaged keymap schema invalid ({identifier}): expected 'scancode_map' and 'activities'."
            )

        return doc

    async def _send_with_log(self, action: str, **extras: Any) -> None:
        success = await self._send_cmd(action=action, **extras)
        if success:
            return
        if not logger.isEnabledFor(logging.DEBUG):
            return
        now = time.monotonic()
        if now - self._last_cmd_fail_log < 5.0:
            return
        self._last_cmd_fail_log = now
        logger.warning("HA command send failed: %s", action)

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
                    domain = action.get("domain")
                    if domain not in {"ha", "ble", "noop", "tv", "speaker"}:
                        raise ValueError(f"action {activity}.{rem_key}[{idx}] has unknown domain={domain!r}")

                    if domain == "ha":
                        if not isinstance(action.get("action"), str):
                            raise ValueError(f"ha action {activity}.{rem_key}[{idx}] missing 'action' string")
                    elif domain == "ble":
                        if not (isinstance(action.get("usage"), str) and isinstance(action.get("code"), str)):
                            raise ValueError(f"ble action {activity}.{rem_key}[{idx}] requires 'usage' and 'code'")
