"""Local runtime engine for mode + flow control."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from .flows import FlowRunner

logger = logging.getLogger(__name__)


class RuntimeEngine:
    """Owns local mode/flow state and exposes a unified command entrypoint."""

    def __init__(
        self,
        *,
        dispatcher: Any | None = None,
        tv: Any = None,
        speaker: Any = None,
        ble: Any = None,
        initial_mode: str = "power_off",
    ) -> None:
        self._dispatcher = dispatcher
        self._mode = initial_mode
        self._last_flow: str | None = None
        self._flow_running = False
        self._last_trigger: str | None = None
        self._startup_reconciled = False
        self._lock = asyncio.Lock()
        self._flows = FlowRunner(runtime=self, tv=tv, speaker=speaker, ble=ble)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def last_flow(self) -> str | None:
        return self._last_flow

    @property
    def flow_running(self) -> bool:
        return self._flow_running

    @property
    def last_trigger(self) -> str | None:
        return self._last_trigger

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "last_flow": self._last_flow,
            "flow_running": self._flow_running,
            "last_trigger": self._last_trigger,
        }

    def attach_dispatcher(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    async def reconcile_startup_state(self) -> dict[str, Any]:
        self._last_trigger = "startup_reconcile"
        mode = self._infer_startup_mode()
        result = await self.set_mode(mode, trigger="startup_reconcile")
        if result.get("ok"):
            self._startup_reconciled = True
        return result

    def _infer_startup_mode(self) -> str:
        # Strongest signal first: TV on => watch
        try:
            tv = getattr(self._flows, "_tv", None)
            if tv is not None:
                snap = tv.snapshot()
                if snap.logical_on is True:
                    logger.info("startup reconcile selected mode=watch")
                    return "watch"
        except Exception:
            pass

        # Music-ish playback => listen
        try:
            speaker = getattr(self._flows, "_speaker", None)
            if speaker is not None:
                snap = speaker.snapshot()
                playback = (snap.get("playback_status") or "").strip().lower()
                source = (snap.get("source") or "").strip().lower()
                if playback == "play" and source in {"airplay", "wifi", "multiroom-secondary"}:
                    logger.info("startup reconcile selected mode=listen")
                    return "listen"
        except Exception:
            pass

        logger.info("startup reconcile selected mode=power_off")
        return "power_off"

    async def start(self) -> None:
        await self.reconcile_startup_state()

    async def set_mode(self, name: str, *, trigger: str = "internal") -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "mode name required"}

        if self._dispatcher is None:
            return {"ok": False, "error": "dispatcher unavailable"}

        valid_modes_fn = getattr(self._dispatcher, "available_modes", None)
        valid_modes = set(valid_modes_fn()) if callable(valid_modes_fn) else set()

        if valid_modes and name not in valid_modes:
            logger.warning(
                "invalid mode rejected name=%s trigger=%s valid_modes=%s",
                name,
                trigger,
                sorted(valid_modes),
            )
            return {
                "ok": False,
                "domain": "mode",
                "action": "set",
                "error": "invalid_mode",
                "requested_mode": name,
                "valid_modes": sorted(valid_modes),
                "trigger": trigger,
            }

        prior = self._mode

        await self._dispatcher.set_mode_bindings(name)
        self._last_trigger = trigger
        self._mode = name

        if prior != name:
            logger.info("mode changed %s -> %s trigger=%s", prior, name, trigger)
        else:
            logger.info("mode unchanged %s trigger=%s", name, trigger)

        return {
            "ok": True,
            "domain": "mode",
            "action": "set",
            "mode": self._mode,
            "trigger": trigger,
        }

    async def run_flow(self, name: str, *, trigger: str = "internal") -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "flow name required"}

        if self._lock.locked():
            logger.info("flow ignored name=%s trigger=%s reason=runner_busy", name, trigger)
            return {
                "ok": False,
                "domain": "flow",
                "action": "run",
                "name": name,
                "trigger": trigger,
                "reason": "runner_busy",
            }

        async with self._lock:
            self._flow_running = True
            self._last_trigger = trigger
            logger.info("flow started name=%s trigger=%s", name, trigger)
            try:
                ok = await self._flows.run(name=name, trigger=trigger)
                if ok:
                    self._last_flow = name
                else:
                    return {
                        "ok": False,
                        "domain": "flow",
                        "action": "run",
                        "name": name,
                        "trigger": trigger,
                        "error": "flow_failed",
                    }
                logger.info("flow completed name=%s trigger=%s", name, trigger)
                return {
                    "ok": True,
                    "domain": "flow",
                    "action": "run",
                    "name": name,
                    "mode": self._mode,
                    "last_flow": self._last_flow,
                    "trigger": trigger,
                }
            except Exception as exc:
                logger.exception("flow failed name=%s trigger=%s", name, trigger)
                return {
                    "ok": False,
                    "domain": "flow",
                    "action": "run",
                    "name": name,
                    "trigger": trigger,
                    "error": str(exc),
                }
            finally:
                self._flow_running = False