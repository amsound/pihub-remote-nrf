"""Route remote key events to BLE, TV or Speaker methods"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .ble_dongle import CompiledBleFrames
from .validation import parse_ms

try:
    from importlib import resources as importlib_resources
except ImportError:  # pragma: no cover - fallback for older Python
    import importlib_resources  # type: ignore

_DIRECT_FAIL_LOG_INTERVAL_S = 60.0

# Repeat knobs (volume only: rem_vol_up/rem_vol_down). BLE never repeats.
REPEAT_INITIAL_MS = 400
REPEAT_RATE_MS = 300

_REPEAT_KEYS = {"rem_vol_up", "rem_vol_down"}

_METHOD_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_DENY_CALLS = {
    # lifecycle / internal methods that should never be callable from keymap
    "start",
    "stop",
    "_runner",
    "snapshot",
}

EdgeCB = Callable[[str, str], Awaitable[None]] | Callable[[str, str], None]

logger = logging.getLogger(__name__)


class Dispatcher:
    """
    Routes remote key edges to actions defined per-mode in keymap.json:

    - Flow action:
        { "domain": "flow", "action": "run", "name": "<flow-name>",
        "when"?: "down"|"up" (default "down"),
        "min_hold_ms"?: <int>
        }

    - BLE HID edge-accurate action:
        { "domain": "ble", "usage": "keyboard"|"consumer", "code": "<hid-name>" }

    - No-op:
        { "domain": "noop" }

    Notes:
    - Key-repeat is *not* part of keymap schema. It is forced for rem_vol_up/rem_vol_down only.
    - Repeat applies to TV/Speaker volume only.
    """

    def __init__(
        self,
        cfg: Any,
        ble: Any,
        tv: Any = None,
        speaker: Any = None,
        settings: Any = None,
        run_flow: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._cfg = cfg
        self._ble = ble
        self._tv = tv
        self._speaker = speaker
        self._settings = settings
        self._run_flow = run_flow
        self._last_cmd_fail_log = 0.0

        # Load full keymap document, then split into parts we use
        km = self._load_keymap()
        try:
            self._validate_keymap(km)
            self._scancode_map: Dict[str, str] = dict(km["scancode_map"])
            self._bindings: Dict[str, Dict[str, List[Dict[str, Any]]]] = dict(km["modes"])
            if not isinstance(self._scancode_map, dict) or not isinstance(self._bindings, dict):
                raise TypeError
        except Exception as exc:
            raise ValueError(
                "keymap.json schema invalid: expected 'scancode_map' (dict) and 'modes' (dict)."
            ) from exc

        self._mode: Optional[str] = None
        self._mode_none_logged = False
        self._active_bindings: Dict[str, List[Dict[str, Any]]] = {}

        # Active repeat tasks keyed by rem_* (per-key)
        self._repeat_tasks: Dict[str, asyncio.Task] = {}

        # Press timing (seconds from loop.time()) keyed by rem_*
        self._pressed_at: Dict[str, float] = {}

        # Delayed hold triggers: (rem_key, action_index) -> task
        self._hold_tasks: Dict[Tuple[str, int], asyncio.Task] = {}

        # Precompiled BLE frames per mode (hot path)
        self._ble_frames_by_mode: Dict[str, CompiledBleFrames] = {}
        self._active_ble_frames: Optional[CompiledBleFrames] = None
        self._compile_ble_frames_once()

        # Summary: count modes and scancodes
        acts = len(self._bindings)
        scan_total = len(self._scancode_map)
        logger.info("keymap loaded: %s modes, %s scancodes", acts, scan_total)

    @property
    def scancode_map(self) -> Dict[str, str]:
        """Public accessor for the logical rem_* scancode map."""
        return self._scancode_map

    def available_modes(self) -> set[str]:
        """Return the valid mode names loaded from keymap.json."""
        return set(self._bindings.keys())

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

    def _log_direct_failure(
        self,
        *,
        domain: str,
        reason: str,
        action: str | None,
        rem_key: str | None,
    ) -> None:
        now = time.monotonic()
        if (now - self._last_cmd_fail_log) < _DIRECT_FAIL_LOG_INTERVAL_S:
            return
        self._last_cmd_fail_log = now
        logger.warning(
            "direct action dropped domain=%s reason=%s action=%s rem_key=%s",
            domain,
            reason,
            action,
            rem_key,
        )

    def _clear_direct_failure_latch(self) -> None:
        self._last_cmd_fail_log = 0.0

def _set_tv_direct_fault(self, reason: str) -> None:
    tv = getattr(self, "_tv", None)
    if tv is None:
        return
    try:
        ws = getattr(tv, "ws", None)
        state = getattr(ws, "state", None)
        if state is not None:
            state.last_error = reason
    except Exception:
        logger.debug("failed to set tv direct fault reason=%s", reason, exc_info=True)


def _clear_tv_direct_fault(self) -> None:
    tv = getattr(self, "_tv", None)
    if tv is None:
        return
    try:
        ws = getattr(tv, "ws", None)
        state = getattr(ws, "state", None)
        if state is not None:
            current = str(getattr(state, "last_error", "") or "")
            if current.startswith("direct_action_"):
                state.last_error = ""
    except Exception:
        logger.debug("failed to clear tv direct fault", exc_info=True)


    def _set_speaker_direct_fault(self, reason: str) -> None:
        sp = getattr(self, "_speaker", None)
        if sp is None:
            return
        try:
            state = getattr(sp, "state", None)
            if state is not None:
                state.last_error = reason
        except Exception:
            logger.debug("failed to set speaker direct fault reason=%s", reason, exc_info=True)


    def _clear_speaker_direct_fault(self) -> None:
        sp = getattr(self, "_speaker", None)
        if sp is None:
            return
        try:
            state = getattr(sp, "state", None)
            if state is not None:
                current = str(getattr(state, "last_error", "") or "")
                if current.startswith("direct_action_"):
                    state.last_error = None
        except Exception:
            logger.debug("failed to clear speaker direct fault", exc_info=True)

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

    def _compile_ble_frames_once(self) -> None:
        """
        Precompile BLE actions into binary frames.

        New style: compile once per mode using the dongle link's encoder
        (which loads pihub.assets/hid_keymap.json) and store the resulting
        CompiledBleFrames keyed by mode.

        This keeps the hot path to:
        dict lookup (active frames) + enqueue bytes
        """
        compiled: Dict[str, CompiledBleFrames] = {}
        total = 0

        for mode, mapping in (self._bindings or {}).items():
            if not isinstance(mapping, dict):
                continue
            try:
                frames = self._ble.compile_ble_frames(mapping)
            except Exception:
                logger.debug("ble compile failed for mode=%s", mode, exc_info=True)
                continue
            compiled[mode] = frames
            total += (len(frames.kb_down) + len(frames.cc_down))

        self._ble_frames_by_mode = compiled
        logger.info("compiled %d ble HID codes into binary frames", total)

    async def set_mode_bindings(self, mode: Optional[str]) -> None:
        """Apply the current mode by selecting its key bindings and BLE frame cache."""
        prior_mode = self._mode
        self._mode = mode

        if mode is None:
            self._active_bindings = {}
        else:
            self._active_bindings = self._bindings.get(mode, {}) or {}

        self._active_ble_frames = None if mode is None else self._ble_frames_by_mode.get(mode)

        if (prior_mode is None) != (mode is None):
            self._mode_none_logged = False

    # USB edges come from UnifyingReader
    async def on_usb_edge(self, rem_key: str, edge: str) -> None:
        """Handle a key edge originating from the USB receiver."""
        loop = asyncio.get_running_loop()

        if self._mode is None and not self._mode_none_logged:
            logger.info("mode not set yet; ignoring input until mode set")
            self._mode_none_logged = True

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
            self._ble.release_all()

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

    # ---- Hold-trigger helpers ----
    async def _schedule_hold_action(
        self,
        rem_key: str,
        action_index: int,
        min_hold_ms: int,
        callback: Callable[[], Awaitable[None]],
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
                    await callback()
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
        
        if domain == "flow":
            await self._handle_flow_action(a, edge, rem_key=rem_key, action_index=action_index)
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
                self._ble.compiled_key_down(frames, usage=usage, code=code)
            elif edge == "up":
                self._ble.compiled_key_up(frames, usage=usage, code=code)
            return

        # Fallback: encode on-demand (should be rare once compiled)
        if edge == "down":
            self._ble.key_down(usage=usage, code=code)
        elif edge == "up":
            self._ble.key_up(usage=usage, code=code)

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
            self._log_direct_failure(
                domain="speaker",
                reason="speaker_missing",
                action=str(a.get("action") or ""),
                rem_key=rem_key,
            )
            return

        try:
            st = getattr(sp, "state", None)
            if st is not None:
                reachable = bool(getattr(st, "reachable", False))
                connected = bool(getattr(st, "connected", False))
                ready = bool(getattr(st, "ready", False))

                if not reachable:
                    self._set_speaker_direct_fault("direct_action_speaker_not_reachable")
                    self._log_direct_failure(
                        domain="speaker",
                        reason="speaker_unreachable",
                        action=str(a.get("action") or ""),
                        rem_key=rem_key,
                    )
                    return

                if not connected:
                    self._set_speaker_direct_fault("direct_action_speaker_not_connected")
                    self._log_direct_failure(
                        domain="speaker",
                        reason="speaker_not_connected",
                        action=str(a.get("action") or ""),
                        rem_key=rem_key,
                    )
                    return

                if not ready:
                    self._set_speaker_direct_fault("direct_action_speaker_not_ready")
                    self._log_direct_failure(
                        domain="speaker",
                        reason="speaker_not_ready",
                        action=str(a.get("action") or ""),
                        rem_key=rem_key,
                    )
                    return
        except Exception:
            self._set_speaker_direct_fault("direct_action_speaker_state_error")
            self._log_direct_failure(
                domain="speaker",
                reason="speaker_state_error",
                action=str(a.get("action") or ""),
                rem_key=rem_key,
            )
            return

        action = a.get("action")
        if not isinstance(action, str) or not action:
            return

        if action == "play_stream_url":
            if self._settings is None:
                self._set_speaker_direct_fault("direct_action_settings_missing")
                self._log_direct_failure(
                    domain="speaker",
                    reason="settings_missing",
                    action=action,
                    rem_key=rem_key,
                )
                return
            try:
                slot = int(a.get("slot"))
            except Exception:
                self._set_speaker_direct_fault("direct_action_invalid_stream_slot")
                self._log_direct_failure(
                    domain="speaker",
                    reason="invalid_stream_slot",
                    action=action,
                    rem_key=rem_key,
                )
                return
            url = self._settings.get_stream_url(slot)
            if not url:
                logger.info("speaker stream slot empty slot=%s", slot)
                return
            try:
                await sp.play_url(url)
                self._clear_speaker_direct_fault()
                self._clear_direct_failure_latch()
            except Exception:
                self._set_speaker_direct_fault("direct_action_play_url_failed")
                self._log_direct_failure(
                    domain="speaker",
                    reason="play_url_failed",
                    action=action,
                    rem_key=rem_key,
                )
            return

        kwargs = self._action_kwargs(a)
        try:
            await self._call_action_method(sp, action, kwargs)
            self._clear_speaker_direct_fault()
            self._clear_direct_failure_latch()
        except Exception:
            self._set_speaker_direct_fault("direct_action_speaker_action_failed")
            self._log_direct_failure(
                domain="speaker",
                reason="speaker_action_failed",
                action=action,
                rem_key=rem_key,
            )

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
            self._log_direct_failure(
                domain="tv",
                reason="tv_missing",
                action=str(a.get("action") or ""),
                rem_key=rem_key,
            )
            return

        action = a.get("action")
        if not isinstance(action, str) or not action:
            return

        kwargs = self._action_kwargs(a)
        fn = getattr(self._tv, action, None)
        if fn is not None and callable(fn) and inspect.iscoroutinefunction(fn):
            try:
                result = await fn(**kwargs)
                if result is False:
                    self._set_tv_direct_fault("direct_action_tv_not_connected")
                    self._log_direct_failure(
                        domain="tv",
                        reason="tv_not_connected",
                        action=action,
                        rem_key=rem_key,
                    )
                    return
                self._clear_tv_direct_fault()
                self._clear_direct_failure_latch()
            except Exception:
                self._set_tv_direct_fault("direct_action_tv_action_failed")
                self._log_direct_failure(
                    domain="tv",
                    reason="tv_action_failed",
                    action=action,
                    rem_key=rem_key,
                )
            return

        try:
            ok = await self._tv.ws.send_key(action)
            if ok is False:
                self._set_tv_direct_fault("direct_action_tv_not_connected")
                self._log_direct_failure(
                    domain="tv",
                    reason="tv_send_failed",
                    action=action,
                    rem_key=rem_key,
                )
                return
            self._clear_tv_direct_fault()
            self._clear_direct_failure_latch()
        except Exception:
            self._set_tv_direct_fault("direct_action_tv_send_exception")
            self._log_direct_failure(
                domain="tv",
                reason="tv_send_exception",
                action=action,
                rem_key=rem_key,
            )
            return

    async def _handle_flow_action(
        self,
        a: dict,
        edge: str,
        *,
        rem_key: Optional[str],
        action_index: int,
    ) -> None:
        """Handle local flow actions via the runtime engine."""
        action = a.get("action")
        if action != "run":
            return
        if self._run_flow is None:
            return

        name = a.get("name")
        if not isinstance(name, str) or not name:
            return

        trigger = f"remote.{rem_key}" if rem_key else "remote"
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
        when = a.get("when", "down")

        async def _run() -> None:
            task = asyncio.create_task(
                self._run_flow(name=name, trigger=trigger),
                name=f"flow:{name}:{trigger}",
            )

            def _log_flow_result(t: asyncio.Task) -> None:
                try:
                    _ = t.result()
                except asyncio.CancelledError:
                    logger.info("spawned flow task cancelled name=%s trigger=%s", name, trigger)
                except Exception:
                    logger.exception("spawned flow task failed name=%s trigger=%s", name, trigger)

            task.add_done_callback(_log_flow_result)

        loop = asyncio.get_running_loop()

        if when == "up" and edge == "up":
            if min_hold_ms > 0 and rem_key:
                t0 = self._pressed_at.get(rem_key)
                if t0 is None:
                    return
                elapsed_ms = int((loop.time() - t0) * 1000.0)
                if elapsed_ms < min_hold_ms:
                    return
            await _run()
            return

        if when == "down" and edge == "down":
            if min_hold_ms > 0 and rem_key is not None:
                await self._schedule_hold_action(
                    rem_key=rem_key,
                    action_index=action_index,
                    min_hold_ms=min_hold_ms,
                    callback=_run,
                )
                return
            await _run()
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

        if not isinstance(doc, dict) or "scancode_map" not in doc or "modes" not in doc:
            raise ValueError(
                f"packaged keymap schema invalid ({identifier}): expected 'scancode_map' and 'modes'."
            )

        return doc

    @staticmethod
    def _validate_keymap(doc: dict) -> None:
        if not isinstance(doc, dict):
            raise ValueError("keymap.json must be a dict")

        modes = doc.get("modes")
        if not isinstance(modes, dict):
            raise ValueError("keymap.json 'modes' must be a dict")

        for mode, mapping in modes.items():
            if not isinstance(mapping, dict):
                raise ValueError(f"mode '{mode}' must map to a dict of actions")
            for rem_key, actions in mapping.items():
                if not isinstance(actions, list):
                    raise ValueError(f"actions for '{mode}.{rem_key}' must be a list")
                for idx, action in enumerate(actions):
                    if not isinstance(action, dict):
                        raise ValueError(f"action {mode}.{rem_key}[{idx}] must be a dict")
                    domain = action.get("domain")
                    if domain not in {"ble", "noop", "tv", "speaker", "flow"}:
                        raise ValueError(f"action {mode}.{rem_key}[{idx}] has unknown domain={domain!r}")
                    elif domain == "flow":
                        if action.get("action") != "run":
                            raise ValueError(f"flow action {mode}.{rem_key}[{idx}] requires action='run'")
                        if not isinstance(action.get("name"), str):
                            raise ValueError(f"flow action {mode}.{rem_key}[{idx}] missing 'name' string")
                    elif domain == "ble":
                        if not (isinstance(action.get("usage"), str) and isinstance(action.get("code"), str)):
                            raise ValueError(f"ble action {mode}.{rem_key}[{idx}] requires 'usage' and 'code'")