"""Named PiHub sequences and the shared SequenceRunner."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

WATCH_VOLUME_PCT = 30
LISTEN_VOLUME_PCT = 22
LISTEN_PRESET = 1
SPEAKER_WATCH_SOURCE = "hdmi"


@dataclass(frozen=True)
class SequenceStep:
    id: str
    domain: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    when: str | None = None
    timeout_s: float | None = None
    mode: str = "await"   # "await" | "dispatch"


@dataclass(frozen=True)
class SequenceDefinition:
    name: str
    steps: tuple[SequenceStep, ...]


class SequenceRunner:
    def __init__(
        self,
        *,
        runtime: Any,
        tv: Any = None,
        speaker: Any = None,
        ble: Any = None,
    ) -> None:
        self._runtime = runtime
        self._tv = tv
        self._speaker = speaker
        self._ble = ble
        self._predicates: dict[str, Callable[[dict[str, Any]], bool]] = {
            "tv_was_on": lambda snap: bool(snap.get("tv_was_on")),
            "tv_was_off": lambda snap: bool(snap.get("tv_was_off")),
            "speaker_source_wifi": lambda snap: str(snap.get("speaker_source") or "") == "wifi",
        }
        self._defs: dict[str, SequenceDefinition] = {
            "listen": SequenceDefinition(
                name="listen",
                steps=(
                    SequenceStep("set_mode", "mode", "set", {"name": "listen"}),

                    # If tv was on
                    SequenceStep(
                        "ble_return_home",
                        "ble",
                        "return_home",
                        when="tv_was_on",
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "wait_1",
                        "system",
                        "sleep",
                        {"seconds": 2.5},
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_was_on",
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "wait_2",
                        "system",
                        "sleep",
                        {"seconds": 1.0},
                        when="tv_was_on",
                    ),

                    # If tv not on
                    SequenceStep(
                        "speaker_preset",
                        "speaker",
                        "preset",
                        {"preset": LISTEN_PRESET},
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "speaker_volume",
                        "speaker",
                        "set_volume",
                        {"volume": LISTEN_VOLUME_PCT},
                        mode="dispatch",
                    ),

                    # Only if tv was on
                    SequenceStep(
                        "wait_tv_off",
                        "wait",
                        "tv_off",
                        {"timeout_s": 20.0},
                        when="tv_was_on",
                    ),
                ),
            ),
            "watch": SequenceDefinition(
                name="watch",
                steps=(
                    SequenceStep("set_mode", "mode", "set", {"name": "watch"}),

                    SequenceStep(
                        "tv_power_on",
                        "tv",
                        "power_on",
                        when="tv_was_off",
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "wait_1",
                        "system",
                        "sleep",
                        {"seconds": 2.0},
                        when="tv_was_off",
                    ),
                    SequenceStep(
                        "ble_power_on",
                        "ble",
                        "power_on",
                        when="tv_was_off",
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "wait_2",
                        "wait",
                        "tv_on",
                        {"timeout_s": 2.0},
                        when="tv_was_off",
                    ),
                    SequenceStep(
                        "speaker_set_hdmi",
                        "speaker",
                        "set_source",
                        {"source": SPEAKER_WATCH_SOURCE},
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "speaker_volume",
                        "speaker",
                        "set_volume",
                        {"volume": WATCH_VOLUME_PCT},
                        mode="dispatch",
                    ),
                ),
            ),
            "power_off": SequenceDefinition(
                name="power_off",
                steps=(
                    SequenceStep("set_mode", "mode", "set", {"name": "power_off"}),

                    # If TV was On
                    SequenceStep(
                        "ble_return_home",
                        "ble",
                        "return_home",
                        when="tv_was_on",
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "wait_1",
                        "system",
                        "sleep",
                        {"seconds": 2.0},
                        when="tv_was_on",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_was_on",
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "wait_tv_off",
                        "wait",
                        "tv_off",
                        {"timeout_s": 20},
                        when="tv_was_on",
                    ),

                    # If speaker on wifi
                    SequenceStep(
                        "speaker_stop",
                        "speaker",
                        "stop_playback",
                        when="speaker_source_wifi",
                        mode="dispatch",
                    ),
                    SequenceStep(
                        "wait_1",
                        "system",
                        "sleep",
                        {"seconds": 0.5},
                        when="speaker_source_wifi",
                    ),
                    SequenceStep(
                        "speaker_power_off",
                        "speaker",
                        "power_off",
                        when="speaker_source_wifi",
                        mode="dispatch",
                    ),
                ),
            ),
        }

    async def run(
        self,
        *,
        name: str,
        trigger: str,
        args: dict[str, Any] | None = None,
        source: str = "intent",
    ) -> bool:
        seq = self._defs.get((name or "").strip())
        if seq is None:
            logger.warning("unknown flow name=%s trigger=%s source=%s", name, trigger, source)
            return False

        snapshot = self._build_snapshot()
        logger.info(
            "sequence started name=%s trigger=%s source=%s snapshot=%s",
            seq.name,
            trigger,
            source,
            self._format_snapshot(snapshot),
        )

        for index, step in enumerate(seq.steps, start=1):
            if step.when and not self._predicate(step.when, snapshot):
                logger.info(
                    "sequence step skipped sequence=%s step=%s index=%d reason=when_false when=%s",
                    seq.name,
                    step.id,
                    index,
                    step.when,
                )
                continue
            logger.info(
                "sequence step start sequence=%s step=%s index=%d domain=%s action=%s",
                seq.name,
                step.id,
                index,
                step.domain,
                step.action,
            )
            try:
                await self._run_step(sequence_name=seq.name, step=step)
            except Exception:
                logger.exception(
                    "sequence step failed sequence=%s step=%s index=%d domain=%s action=%s",
                    seq.name,
                    step.id,
                    index,
                    step.domain,
                    step.action,
                )
                raise
            logger.info(
                "sequence step ok sequence=%s step=%s index=%d",
                seq.name,
                step.id,
                index,
            )

        logger.info("sequence completed name=%s trigger=%s source=%s", seq.name, trigger, source)
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

    async def _run_step(self, *, sequence_name: str, step: SequenceStep) -> None:
        async def _invoke() -> None:
            await self._dispatch_step(sequence_name=sequence_name, step=step)

        try:
            if step.mode == "dispatch":
                asyncio.create_task(_invoke())
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

    async def _dispatch_step(self, *, sequence_name: str, step: SequenceStep) -> None:
        args = step.args or {}

        if step.domain == "mode" and step.action == "set":
            await self._runtime.set_mode(str(args["name"]), trigger=f"flow.{sequence_name}")
            return

        if step.domain == "speaker" and step.action == "preset":
            if self._speaker is not None:
                await self._speaker.preset(int(args["preset"]))
            return

        if step.domain == "speaker" and step.action == "set_volume":
            if self._speaker is not None:
                await self._speaker.set_volume(int(args["volume"]))
            return

        if step.domain == "speaker" and step.action == "stop_playback":
            if self._speaker is not None:
                await self._speaker.stop_playback()
            return

        if step.domain == "speaker" and step.action == "power_off":
            if self._speaker is not None:
                await self._speaker.power_off()
            return

        if step.domain == "speaker" and step.action == "set_source":
            if self._speaker is not None:
                await self._speaker.set_source(str(args["source"]))
            return

        if step.domain == "ble" and step.action == "return_home":
            if self._ble is not None:
                await self._ble.return_home()
            return

        if step.domain == "ble" and step.action == "power_on":
            if self._ble is not None:
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

    async def _wait_for_tv_on(self, *, timeout_s: float) -> bool:
        if self._tv is None:
            return False
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                if self._tv.snapshot().presence_on is True:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def _wait_for_tv_off(self, *, timeout_s: float) -> bool:
        if self._tv is None:
            return False
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                if self._tv.snapshot().presence_on is False:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False


class FlowRunner:
    def __init__(
        self,
        *,
        runtime: Any,
        tv: Any = None,
        speaker: Any = None,
        ble: Any = None,
    ) -> None:
        self._sequences = SequenceRunner(runtime=runtime, tv=tv, speaker=speaker, ble=ble)

    async def run(
        self,
        *,
        name: str,
        trigger: str,
        args: dict[str, Any] | None = None,
        source: str = "intent",
    ) -> bool:
        del args
        return await self._sequences.run(name=name, trigger=trigger, args=None, source=source)
