"""Named PiHub sequences and the shared SequenceRunner."""

from __future__ import annotations

import asyncio
import logging

from dataclasses import dataclass, field
from typing import Any, Callable

from .history import FlowRunReport, FlowStepReport

logger = logging.getLogger(__name__)

SPEAKER_WATCH_SOURCE = "hdmi"
LISTEN_SOURCES = {"wifi", "airplay", "multiroom-secondary"}
DISPATCH_SETTLE_TIMEOUT_S = 15.0


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
    ) -> None:
        self._runtime = runtime
        self._tv = tv
        self._speaker = speaker
        self._ble = ble
        self._settings = settings
        self._predicates: dict[str, Callable[[dict[str, Any]], bool]] = {
            "tv_was_on": lambda snap: bool(snap.get("tv_was_on")),
            "tv_was_off": lambda snap: bool(snap.get("tv_was_off")),
            "speaker_source_listen": lambda snap: str(snap.get("speaker_source") or "") in LISTEN_SOURCES,
        }
        self._defs: dict[str, SequenceDefinition] = {
            "listen": SequenceDefinition(
                name="listen",
                target_mode="listen",
                steps=(
                    SequenceStep(
                        "ble_return_home",
                        "ble",
                        "return_home",
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "wait_2s",
                        "system",
                        "sleep",
                        {"seconds": 2.0},
                        when="tv_was_on",
                        mode="await",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "wait_1s",
                        "system",
                        "sleep",
                        {"seconds": 1.0},
                        when="tv_was_on",
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_set_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "listen_volume_pct"},
                    ),
                    SequenceStep(
                        "speaker_set_preset",
                        "speaker",
                        "play_listen_target",
                        {},
                    ),
                    SequenceStep(
                        "wait_tv_off",
                        "wait",
                        "tv_off",
                        {"timeout_s": 20.0},
                        when="tv_was_on",
                        mode="await",
                    ),
                ),
            ),
            "watch": SequenceDefinition(
                name="watch",
                target_mode="watch",
                steps=(
                    SequenceStep(
                        "speaker_stop",
                        "speaker",
                        "stop_playback",
                        when="speaker_source_listen",
                    ),
                    SequenceStep(
                        "tv_power_on",
                        "tv",
                        "power_on",
                        when="tv_was_off",
                    ),
                    SequenceStep(
                        "wait_2s",
                        "system",
                        "sleep",
                        {"seconds": 2.0},
                        when="tv_was_off",
                        mode="await",
                    ),
                    SequenceStep(
                        "ble_power_on",
                        "ble",
                        "power_on",
                        when="tv_was_off",
                    ),
                    SequenceStep(
                        "wait_1s",
                        "system",
                        "sleep",
                        {"seconds": 1.0},
                        when="tv_was_off",
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_set_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "watch_volume_pct"},
                    ),
                    SequenceStep(
                        "wait_0.5s",
                        "system",
                        "sleep",
                        {"seconds": 0.5},
                        when="tv_was_off",
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_set_hdmi",
                        "speaker",
                        "set_source",
                        {"source": SPEAKER_WATCH_SOURCE},
                    ),
                    SequenceStep(
                        "wait_tv_on",
                        "wait",
                        "tv_on",
                        {"timeout_s": 20.0},
                        when="tv_was_off",
                        mode="await",
                    ),
                ),
            ),
            "listen_signal": SequenceDefinition(
                name="listen_signal",
                target_mode="listen",
                steps=(
                    SequenceStep(
                        "ble_return_home",
                        "ble",
                        "return_home",
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "wait_2s",
                        "system",
                        "sleep",
                        {"seconds": 2.0},
                        when="tv_was_on",
                        mode="await",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "wait_tv_off",
                        "wait",
                        "tv_off",
                        {"timeout_s": 20.0},
                        when="tv_was_on",
                        mode="await",
                    ),
                ),
            ),
            "watch_signal": SequenceDefinition(
                name="watch_signal",
                target_mode="watch",
                steps=(
                    SequenceStep(
                        "wait_2s",
                        "system",
                        "sleep",
                        {"seconds": 2.0},
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_set_volume",
                        "speaker",
                        "set_volume",
                        {"setting": "watch_volume_pct"},
                    ),
                    SequenceStep(
                        "wait_0.5s",
                        "system",
                        "sleep",
                        {"seconds": 0.5},
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_set_hdmi",
                        "speaker",
                        "set_source",
                        {"source": SPEAKER_WATCH_SOURCE},
                    ),
                    SequenceStep(
                        "wait_tv_on",
                        "wait",
                        "tv_on",
                        {"timeout_s": 20.0},
                        mode="await",
                    ),
                ),
            ),
            "power_off": SequenceDefinition(
                name="power_off",
                target_mode="power_off",
                steps=(
                    SequenceStep(
                        "ble_return_home",
                        "ble",
                        "return_home",
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "wait_2.5s",
                        "system",
                        "sleep",
                        {"seconds": 2.5},
                        when="tv_was_on",
                        mode="await",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "wait_tv_off",
                        "wait",
                        "tv_off",
                        {"timeout_s": 20},
                        when="tv_was_on",
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_stop",
                        "speaker",
                        "stop_playback",
                        when="speaker_source_listen",
                    ),
                    SequenceStep(
                        "wait_0.5s",
                        "system",
                        "sleep",
                        {"seconds": 0.5},
                        when="speaker_source_listen",
                        mode="await",
                    ),
                    SequenceStep(
                        "speaker_power_off",
                        "speaker",
                        "power_off",
                        when="speaker_source_listen",
                    ),
                ),
            ),
        }

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
        speaker_source = self._speaker_source()
        tv_was_on = self._tv_is_on()
        return {
            "current_mode": self._runtime.mode,
            "tv_was_on": tv_was_on,
            "tv_was_off": not tv_was_on,
            "speaker_source": speaker_source,
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
            settle_timeout_s = float(record.step.timeout_s or DISPATCH_SETTLE_TIMEOUT_S)

            try:
                await asyncio.wait_for(record.task, timeout=settle_timeout_s)
            except asyncio.TimeoutError:
                record.task.cancel()
                try:
                    await record.task
                except Exception:
                    pass
                failures.append(
                    {
                        "step_id": record.step.id,
                        "domain": record.step.domain,
                        "action": record.step.action,
                        "phase": "dispatch_settle",
                        "error": f"dispatch_settle_timeout:{settle_timeout_s:g}s",
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
            await self._dispatch_step(sequence_name=sequence_name, step=step)

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

            if step.timeout_s is None:
                await _invoke()
                return

            await asyncio.wait_for(_invoke(), timeout=step.timeout_s)

        except asyncio.TimeoutError:
            logger.exception(
                "sequence step timeout sequence=%s step=%s domain=%s action=%s timeout_s=%s",
                sequence_name,
                step.id,
                step.domain,
                step.action,
                step.timeout_s,
            )
            raise
        except asyncio.CancelledError:
            logger.exception(
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

        if step.domain == "ble" and step.action == "return_home":
            self._require_ble_ready(step=step)
            await self._ble.return_home()
            return

        if step.domain == "ble" and step.action == "power_on":
            self._require_ble_ready(step=step)
            await self._ble.power_on()
            return

        if step.domain == "tv" and step.action == "power_on":
            if self._tv is not None:
                await self._tv.power_on()
            return

        if step.domain == "tv" and step.action == "power_off":
            if self._tv is not None:
                await self._tv.power_off()
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
            "tv_was_on",
            "tv_was_off",
            "speaker_source",
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
            return WATCH_VOLUME_PCT
        try:
            return int(self._settings.get_watch_volume_pct())
        except Exception:
            return WATCH_VOLUME_PCT

    def _listen_volume_pct(self) -> int:
        if self._settings is None:
            return LISTEN_VOLUME_PCT
        try:
            return int(self._settings.get_listen_volume_pct())
        except Exception:
            return LISTEN_VOLUME_PCT

    def _listen_target(self) -> dict[str, Any]:
        if self._settings is None:
            return {"type": "preset", "preset": LISTEN_PRESET, "stream": 1}
        try:
            target = self._settings.get_listen_target()
            return {
                "type": str(target.get("type") or "preset"),
                "preset": int(target.get("preset") or LISTEN_PRESET),
                "stream": int(target.get("stream") or 1),
            }
        except Exception:
            return {"type": "preset", "preset": LISTEN_PRESET, "stream": 1}

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
    ) -> None:
        self._sequences = SequenceRunner(
            runtime=runtime,
            tv=tv,
            speaker=speaker,
            ble=ble,
            settings=settings,
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
