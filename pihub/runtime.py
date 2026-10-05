"""Local runtime engine for mode + sequence control."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .flows import FlowRunner, FlowStepFailures
from .history import FlowRunReport, HistoryStore

logger = logging.getLogger(__name__)

DEFAULT_MODE_PATH = "/data/mode.json"
# Used only when no mode has been remembered yet (first ever start, or the file is gone).
FALLBACK_MODE = "power_off"
# How long start-up waits for the TV and speaker to give their first reading.
STARTUP_SETTLE_S = 5.0


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
        speaker_backend: str | None = None,
        mode_path: str = DEFAULT_MODE_PATH,
    ) -> None:
        self._dispatcher = dispatcher
        self._tv = tv
        self._speaker = speaker
        self._mode_path = mode_path
        # One thread, so saves land in the order they were made.
        self._mode_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mode-writer")
        self._mode = FALLBACK_MODE
        # Set once a flow runs or a mode is set after start-up. From then on the
        # user's intent stands and start-up must not second-guess it.
        self._acted = False
        self._startup_task: asyncio.Task | None = None
        self._remember_failed = False
        self._last_flow: str | None = None
        self._flow_running = False
        self._last_trigger: str | None = None
        self._last_trigger_at: float | None = None  # wall clock, set only when a trigger happens
        self._error = False
        self._last_error: str | None = None
        self._last_result: str | None = None
        self._lock = asyncio.Lock()
        self._active_sequence_task: asyncio.Task | None = None
        self._flows = FlowRunner(
            tv=tv,
            speaker=speaker,
            ble=ble,
            settings=settings,
            speaker_backend=speaker_backend,
        )
        self._history = history

    @property
    def mode(self) -> str:
        return self._mode

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "last_flow": self._last_flow,
            "flow_running": self._flow_running,
            "last_trigger": self._last_trigger,
            "last_trigger_at": self._last_trigger_at,
            "error": self._error,
            "last_error": self._last_error,
            "last_result": self._last_result,
        }

    def _note_trigger(self, trigger: str) -> None:
        self._last_trigger = trigger
        self._last_trigger_at = time.time()

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
        if t.startswith("http.remote"):
            return "web remote"
        if t == "http.status":
            return "status page"
        if t == "http.ha":
            return "home assistant"
        if t.startswith("http."):
            return "http"
        return t or "internal"

    def attach_dispatcher(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    # ---- start-up: nothing is sent to any device ----

    async def start(self) -> None:
        """Restore the remembered mode, then check it against the devices once they report."""
        mode, last_flow = self._load_remembered()
        valid = self._valid_modes()
        if valid and mode not in valid:
            mode, last_flow = FALLBACK_MODE, None
        await self._dispatcher.set_mode_bindings(mode)
        self._mode = mode
        self._last_flow = last_flow
        self._note_trigger("startup")
        self._set_runtime_ok("ok")
        logger.info("startup: mode %s (%s)", mode, "remembered" if last_flow is not None else "default")
        self._startup_task = asyncio.create_task(self._correct_startup_mode(), name="runtime:startup")
        self._startup_task.add_done_callback(self._startup_done)

    @staticmethod
    def _startup_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("startup mode check failed; the remembered mode stands", exc_info=task.exception())

    async def stop(self) -> None:
        task, self._startup_task = self._startup_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(self._mode_writer.shutdown)   # let the last save land

    def _valid_modes(self) -> set[str]:
        fn = getattr(self._dispatcher, "available_modes", None)
        return set(fn()) if callable(fn) else set()

    def _load_remembered(self) -> tuple[str, str | None]:
        try:
            with open(self._mode_path, encoding="utf-8") as f:
                raw = json.load(f)
            mode = str(raw["mode"])
            return mode, str(raw.get("last_flow") or mode)
        except FileNotFoundError:
            return FALLBACK_MODE, None
        except Exception as exc:
            logger.warning("startup: %s unreadable (%s); using %s", self._mode_path, exc, FALLBACK_MODE)
            return FALLBACK_MODE, None

    def _remember(self) -> None:
        """Save the mode so the next start restores it.

        Written by the mode-writer thread: a slow SD card must not hold up key presses.
        """
        text = json.dumps({"mode": self._mode, "last_flow": self._last_flow}) + "\n"
        try:
            self._mode_writer.submit(self._write_mode, text)
        except RuntimeError:   # shutting down
            self._write_mode(text)

    def _write_mode(self, text: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._mode_path) or ".", exist_ok=True)
            tmp = f"{self._mode_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self._mode_path)
            self._remember_failed = False
        except OSError as exc:
            if not self._remember_failed:
                self._remember_failed = True
                logger.warning("could not save mode to %s: %s", self._mode_path, exc)

    async def _correct_startup_mode(self) -> None:
        """Once the TV and speaker have reported, make the mode match the room.

        Mode only: key bindings and the last-flow marker. No flow runs and
        nothing is sent to any device. Listening wins over the TV being on,
        because a listen session is the active statement of intent. Skipped if
        anyone has acted since start-up, and anything not known is left alone.
        """
        waits = [d.initial.wait(STARTUP_SETTLE_S) for d in (self._tv, self._speaker) if d is not None]
        airplay = getattr(self._speaker, "airplay_initial", None)   # the soundbar senses AirPlay separately
        if airplay is not None:
            waits.append(airplay.wait(STARTUP_SETTLE_S))
        if waits:
            await asyncio.gather(*waits)
        if self._acted or self._flow_running:
            return

        tv_on: bool | None = False
        if self._tv is not None:
            tv_on = self._tv.snapshot().presence_on if self._tv.initial.received else None
        listening: bool | None = False
        if self._speaker is not None:
            listening = self._speaker.listening() if self._speaker.initial.received else None

        if listening:
            want = "listen"
        elif tv_on:
            want = "watch"
        elif listening is None or tv_on is None:
            return   # can't tell: the remembered mode stands
        else:
            want = "power_off"

        valid = self._valid_modes()
        if want == self._mode or (valid and want not in valid):
            return
        prior = self._mode
        await self._dispatcher.set_mode_bindings(want)
        self._mode = want
        self._last_flow = want
        self._remember()
        logger.info(
            "startup: tv %s, speaker %s: mode %s -> %s",
            "on" if tv_on else "off", "listening" if listening else "idle", prior, want,
        )

    async def _commit_mode_internal(self, name: str, *, trigger: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("mode name required")

        if self._dispatcher is None:
            raise RuntimeError("dispatcher unavailable")

        valid_modes_fn = getattr(self._dispatcher, "available_modes", None)
        valid_modes = set(valid_modes_fn()) if callable(valid_modes_fn) else set()
        if valid_modes and name not in valid_modes:
            raise ValueError(f"invalid_mode:{name}")

        prior = self._mode
        await self._dispatcher.set_mode_bindings(name)
        self._mode = name

        if prior != name:
            logger.info("mode %s -> %s", prior, name)
        else:
            logger.info("mode unchanged (%s)", name)

        return {
            "ok": True,
            "domain": "mode",
            "action": "set",
            "mode": self._mode,
            "trigger": trigger,
        }

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
            self._last_result = "busy"  # a skip, not an error
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
        self._note_trigger(trigger)
        self._mode = name
        self._acted = True
        self._remember()

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

            self._last_result = "busy"  # a skip, not an error
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
            self._acted = True
            self._note_trigger(trigger)

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
                self._flows.run(name=name, trigger=trigger, source=source, report=report),
                name=f"sequence:{name}",
            )
            self._active_sequence_task = seq_task

            target_mode = self._flows.target_mode(name)

            try:
                try:
                    ok = await asyncio.shield(seq_task)
                except asyncio.CancelledError:
                    logger.warning(
                        "sequence caller cancelled name=%s trigger=%s source=%s; waiting for sequence task to finish",
                        name,
                        trigger,
                        source,
                    )

                    current = asyncio.current_task()
                    if current is not None:
                        with contextlib.suppress(Exception):
                            current.uncancel()

                    ok = await asyncio.shield(seq_task)

                if not ok:
                    return self._flow_failed(report, name=name, trigger=trigger, source=source, error="flow_failed")

                if target_mode:
                    try:
                        await self._commit_mode_internal(target_mode, trigger=f"flow.{name}")
                    except Exception as exc:
                        return self._flow_failed(
                            report,
                            name=name,
                            trigger=trigger,
                            source=source,
                            error=f"mode_commit_failed:{exc}",
                            metadata={"phase": "mode_commit", "target_mode": target_mode},
                        )

                self._last_flow = {
                    "listen_signal": "listen",
                    "watch_signal": "watch",
                }.get(name, name)
                self._remember()

                self._set_runtime_ok("ok")
                report.finish(result="ok")

                if self._history is not None:
                    self._history.emit(
                        kind="flow_finished",
                        message=f"flow {name} completed",
                        flow_name=name,
                        trigger=trigger,
                        metadata={
                            "source": source,
                            "report_id": report.id,
                            "result": "ok",
                            "duration_ms": report.to_dict().get("duration_ms"),
                            "mode": self._mode,
                            "last_flow": self._last_flow,
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
                    "result": "ok",
                }

            except FlowStepFailures as exc:
                logger.warning(
                    "sequence finished with step failures name=%s trigger=%s source=%s error=%s",
                    name,
                    trigger,
                    source,
                    str(exc),
                )
                return self._flow_failed(
                    report,
                    name=name,
                    trigger=trigger,
                    source=source,
                    error=str(exc),
                    metadata={"phase": "step_failures", "failures": exc.failures},
                    extra={"failures": exc.failures},
                )

            except Exception as exc:
                logger.exception("sequence failed name=%s trigger=%s source=%s", name, trigger, source)
                return self._flow_failed(report, name=name, trigger=trigger, source=source, error=str(exc))

            finally:
                self._active_sequence_task = None
                self._flow_running = False

    def _flow_failed(
        self,
        report: FlowRunReport,
        *,
        name: str,
        trigger: str,
        source: str,
        error: str,
        metadata: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a failed flow (runtime status, report, history) and build its result."""
        self._set_runtime_error(error, result="failed")
        report.finish(result="failed", error=error)

        if self._history is not None:
            self._history.emit(
                kind="flow_failed",
                message=f"flow {name} failed",
                level="error",
                flow_name=name,
                trigger=trigger,
                metadata={"source": source, "report_id": report.id, "error": error, **(metadata or {})},
            )

        return {
            "ok": False,
            "domain": "flow",
            "action": "run",
            "name": name,
            "trigger": trigger,
            "source": source,
            "error": error,
            "report_id": report.id,
            **(extra or {}),
        }

    async def on_device_state_change(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        payload = payload or {}

        if not name:
            self._set_runtime_error("state_change_name_required", result="invalid")
            return {"ok": False, "error": "state change name required"}

        if self._lock.locked():
            logger.info("device-state %s ignored (flow running)", name)
            self._last_result = "busy"  # a skip, not an error
            return {
                "ok": False,
                "name": name,
                "source": "device_state_change",
                "reason": "runner_busy",
            }

        if name == "listen" and self._last_flow == "listen":
            logger.info("device-state listen ignored (already listen)")
            self._set_runtime_ok("ignored_already_listen")
            return {"ok": False, "name": name, "reason": "last_flow_listen"}

        if name == "watch" and self._last_flow == "watch":
            logger.info("device-state watch ignored (already watch)")
            self._set_runtime_ok("ignored_already_watch")
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
