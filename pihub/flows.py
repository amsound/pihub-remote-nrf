"""Named PiHub flows."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

TV_WAIT_TIMEOUT_S = 20.0
TV_CEC_SETTLE_S = 1.0
TV_OFF_SETTLE_S = 1.0

WATCH_VOLUME_PCT = 30
LISTEN_VOLUME_PCT = 22
LISTEN_PRESET = 1


@dataclass(frozen=True)
class SequenceStep:
    id: str
    domain: str
    action: str
    args: dict[str, Any] | None = None
    when: str | None = None
    timeout_s: float | None = None


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
            "tv_path_active": lambda snap: bool(snap.get("tv_path_active")),
            "tv_is_on": lambda snap: bool(snap.get("tv_is_on")),
            "default_run": lambda snap: bool(snap.get("default_run")),
            "override_run": lambda snap: bool(snap.get("override_run")),
            "override_run_and_is_wifi": lambda snap: bool(snap.get("override_run") and snap.get("is_wifi")),
            "default_run_or_override_run_and_is_wifi": lambda snap: bool(
                snap.get("default_run") or (snap.get("override_run") and snap.get("is_wifi"))
            ),
        }
        self._defs: dict[str, SequenceDefinition] = {
            "listen": SequenceDefinition(
                name="listen",
                steps=(
                    SequenceStep("set_mode", "mode", "set", {"name": "listen"}),
                    SequenceStep(
                        "speaker_preset",
                        "speaker",
                        "preset",
                        {"preset": LISTEN_PRESET},
                        when="default_run",
                        timeout_s=5.0,
                    ),
                    SequenceStep(
                        "speaker_volume",
                        "speaker",
                        "set_volume",
                        {"volume": LISTEN_VOLUME_PCT},
                        when="default_run_or_override_run_and_is_wifi",
                        timeout_s=5.0,
                    ),
                    SequenceStep(
                        "ble_return_home",
                        "ble",
                        "return_home",
                        when="tv_path_active",
                        timeout_s=5.0,
                    ),
                    SequenceStep(
                        "tv_settle",
                        "system",
                        "sleep",
                        {"seconds": TV_OFF_SETTLE_S},
                        when="tv_path_active",
                    ),
                    SequenceStep(
                        "tv_power_off",
                        "tv",
                        "power_off",
                        when="tv_path_active",
                        timeout_s=5.0,
                    ),
                    SequenceStep(
                        "wait_tv_off",
                        "wait",
                        "tv_off",
                        {"timeout_s": TV_WAIT_TIMEOUT_S},
                        when="tv_path_active",
                    ),
                ),
            ),
        }

    async def run(self, *, name: str, trigger: str, args: dict[str, Any] | None = None) -> bool:
        seq = self._defs.get((name or "").strip())
        if seq is None:
            raise KeyError(name)
        snapshot = self._build_snapshot(name=seq.name, args=args)
        logger.info("sequence started name=%s trigger=%s snapshot=%s", seq.name, trigger, self._format_snapshot(snapshot))
        for index, step in enumerate(seq.steps, start=1):
            if step.when and not self._predicates[step.when](snapshot):
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
                await self._run_step(step=step, trigger=trigger)
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
            logger.info("sequence step ok sequence=%s step=%s index=%d", seq.name, step.id, index)
        logger.info("sequence completed name=%s trigger=%s", seq.name, trigger)
        return True

    def _build_snapshot(self, *, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        flow_args = args or {}
        if name == "listen":
            source = self._speaker_source()
            override_run = bool(flow_args.get("override", False))
            tv_is_on = self._tv_is_on()
            prior_mode = self._runtime.mode
            return {
                "prior_mode": prior_mode,
                "tv_is_on": tv_is_on,
                "tv_path_active": tv_is_on or prior_mode == "watch",
                "speaker_source": source,
                "override_run": override_run,
                "default_run": not override_run,
                "is_wifi": source == "wifi",
            }
        raise KeyError(name)

    async def _run_step(self, *, step: SequenceStep, trigger: str) -> None:
        async def _invoke() -> None:
            await self._dispatch_step(step=step, trigger=trigger)

        if step.timeout_s is None:
            await _invoke()
            return
        await asyncio.wait_for(_invoke(), timeout=step.timeout_s)

    async def _dispatch_step(self, *, step: SequenceStep, trigger: str) -> None:
        args = step.args or {}
        if step.domain == "mode" and step.action == "set":
            await self._runtime.set_mode(str(args["name"]), trigger=f"flow.{self._runtime.mode if False else 'listen'}")
            return
        if step.domain == "speaker" and step.action == "preset":
            if self._speaker is None:
                return
            await self._speaker.preset(int(args["preset"]))
            return
        if step.domain == "speaker" and step.action == "set_volume":
            if self._speaker is None:
                return
            await self._speaker.set_volume(int(args["volume"]))
            return
        if step.domain == "ble" and step.action == "return_home":
            if self._ble is None:
                return
            await self._ble.return_home()
            return
        if step.domain == "tv" and step.action == "power_off":
            if self._tv is None:
                return
            await self._tv.power_off()
            return
        if step.domain == "system" and step.action == "sleep":
            await asyncio.sleep(float(args["seconds"]))
            return
        if step.domain == "wait" and step.action == "tv_off":
            await self._wait_for_tv_off(timeout_s=float(args["timeout_s"]))
            return
        raise ValueError(f"unsupported step domain/action: {step.domain}.{step.action}")

    @staticmethod
    def _format_snapshot(snapshot: dict[str, Any]) -> str:
        keys = ["prior_mode", "tv_is_on", "tv_path_active", "speaker_source", "default_run", "override_run", "is_wifi"]
        parts = [f"{k}={snapshot.get(k)!r}" for k in keys if k in snapshot]
        return " ".join(parts)

    def _tv_is_on(self) -> bool:
        if self._tv is None:
            return False
        try:
            snap = self._tv.snapshot()
            return snap.logical_on is True
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

    async def _wait_for_tv_off(self, *, timeout_s: float) -> bool:
        if self._tv is None:
            return False
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                if self._tv.snapshot().logical_on is False:
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
        self._runtime = runtime
        self._tv = tv
        self._speaker = speaker
        self._ble = ble
        self._sequences = SequenceRunner(runtime=runtime, tv=tv, speaker=speaker, ble=ble)

    async def run(self, *, name: str, trigger: str, args: dict[str, Any] | None = None) -> bool:
        name = (name or "").strip()
        if name == "listen":
            return await self._sequences.run(name=name, trigger=trigger, args=args)
        if name == "watch":
            return await self._run_watch(trigger=trigger)
        if name == "power_off":
            return await self._run_power_off(trigger=trigger)
        logger.warning("unknown flow name=%s trigger=%s", name, trigger)
        return False

    async def _run_watch(self, *, trigger: str) -> bool:
        del trigger
        tv_was_on = self._tv_is_on()
        await self._runtime.set_mode("watch", trigger="flow.watch")
        if self._tv is not None and not tv_was_on:
            logger.info("flow watch: powering on tv")
            await self._tv.power_on()
            await asyncio.sleep(TV_CEC_SETTLE_S)
        if self._ble is not None:
            logger.info("flow watch: powering on ble target")
            await self._ble.power_on()
        if self._speaker is not None:
            logger.info("flow watch: setting speaker volume=%d", WATCH_VOLUME_PCT)
            await self._speaker.set_volume(WATCH_VOLUME_PCT)
        if self._tv is not None and not tv_was_on:
            await self._wait_for_tv_on(timeout_s=TV_WAIT_TIMEOUT_S)
        return True

    async def _run_power_off(self, *, trigger: str) -> bool:
        del trigger
        current_mode = self._runtime.mode
        tv_was_on = self._tv_is_on()
        await self._runtime.set_mode("power_off", trigger="flow.power_off")
        if current_mode == "listen":
            if self._speaker is not None:
                logger.info("flow power_off: stopping speaker playback")
                await self._speaker.stop_playback()
                await asyncio.sleep(0.5)
                logger.info("flow power_off: powering off speaker")
                await self._speaker.power_off()
            return True
        if current_mode == "watch":
            if self._ble is not None and tv_was_on:
                logger.info("flow power_off: returning ble target home")
                await self._ble.return_home()
                await asyncio.sleep(TV_OFF_SETTLE_S)
            if self._tv is not None and tv_was_on:
                logger.info("flow power_off: powering off tv")
                await self._tv.power_off()
                await self._wait_for_tv_off(timeout_s=TV_WAIT_TIMEOUT_S)
            return True
        if self._ble is not None and tv_was_on:
            logger.info("flow power_off: returning ble target home")
            await self._ble.return_home()
            await asyncio.sleep(TV_OFF_SETTLE_S)
        if self._tv is not None and tv_was_on:
            logger.info("flow power_off: powering off tv")
            await self._tv.power_off()
            await self._wait_for_tv_off(timeout_s=TV_WAIT_TIMEOUT_S)
        return True

    def _tv_is_on(self) -> bool:
        return self._sequences._tv_is_on()

    async def _wait_for_tv_on(self, *, timeout_s: float) -> bool:
        if self._tv is None:
            return False
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                if self._tv.snapshot().logical_on is True:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def _wait_for_tv_off(self, *, timeout_s: float) -> bool:
        return await self._sequences._wait_for_tv_off(timeout_s=timeout_s)
