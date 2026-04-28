"""Named PiHub sequences and the shared SequenceRunner."""

from __future__ import annotations

import asyncio
import logging

from dataclasses import dataclass, field
from typing import Any, Callable

from .history import FlowRunReport, FlowStepReport
from .settings import SettingsData

logger = logging.getLogger(__name__)

SPEAKER_WATCH_SOURCE = "hdmi"
LISTEN_SOURCES = {"wifi", "airplay", "multiroom-secondary"}
DISPATCH_SETTLE_TIMEOUT_S = 10.0
_FLOW_DEFAULTS = SettingsData()


class FlowDispatchError(RuntimeError):
    def __init__(self, *, sequence_name: str, failures: list[tuple[str, str]]) -> None:
        self.sequence_name = sequence_name
        self.failures = list(failures)
        detail = ", ".join(f"{step_id}: {error}" for step_id, error in self.failures)
        super().__init__(f"dispatch_failed: {detail}")

class FlowStepFailures(RuntimeError):
    def __init__(self, *, sequence_name: str, failures: list[dict[str, str]]) -> None:
        self.sequence_name = sequence_name
        self.failures = list(failures)
        detail = ", ".join(
            f"{item.get('step_id', '?')}: {item.get('error', 'failed')}"
            for item in self.failures
        )
        super().__init__(f"flow_failed: {detail}")


@dataclass(frozen=True)
class SequenceStep:
    id: str
    domain: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    when: str | None = None
    timeout_s: float | None = None
    mode: str = "dispatch"   # "dispatch" | "await"


@dataclass(frozen=True)
class SequenceDefinition:
    name: str
    target_mode: str | None
    steps: tuple[SequenceStep, ...]


class FlowWaitTimeout(RuntimeError):
    def __init__(self, *, kind: str, timeout_s: float) -> None:
        self.kind = kind
        self.timeout_s = timeout_s
        super().__init__(f"{kind}_timeout")


@dataclass(frozen=True)
class _DispatchRecord:
    step: SequenceStep
    report: FlowStepReport | None
    task: asyncio.Task[None]


