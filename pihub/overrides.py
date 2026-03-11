"""Device-state monitor that reports external state changes back to runtime."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
POST_BOOT_GRACE_S = 3.0


@dataclass
class DeviceStateSnapshot:
    tv_on: bool | None
    speaker_playback: str | None
    speaker_source: str | None


class OverrideEngine:
    """Compatibility wrapper around the old app wiring.

    Despite the historic name, this no longer applies mode changes directly.
    It only detects device-state changes and asks runtime to route them.
    """

    def __init__(
        self,
        *,
        runtime: Any,
        tv: Any = None,
        speaker: Any = None,
        apply_mode: bool = True,
    ) -> None:
        self._runtime = runtime
        self._tv = tv
        self._speaker = speaker
        self._apply_mode = apply_mode
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._last_dispatched_signature: tuple[Any, ...] | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._runner(), name="device-state-monitor")

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task:
            self._task.cancel()
            with asyncio.CancelledError:
                pass

    async def _runner(self) -> None:
        try:
            await asyncio.sleep(POST_BOOT_GRACE_S)
            while not self._stop_evt.is_set():
                await self.poll_once()
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    async def poll_once(self) -> None:
        snap = self._snapshot()
        signature = (snap.tv_on, snap.speaker_playback, snap.speaker_source)
        if signature == self._last_dispatched_signature:
            return

        if not self._apply_mode:
            logger.info(
                "device state change detected but apply suppressed tv_on=%s speaker_playback=%s speaker_source=%s",
                snap.tv_on,
                snap.speaker_playback,
                snap.speaker_source,
            )
            self._last_dispatched_signature = signature
            return

        result = await self._runtime.on_device_state_change(
            {
                "tv_on": snap.tv_on,
                "speaker_playback": snap.speaker_playback,
                "speaker_source": snap.speaker_source,
            }
        )

        reason = result.get("reason")
        if result.get("ok") is False and reason == "runner_busy":
            logger.info(
                "device state change deferred tv_on=%s speaker_playback=%s speaker_source=%s reason=runner_busy",
                snap.tv_on,
                snap.speaker_playback,
                snap.speaker_source,
            )
            return

        self._last_dispatched_signature = signature

    def _snapshot(self) -> DeviceStateSnapshot:
        tv_on = None
        if self._tv is not None:
            try:
                s = self._tv.snapshot()
                tv_on = s.logical_on
            except Exception:
                pass

        speaker_playback = None
        speaker_source = None
        if self._speaker is not None:
            try:
                s = self._speaker.snapshot()
                speaker_playback = (s.get("playback_status") or None)
                speaker_source = (s.get("source") or None)
            except Exception:
                pass

        return DeviceStateSnapshot(
            tv_on=tv_on,
            speaker_playback=speaker_playback,
            speaker_source=speaker_source,
        )
