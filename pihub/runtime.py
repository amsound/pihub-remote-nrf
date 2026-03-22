"""Local runtime engine for mode + sequence control."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .flows import FlowRunner, FlowWaitTimeout
from .history import FlowRunReport, HistoryStore

logger = logging.getLogger(__name__)


class RuntimeEngine:
    """Own local mode/sequence state and route intent and device-state signals."""

    def __init__(
        self,
        *,
        dispatcher: Any | None = None,
        tv: Any = None,
        speaker: Any = None,
        ble: Any = None,
        settings: Any = None,
        history: HistoryStore | None = None,
        initial_mode: str = "power_off",
    ) -> None:
        self._dispatcher = dispatcher
        self._mode = initial_mode
        self._last_flow: str | None = None
        self._flow_running = False
        self._last_trigger: str | None = None
        self._error = False
        self._last_error: str | None = None
        self._last_result: str | None = None
        self._startup_reconciled = False
        self._lock = asyncio.Lock()
        self._active_sequence_task: asyncio.Task | None = None
        self._flows = FlowRunner(runtime=self, tv=tv, speaker=speaker, ble=ble, settings=settings)
        self._history = history

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
            "error": self._error,
            "last_error": self._last_error,
            "last_result": self._last_result,
        }

    def _set_runtime_ok(self, result: str = "ok") -> None:
        self._error = False
        self._last_error = None
        self._last_result = result

    def _set_runtime_error(self, error: str, *, result: str) -> None:
        self._error = True
        self._last_error = (error or "").strip() or None
        self._last_result = result

    def _log_trigger_kind(self, trigger: str | None) -> str:
        t = str(trigger or "").strip().lower()
        if t.startswith("remote."):
            return "remote"
        if t.startswith("device_state_change."):
            return "device-state"
        if t.startswith("startup"):
            return "startup"
        if t.startswith("http."):
            return "http"
        return t or "internal"

    def attach_dispatcher(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    async def initialize_startup_mode(self) -> dict[str, Any]:
        self._last_trigger = "startup_reconcile"
        logger.info("startup reconcile selected mode=power_off")
        result = await self.set_mode("power_off", trigger="startup_reconcile")
        if result.get("ok"):
            self._startup_reconciled = True
        return result

    async def start(self) -> None:
        await self.initialize_startup_mode()

    async def set_mode(self, name: str, *, trigger: str = "internal") -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            self._set_runtime_error("mode_name_required", result="invalid")
            return {"ok": False, "error": "mode name required"}

        if self._dispatcher is None:
            self._set_runtime_error("dispatcher_unavailable", result="failed")
            return {"ok": False, "error": "dispatcher unavailable"}

        current_task = asyncio.current_task()
        if self._flow_running and current_task is not self._active_sequence_task:
            logger.info("mode ignored name=%s trigger=%s reason=sequence_running", name, trigger)
            self._set_runtime_error("sequence_running", result="busy")
            return {
                "ok": False,
                "domain": "mode",
                "action": "set",
                "requested_mode": name,
                "trigger": trigger,
                "reason": "sequence_running",
            }

        valid_modes_fn = getattr(self._dispatcher, "available_modes", None)
        valid_modes = set(valid_modes_fn()) if callable(valid_modes_fn) else set()
        if valid_modes and name not in valid_modes:
            logger.warning(
                "invalid mode rejected name=%s trigger=%s valid_modes=%s",
                name,
                trigger,
                sorted(valid_modes),
            )
            self._set_runtime_error("invalid_mode", result="invalid")
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
            logger.info("mode %s -> %s", prior, name)
        else:
            logger.info("mode unchanged (%s)", name)

        self._set_runtime_ok("ok")
        return {
            "ok": True,
            "domain": "mode",
            "action": "set",
            "mode": self._mode,
            "trigger": trigger,
        }

    async def run_flow(
        self,
        name: str,
        *,
        trigger: str = "internal",
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.run_sequence(
            name=name,
            trigger=trigger,
            source="intent",
            args=args,
        )

    async def run_sequence(
        self,
        name: str,
        *,
        trigger: str,
        source: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        source = (source or "").strip() or "intent"
        if not name:
            self._set_runtime_error("flow_name_required", result="invalid")
            return {"ok": False, "error": "flow name required"}

        if self._lock.locked():
            if source == "device_state_change":
                logger.info("device-state %s ignored (flow running)", name)
            else:
                logger.info("flow %s ignored (flow running)", name)

            self._set_runtime_error("runner_busy", result="busy")
            return {
                "ok": False,
                "domain": "flow",
                "action": "run",
                "name": name,
                "trigger": trigger,
                "source": source,
                "reason": "runner_busy",
            }

        report = FlowRunReport(
            flow_name=name,
            trigger=trigger,
            source=source,
        )
        if self._history is not None:
            self._history.add_flow_report(report)

        async with self._lock:
            self._flow_running = True
            self._last_trigger = trigger

            if self._history is not None:
                self._history.emit(
                    kind="flow_started",
                    message=f"flow {name} started",
                    flow_name=name,
                    trigger=trigger,
                    metadata={"source": source, "report_id": report.id},
                )

            logger.info("flow %s started (trigger=%s)", name, self._log_trigger_kind(trigger))

            seq_task = asyncio.create_task(
                self._flows.run(name=name, trigger=trigger, args=args, source=source, report=report),
                name=f"sequence:{name}",
            )
            self._active_sequence_task = seq_task

            try:
                try:
                    ok = await asyncio.shield(seq_task)
                except asyncio.CancelledError:
                    logger.exception(
                        "sequence caller cancelled name=%s trigger=%s source=%s; waiting for sequence task to finish",
                        name,
                        trigger,
                        source,
                    )
                    ok = await asyncio.shield(seq_task)

                if ok:
                    logical_name = {
                        "listen_signal": "listen",
                        "watch_signal": "watch",
                    }.get(name, name)
                    self._last_flow = logical_name
                    self._set_runtime_ok("ok")

                    result_name = "ok_with_warnings" if report.warnings else "ok"
                    report.finish(result=result_name)

                    if self._history is not None:
                        self._history.emit(
                            kind="flow_finished",
                            message=f"flow {name} completed",
                            flow_name=name,
                            trigger=trigger,
                            metadata={
                                "source": source,
                                "report_id": report.id,
                                "result": result_name,
                                "warning_count": len(report.warnings),
                                "duration_ms": report.to_dict().get("duration_ms"),
                            },
                        )

                    logger.info("flow %s completed", name)
                    return {
                        "ok": True,
                        "domain": "flow",
                        "action": "run",
                        "name": name,
                        "mode": self._mode,
                        "last_flow": self._last_flow,
                        "trigger": trigger,
                        "source": source,
                        "report_id": report.id,
                        "result": result_name,
                    }

                self._set_runtime_error("flow_failed", result="failed")
                report.finish(result="failed", error="flow_failed")

                if self._history is not None:
                    self._history.emit(
                        kind="flow_failed",
                        message=f"flow {name} failed",
                        level="error",
                        flow_name=name,
                        trigger=trigger,
                        metadata={
                            "source": source,
                            "report_id": report.id,
                            "error": "flow_failed",
                        },
                    )

                return {
                    "ok": False,
                    "domain": "flow",
                    "action": "run",
                    "name": name,
                    "trigger": trigger,
                    "source": source,
                    "error": "flow_failed",
                    "report_id": report.id,
                }

            except FlowWaitTimeout as exc:
                logger.warning(
                    "sequence wait timeout name=%s trigger=%s source=%s error=%s",
                    name,
                    trigger,
                    source,
                    str(exc),
                )
                self._set_runtime_error(str(exc), result="failed")
                report.finish(result="failed", error=str(exc))

                if self._history is not None:
                    self._history.emit(
                        kind="flow_failed",
                        message=f"flow {name} failed",
                        level="error",
                        flow_name=name,
                        trigger=trigger,
                        metadata={
                            "source": source,
                            "report_id": report.id,
                            "error": str(exc),
                        },
                    )

                return {
                    "ok": False,
                    "domain": "flow",
                    "action": "run",
                    "name": name,
                    "trigger": trigger,
                    "source": source,
                    "error": str(exc),
                    "report_id": report.id,
                }

            except Exception as exc:
                logger.exception("sequence failed name=%s trigger=%s source=%s", name, trigger, source)
                self._set_runtime_error(str(exc), result="failed")
                report.finish(result="failed", error=str(exc))

                if self._history is not None:
                    self._history.emit(
                        kind="flow_failed",
                        message=f"flow {name} failed",
                        level="error",
                        flow_name=name,
                        trigger=trigger,
                        metadata={
                            "source": source,
                            "report_id": report.id,
                            "error": str(exc),
                        },
                    )

                return {
                    "ok": False,
                    "domain": "flow",
                    "action": "run",
                    "name": name,
                    "trigger": trigger,
                    "source": source,
                    "error": str(exc),
                    "report_id": report.id,
                }

            finally:
                self._active_sequence_task = None
                self._flow_running = False

    async def on_device_state_change(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        payload = payload or {}

        if not name:
            self._set_runtime_error("state_change_name_required", result="invalid")
            return {"ok": False, "error": "state change name required"}

        if self._lock.locked():
            logger.info("device-state %s ignored (flow running)", name)
            self._set_runtime_error("runner_busy", result="busy")
            return {
                "ok": False,
                "name": name,
                "source": "device_state_change",
                "reason": "runner_busy",
            }

        # Idempotence for device signals uses last logical flow, not current mode.
        if name == "listen" and self._last_flow == "listen":
            logger.info("device-state listen ignored (already listen)")
            return {"ok": False, "name": name, "reason": "last_flow_listen"}

        if name == "watch" and self._last_flow == "watch":
            logger.info("device-state watch ignored (already watch)")
            return {"ok": False, "name": name, "reason": "last_flow_watch"}

        sequence_name = {
            "listen": "listen_signal",
            "watch": "watch_signal",
        }.get(name)

        if not sequence_name:
            self._set_runtime_error("unknown_device_state_change", result="invalid")
            return {
                "ok": False,
                "name": name,
                "reason": "unknown_device_state_change",
            }

        return await self.run_sequence(
            name=sequence_name,
            trigger=f"device_state_change.{name}",
            source="device_state_change",
            args=payload,
        )

    async def on_cmd(self, payload: dict[str, Any]) -> dict[str, Any]:
        domain = str(payload.get("domain") or "").strip().lower()
        action = str(payload.get("action") or "").strip().lower()
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            self._set_runtime_error("args_must_be_object", result="invalid")
            return {"ok": False, "error": "args must be an object"}
        if domain == "flow" and action == "run":
            return await self.run_flow(
                str(args.get("name") or ""),
                trigger=str(args.get("trigger") or "http.command"),
                args=args,
            )
        if domain == "mode" and action == "set":
            return await self.set_mode(
                str(args.get("name") or ""),
                trigger=str(args.get("trigger") or "http.command"),
            )
        self._set_runtime_error("unsupported_command", result="invalid")
        return {
            "ok": False,
            "error": "unsupported_command",
            "domain": domain,
            "action": action,
        }