class SequenceRunner:
    def __init__(
        self,
        *,
        runtime: Any,
        tv: Any = None,
        speaker: Any = None,
        ble: Any = None,
        settings: Any = None,
        speaker_backend: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._tv = tv
        self._speaker = speaker
        self._ble = ble
        self._settings = settings
        self._predicates: dict[str, Callable[[dict[str, Any]], bool]] = {
            "tv_is_on": lambda snap: bool(snap.get("tv_is_on")),
            "tv_is_off": lambda snap: bool(snap.get("tv_is_off")),

            "speaker_is_on_listen_source": lambda snap: (
                str(snap.get("speaker_source") or "") in LISTEN_SOURCES
            ),
            "speaker_is_in_multiroom": lambda snap: (
                str(snap.get("speaker_source") or "") in LISTEN_SOURCES
                and (
                    bool(snap.get("speaker_is_multiroom_guest"))
                    or bool(snap.get("speaker_is_multiroom_host"))
                )
            ),
            "speaker_should_stop": lambda snap: (
                str(snap.get("speaker_source") or "") in LISTEN_SOURCES
                and not bool(snap.get("speaker_is_multiroom_guest"))
            ),
        }

        self._speaker_backend = (speaker_backend or self._speaker_backend_name()).strip().lower()
        if self._speaker_backend == "samsung_soundbar":
            self._defs = self._build_samsung_soundbar_flows()
        else:
            self._defs = self._build_audiopro_flows()

    def _build_audiopro_flows(self) -> dict[str, SequenceDefinition]:
        return {
            "listen": SequenceDefinition(
                name="listen",
                target_mode="listen",
                steps=(
                    SequenceStep(
                        "apple_tv_return_home",
                        "ble",
                        "return_home",
                        when="tv_is_on",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_is_on",
                        timeout_s=8.0,
                        mode="await",
                    ),

                    SequenceStep(
                        "speaker_listen_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "listen_volume_pct"},
                    ),
                    SequenceStep(
                        "speaker_play_listen_target",
                        "speaker",
                        "play_listen_target",
                        {},
                        mode="await",
                    ),
                ),
            ),

            "watch": SequenceDefinition(
                name="watch",
                target_mode="watch",
                steps=(
                    *self._speaker_stop_steps(),
                    SequenceStep(
                        "apple_tv_power_on",
                        "ble",
                        "power_on",
                        when="tv_is_off",
                    ),
                    SequenceStep(
                        "tv_power_on",
                        "tv",
                        "power_on",
                        when="tv_is_off",
                        timeout_s=8.0,
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_watch_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "watch_volume_pct"},
                    ),
                ),
            ),

            "listen_signal": SequenceDefinition(
                name="listen_signal",
                target_mode="listen",
                steps=(
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_is_on",
                        timeout_s=8.0,
                    ),
                    SequenceStep(
                        "apple_tv_return_home",
                        "ble",
                        "return_home",
                        when="tv_is_on",
                        mode="await",
                    ),
                ),
            ),

            "watch_signal": SequenceDefinition(
                name="watch_signal",
                target_mode="watch",
                steps=(
                    *self._speaker_stop_steps(),

                    SequenceStep(
                        "tv_power_on",
                        "tv",
                        "power_on",
                        when="tv_is_off",
                        timeout_s=8.0,
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_watch_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "watch_volume_pct"},
                    ),
                ),
            ),

            "power_off": SequenceDefinition(
                name="power_off",
                target_mode="power_off",
                steps=(
                    SequenceStep(
                        "apple_tv_return_home",
                        "ble",
                        "return_home",
                        when="tv_is_on",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_is_on",
                        timeout_s=8.0,
                        mode="await",
                    ),


                    *self._speaker_stop_steps(),
                    *self._speaker_power_off_steps(),
                ),
            ),
        }

    def _build_samsung_soundbar_flows(self) -> dict[str, SequenceDefinition]:
        return {
            "listen": SequenceDefinition(
                name="listen",
                target_mode="listen",
                steps=(
                    SequenceStep(
                        "apple_tv_power_off",
                        "ble",
                        "power_off",
                        when="tv_is_on",
                    ),
                    SequenceStep(
                        "speaker_listen_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "listen_volume_pct"},
                    ),
                ),
            ),

            "watch": SequenceDefinition(
                name="watch",
                target_mode="watch",
                steps=(
                    SequenceStep(
                        "apple_tv_power_on",
                        "ble",
                        "power_on",
                        when="tv_is_off",
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_watch_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "watch_volume_pct"},
                    ),
                ),
            ),

            "listen_signal": SequenceDefinition(
                name="listen_signal",
                target_mode="listen",
                steps=(
                    SequenceStep(
                        "apple_tv_power_off",
                        "ble",
                        "power_off",
                        when="tv_is_on",
                        mode="await",
                    ),
                ),
            ),

            "watch_signal": SequenceDefinition(
                name="watch_signal",
                target_mode="watch",
                steps=(
                    SequenceStep(
                        "speaker_watch_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "watch_volume_pct"},
                        mode="await",
                    ),
                ),
            ),

            "power_off": SequenceDefinition(
                name="power_off",
                target_mode="power_off",
                steps=(
                    SequenceStep(
                        "apple_tv_power_off",
                        "ble",
                        "power_off",
                        when="tv_is_on",
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_stop",
                        "speaker",
                        "stop_playback",
                        when="speaker_is_on_listen_source",
                        mode="await",
                    ),
                ),
            ),
        }

    def _speaker_stop_steps(self) -> tuple[SequenceStep, ...]:
        return (
            SequenceStep(
                "speaker_stop",
                "speaker",
                "stop_playback",
                when="speaker_should_stop",
                mode="await",
            ),
            SequenceStep(
                "speaker_leave_group",
                "speaker",
                "leave_native_multiroom_if_needed",
                when="speaker_is_in_multiroom",
                mode="await",
            ),
            SequenceStep(
                "speaker_settle_after_group",
                "system",
                "sleep",
                {"seconds": 1.0},
                when="speaker_is_in_multiroom",
                mode="await",
            ),
        )

    def _speaker_power_off_steps(self) -> tuple[SequenceStep, ...]:
        return (
            SequenceStep(
                "speaker_power_off",
                "speaker",
                "power_off",
                when="speaker_is_on_listen_source",
                mode="await",
            ),
        )

    def target_mode(self, name: str) -> str | None:
        seq = self._defs.get((name or "").strip())
        if seq is None:
            return None
        return seq.target_mode

    async def run(
        self,
        *,
        name: str,
        trigger: str,
        args: dict[str, Any] | None = None,
        source: str = "intent",
        report: FlowRunReport | None = None,
    ) -> bool:
        del args
        seq = self._defs.get((name or "").strip())
        if seq is None:
            logger.warning("unknown flow name=%s trigger=%s source=%s", name, trigger, source)
            return False

        snapshot = self._build_snapshot()
        logger.debug(
            "sequence started name=%s trigger=%s source=%s snapshot=%s",
            seq.name,
            trigger,
            source,
            self._format_snapshot(snapshot),
        )

        dispatch_records: list[_DispatchRecord] = []
        failures: list[dict[str, str]] = []

        for index, step in enumerate(seq.steps, start=1):
            step_report: FlowStepReport | None = None
            if report is not None:
                step_report = report.add_step(
                    step_id=step.id,
                    domain=step.domain,
                    action=step.action,
                    mode=step.mode,
                )

            if step.when and not self._predicate(step.when, snapshot):
                logger.debug(
                    "sequence step skipped sequence=%s step=%s index=%d reason=when_false when=%s",
                    seq.name,
                    step.id,
                    index,
                    step.when,
                )
                if step_report is not None:
                    step_report.finish(status="skipped", reason=f"when_false:{step.when}")
                continue

            logger.debug(
                "sequence step start sequence=%s step=%s index=%d domain=%s action=%s mode=%s",
                seq.name,
                step.id,
                index,
                step.domain,
                step.action,
                step.mode,
            )
            try:
                await self._run_step(
                    sequence_name=seq.name,
                    step=step,
                    report=report,
                    step_report=step_report,
                    dispatch_records=dispatch_records,
                )
            except Exception as exc:
                if self._is_skippable_samsung_speaker_gap(step=step, exc=exc):
                    if step_report is not None and step_report.ts_finished is None:
                        step_report.finish(
                            status="skipped",
                            reason="unsupported_on_samsung_soundbar",
                        )

                    logger.debug(
                        "sequence step skipped due to samsung backend limitation sequence=%s step=%s index=%d domain=%s action=%s error=%s",
                        seq.name,
                        step.id,
                        index,
                        step.domain,
                        step.action,
                        str(exc),
                    )
                    continue

                if step_report is not None and step_report.ts_finished is None:
                    step_report.finish(status="failed", error=str(exc))

                failures.append(
                    {
                        "step_id": step.id,
                        "domain": step.domain,
                        "action": step.action,
                        "phase": "step_run",
                        "error": str(exc),
                    }
                )

                logger.warning(
                    "sequence step failed sequence=%s step=%s index=%d domain=%s action=%s error=%s; continuing",
                    seq.name,
                    step.id,
                    index,
                    step.domain,
                    step.action,
                    str(exc),
                )
                continue

            if step_report is not None and step_report.ts_finished is None:
                step_report.finish(status="ok")

            logger.debug(
                "sequence step ok sequence=%s step=%s index=%d",
                seq.name,
                step.id,
                index,
            )

        failures.extend(
            await self._await_dispatch_records(
                sequence_name=seq.name,
                dispatch_records=dispatch_records,
            )
        )

        if failures:
            raise FlowStepFailures(sequence_name=seq.name, failures=failures)

        logger.debug("sequence completed name=%s trigger=%s source=%s", seq.name, trigger, source)
        return True

    def _build_snapshot(self) -> dict[str, Any]:
        speaker_snapshot = {}
        if self._speaker is not None:
            try:
                speaker_snapshot = self._speaker.snapshot() or {}
            except Exception:
                speaker_snapshot = {}

        speaker_source = str(speaker_snapshot.get("source") or "").strip().lower()
        tv_is_on = self._tv_is_on()

        return {
            "current_mode": self._runtime.mode,
            "tv_is_on": tv_is_on,
            "tv_is_off": not tv_is_on,
            "speaker_source": speaker_source,
            "speaker_is_multiroom_guest": bool(
                speaker_snapshot.get("multiroom_guest_active", False)
            ),
            "speaker_is_multiroom_host": bool(
                speaker_snapshot.get("multiroom_host_active", False)
            ),
        }

    def _predicate(self, name: str, snapshot: dict[str, Any]) -> bool:
        fn = self._predicates.get(name)
        if fn is None:
            raise KeyError(name)
        return bool(fn(snapshot))

    def _track_dispatch_task(
        self,
        *,
        sequence_name: str,
        step: SequenceStep,
        report: FlowRunReport | None,
        step_report: FlowStepReport | None,
        task: asyncio.Task[None],
    ) -> None:
        def _done_callback(done_task: asyncio.Task[None]) -> None:
            if report is None or step_report is None:
                return

            try:
                done_task.result()
            except asyncio.CancelledError:
                logger.warning(
                    "dispatch step cancelled sequence=%s step=%s domain=%s action=%s",
                    sequence_name,
                    step.id,
                    step.domain,
                    step.action,
                )
                self._runtime.note_dispatch_outcome(
                    report=report,
                    step_report=step_report,
                    sequence_name=sequence_name,
                    step=step,
                    error="cancelled",
                )
            except Exception as exc:
                if self._is_skippable_samsung_speaker_gap(step=step, exc=exc):
                    logger.debug(
                        "dispatch step callback skipped due to samsung backend limitation sequence=%s step=%s domain=%s action=%s error=%s",
                        sequence_name,
                        step.id,
                        step.domain,
                        step.action,
                        str(exc),
                    )
                    return

                logger.warning(
                    "dispatch step failed sequence=%s step=%s domain=%s action=%s error=%s",
                    sequence_name,
                    step.id,
                    step.domain,
                    step.action,
                    str(exc),
                )
                self._runtime.note_dispatch_outcome(
                    report=report,
                    step_report=step_report,
                    sequence_name=sequence_name,
                    step=step,
                    error=str(exc),
                )
            else:
                self._runtime.note_dispatch_outcome(
                    report=report,
                    step_report=step_report,
                    sequence_name=sequence_name,
                    step=step,
                    error=None,
                )

        task.add_done_callback(_done_callback)

    async def _await_dispatch_records(
        self,
        *,
        sequence_name: str,
        dispatch_records: list[_DispatchRecord],
    ) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []

        for record in dispatch_records:
            try:
                if record.step.timeout_s is None:
                    await asyncio.wait_for(
                        record.task,
                        timeout=DISPATCH_SETTLE_TIMEOUT_S,
                    )
                else:
                    await record.task

            except asyncio.TimeoutError:
                record.task.cancel()
                try:
                    await record.task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                failures.append(
                    {
                        "step_id": record.step.id,
                        "domain": record.step.domain,
                        "action": record.step.action,
                        "phase": "dispatch_settle",
                        "error": f"dispatch_settle_timeout:{DISPATCH_SETTLE_TIMEOUT_S:g}s",
                    }
                )
            except asyncio.CancelledError:
                failures.append(
                    {
                        "step_id": record.step.id,
                        "domain": record.step.domain,
                        "action": record.step.action,
                        "phase": "dispatch_settle",
                        "error": "cancelled",
                    }
                )
            except Exception as exc:
                if self._is_skippable_samsung_speaker_gap(step=record.step, exc=exc):
                    if record.report is not None:
                        record.report.status = "skipped"
                        record.report.reason = "smartthings_api_limitation"
                        record.report.error = None

                        record.report.outcome_status = None
                        record.report.outcome_reason = None
                        record.report.outcome_error = None
                        record.report.ts_outcome = None

                    logger.debug(
                        "dispatch step skipped due to samsung backend limitation sequence=%s step=%s domain=%s action=%s error=%s",
                        sequence_name,
                        record.step.id,
                        record.step.domain,
                        record.step.action,
                        str(exc),
                    )
                    continue

                failures.append(
                    {
                        "step_id": record.step.id,
                        "domain": record.step.domain,
                        "action": record.step.action,
                        "phase": "dispatch_settle",
                        "error": str(exc),
                    }
                )

        return failures

    async def _run_step(
        self,
        *,
        sequence_name: str,
        step: SequenceStep,
        report: FlowRunReport | None = None,
        step_report: FlowStepReport | None = None,
        dispatch_records: list[_DispatchRecord],
    ) -> None:
        async def _invoke() -> None:
            if step.timeout_s is None:
                await self._dispatch_step(sequence_name=sequence_name, step=step)
                return

            await asyncio.wait_for(
                self._dispatch_step(sequence_name=sequence_name, step=step),
                timeout=step.timeout_s,
            )

        try:
            if step.mode == "dispatch":
                dispatch_task = asyncio.create_task(
                    _invoke(),
                    name=f"dispatch:{sequence_name}:{step.id}",
                )
                if step_report is not None and step_report.ts_finished is None:
                    step_report.mark_dispatched()
                self._track_dispatch_task(
                    sequence_name=sequence_name,
                    step=step,
                    report=report,
                    step_report=step_report,
                    task=dispatch_task,
                )
                dispatch_records.append(
                    _DispatchRecord(step=step, report=step_report, task=dispatch_task)
                )
                return

            await _invoke()

        except asyncio.TimeoutError:
            logger.warning(
                "sequence step timeout sequence=%s step=%s domain=%s action=%s timeout_s=%s",
                sequence_name,
                step.id,
                step.domain,
                step.action,
                step.timeout_s,
            )
            raise
        except asyncio.CancelledError:
            logger.warning(
                "sequence step cancelled sequence=%s step=%s domain=%s action=%s",
                sequence_name,
                step.id,
                step.domain,
                step.action,
            )
            raise
        except Exception:
            logger.exception(
                "sequence step failed sequence=%s step=%s domain=%s action=%s",
                sequence_name,
                step.id,
                step.domain,
                step.action,
            )
            raise

    def _speaker_backend_name(self) -> str:
        if self._speaker is None:
            return ""
        try:
            snap = self._speaker.snapshot() or {}
            return str(snap.get("backend") or "").strip().lower()
        except Exception:
            return ""

    def _is_skippable_samsung_speaker_gap(self, *, step: SequenceStep, exc: Exception) -> bool:
        if self._speaker_backend_name() not in {"samsung_soundbar"}:
            return False

        text = str(exc or "").strip()
        if not text.startswith("unsupported_on_backend:"):
            return False

        allowed = {
            ("speaker", "set_source"),
            ("speaker", "play_listen_target"),
            ("speaker", "preset"),
            ("speaker", "leave_native_multiroom_if_needed"),
        }
        return (step.domain, step.action) in allowed

    def _require_speaker_ready(self, *, step: SequenceStep) -> None:
        if self._speaker is None:
            raise RuntimeError(f"{step.id}: speaker_unavailable")

        state = getattr(self._speaker, "state", None)
        if state is None:
            raise RuntimeError(f"{step.id}: speaker_state_unavailable")

        reachable = bool(getattr(state, "reachable", False))
        connected = bool(getattr(state, "connected", False))
        ready = bool(getattr(state, "ready", False))
        last_error = str(getattr(state, "last_error", "") or "").strip()

        if ready:
            return

        if not reachable:
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"{step.id}: speaker_not_reachable{suffix}")
        if not connected:
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"{step.id}: speaker_not_connected{suffix}")

        suffix = f": {last_error}" if last_error else ""
        raise RuntimeError(f"{step.id}: speaker_not_ready{suffix}")

    def _require_ble_ready(self, *, step: SequenceStep) -> None:
        if self._ble is None:
            raise RuntimeError(f"{step.id}: ble_unavailable")

        status = getattr(self._ble, "status", {}) or {}
        transport_open = bool(status.get("transport_open"))
        connected = bool(status.get("connected"))
        ready = bool(status.get("ready"))
        last_error = str(status.get("last_error") or "").strip()

        if ready:
            return

        if not transport_open:
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"{step.id}: ble_transport_down{suffix}")
        if not connected:
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"{step.id}: ble_not_connected{suffix}")

        suffix = f": {last_error}" if last_error else ""
        raise RuntimeError(f"{step.id}: ble_not_ready{suffix}")
    
    def _require_tv_flow_ready(self, *, step: SequenceStep) -> None:
        if self._tv is None:
            raise RuntimeError(f"{step.id}: tv_unavailable")

        snap = self._tv.snapshot()
        if not bool(snap.token_present):
            raise RuntimeError(f"{step.id}: tv_token_missing")

    async def _dispatch_step(self, *, sequence_name: str, step: SequenceStep) -> None:
        del sequence_name
        args = step.args or {}

        if step.domain == "speaker" and step.action == "preset":
            self._require_speaker_ready(step=step)
            await self._speaker.preset(int(args["preset"]))
            return

        if step.domain == "speaker" and step.action == "set_volume":
            self._require_speaker_ready(step=step)
            if "setting" in args:
                setting_name = str(args["setting"])
                if setting_name == "watch_volume_pct":
                    volume = self._watch_volume_pct()
                elif setting_name == "listen_volume_pct":
                    volume = self._listen_volume_pct()
                else:
                    raise ValueError(f"unknown volume setting: {setting_name}")
            else:
                volume = int(args["volume"])
            await self._speaker.set_volume(int(volume))
            return

        if step.domain == "speaker" and step.action == "play_listen_target":
            self._require_speaker_ready(step=step)
            target = self._listen_target()
            if target["type"] == "preset":
                await self._speaker.preset(int(target["preset"]))
            elif target["type"] == "stream":
                if self._settings is None:
                    raise ValueError("stream listen target configured but settings unavailable")
                url = self._settings.get_stream_url(int(target["stream"]))
                if not url:
                    raise ValueError(f"listen target stream_url_{int(target['stream'])} is empty")
                await self._speaker.play_url(url)
            else:
                raise ValueError(f"unsupported listen target type: {target['type']}")
            return

        if step.domain == "speaker" and step.action == "stop_playback":
            self._require_speaker_ready(step=step)
            await self._speaker.stop_playback()
            return

        if step.domain == "speaker" and step.action == "power_off":
            self._require_speaker_ready(step=step)
            await self._speaker.power_off()
            return

        if step.domain == "speaker" and step.action == "set_source":
            self._require_speaker_ready(step=step)
            await self._speaker.set_source(str(args["source"]))
            return

        if step.domain == "speaker" and step.action == "leave_native_multiroom_if_needed":
            self._require_speaker_ready(step=step)
            await self._speaker.leave_native_multiroom_if_needed(
                ["192.168.70.43", "192.168.70.45", "192.168.70.46"]
            )
            return

        if step.domain == "ble" and step.action == "return_home":
            self._require_ble_ready(step=step)
            await self._ble.return_home()
            return

        if step.domain == "ble" and step.action == "power_on":
            self._require_ble_ready(step=step)
            await self._ble.power_on()
            return

        if step.domain == "ble" and step.action == "power_off":
            self._require_ble_ready(step=step)
            await self._ble.power_off()
            return

        if step.domain == "tv" and step.action == "power_on":
            self._require_tv_flow_ready(step=step)
            ok = await self._tv.power_on()
            if not ok:
                raise RuntimeError(f"{step.id}: tv_power_on_failed")
            return

        if step.domain == "tv" and step.action == "power_off":
            self._require_tv_flow_ready(step=step)
            ok = await self._tv.power_off()
            if not ok:
                raise RuntimeError(f"{step.id}: tv_power_off_failed")
            return

        if step.domain == "system" and step.action == "sleep":
            await asyncio.sleep(float(args["seconds"]))
            return

        if step.domain == "wait" and step.action == "tv_off":
            await self._wait_for_tv_off(timeout_s=float(args["timeout_s"]))
            return

        if step.domain == "wait" and step.action == "tv_on":
            await self._wait_for_tv_on(timeout_s=float(args["timeout_s"]))
            return

        raise ValueError(f"unsupported step domain/action: {step.domain}.{step.action}")

    @staticmethod
    def _format_snapshot(snapshot: dict[str, Any]) -> str:
        keys = [
            "current_mode",
            "tv_is_on",
            "tv_is_off",
            "speaker_source",
            "speaker_is_multiroom_guest",
            "speaker_is_multiroom_host",
        ]
        parts = [f"{k}={snapshot.get(k)!r}" for k in keys if k in snapshot]
        return " ".join(parts)

    def _tv_is_on(self) -> bool:
        if self._tv is None:
            return False
        try:
            snap = self._tv.snapshot()
            return snap.presence_on is True
        except Exception:
            return False

    def _speaker_source(self) -> str:
        if self._speaker is None:
            return ""
        try:
            snap = self._speaker.snapshot()
            return str((snap.get("source") or "")).strip().lower()
        except Exception:
            return ""

    def _watch_volume_pct(self) -> int:
        if self._settings is None:
            return int(_FLOW_DEFAULTS.watch_volume_pct)
        try:
            return int(self._settings.get_watch_volume_pct())
        except Exception:
            return int(_FLOW_DEFAULTS.watch_volume_pct)


    def _listen_volume_pct(self) -> int:
        if self._settings is None:
            return int(_FLOW_DEFAULTS.listen_volume_pct)
        try:
            return int(self._settings.get_listen_volume_pct())
        except Exception:
            return int(_FLOW_DEFAULTS.listen_volume_pct)


    def _listen_target(self) -> dict[str, int | str]:
        if self._settings is None:
            return {
                "type": str(_FLOW_DEFAULTS.listen_target_type),
                "preset": int(_FLOW_DEFAULTS.listen_target_preset),
                "stream": int(_FLOW_DEFAULTS.listen_target_stream),
            }
        try:
            target = self._settings.get_listen_target()
            return {
                "type": str(target.get("type") or _FLOW_DEFAULTS.listen_target_type),
                "preset": int(target.get("preset") or _FLOW_DEFAULTS.listen_target_preset),
                "stream": int(target.get("stream") or _FLOW_DEFAULTS.listen_target_stream),
            }
        except Exception:
            return {
                "type": str(_FLOW_DEFAULTS.listen_target_type),
                "preset": int(_FLOW_DEFAULTS.listen_target_preset),
                "stream": int(_FLOW_DEFAULTS.listen_target_stream),
            }

    async def _wait_for_tv_on(self, *, timeout_s: float) -> None:
        if self._tv is None:
            logger.debug(
                "skipping wait_for_tv_on because tv domain is unavailable timeout_s=%s",
                timeout_s,
            )
            return

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                if self._tv.snapshot().presence_on is True:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)

        raise FlowWaitTimeout(kind="tv_on", timeout_s=timeout_s)

    async def _wait_for_tv_off(self, *, timeout_s: float) -> None:
        if self._tv is None:
            logger.debug(
                "skipping wait_for_tv_off because tv domain is unavailable timeout_s=%s",
                timeout_s,
            )
            return

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                if self._tv.snapshot().presence_on is False:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)

        raise FlowWaitTimeout(kind="tv_off", timeout_s=timeout_s)


class FlowRunner:
    def __init__(
        self,
        *,
        runtime: Any,
        tv: Any = None,
        speaker: Any = None,
        ble: Any = None,
        settings: Any = None,
        speaker_backend: str | None = None,
    ) -> None:
        self._sequences = SequenceRunner(
            runtime=runtime,
            tv=tv,
            speaker=speaker,
            ble=ble,
            settings=settings,
            speaker_backend=speaker_backend,
        )

    def target_mode(self, name: str) -> str | None:
        return self._sequences.target_mode(name)

    async def run(
        self,
        *,
        name: str,
        trigger: str,
        args: dict[str, Any] | None = None,
        source: str = "intent",
        report: FlowRunReport | None = None,
    ) -> bool:
        return await self._sequences.run(
            name=name,
            trigger=trigger,
            args=args,
            source=source,
            report=report,
        )
