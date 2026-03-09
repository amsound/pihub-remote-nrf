"""Override detector and arbiter."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
POST_BOOT_GRACE_S = 3.0
DECISION_COOLDOWN_S = 5.0
LISTEN_SOURCES = {"airplay", "wifi", "multiroom-secondary"}


@dataclass
class OverrideSnapshot:
    tv_on: bool | None
    tv_last_change_age_s: int | None
    speaker_playback: str | None
    speaker_source: str | None


class OverrideEngine:
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
        self._boot_monotonic: float | None = None

        self._prev: OverrideSnapshot | None = None
        self._last_tv_on_ts: float | None = None
        self._last_listen_ts: float | None = None

        self._last_decision: str | None = None
        self._last_decision_ts: float | None = None

        self._last_seen_signature: tuple | None = None
        self._last_action_signature: tuple | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt.clear()
        self._boot_monotonic = asyncio.get_running_loop().time()
        self._task = asyncio.create_task(self._runner(), name="override-engine")

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
        now = asyncio.get_running_loop().time()

        if self._prev is not None:
            if self._prev.tv_on is not True and snap.tv_on is True:
                self._last_tv_on_ts = now

            prev_listen = self._is_listen_signal(self._prev)
            curr_listen = self._is_listen_signal(snap)
            if not prev_listen and curr_listen:
                self._last_listen_ts = now

        if self._runtime.flow_running:
            self._last_seen_signature = None
            self._prev = snap
            return

        desired_mode = self._decide_mode(current_mode=self._runtime.mode, snap=snap)

        if desired_mode is None:
            self._last_seen_signature = None
            self._prev = snap
            return
        
        if desired_mode == self._runtime.mode:
            self._last_seen_signature = None
            self._prev = snap
            return
        
        seen_signature = (
            self._runtime.mode,
            desired_mode,
            snap.tv_on,
            snap.speaker_playback,
            snap.speaker_source,
            self._apply_mode,
        )

        if seen_signature == self._last_seen_signature:
            self._prev = snap
            return

        self._last_seen_signature = seen_signature

        logger.info(
            "override detected current_mode=%s desired_mode=%s tv_on=%s speaker_playback=%s speaker_source=%s apply_mode=%s",
            self._runtime.mode,
            desired_mode,
            snap.tv_on,
            snap.speaker_playback,
            snap.speaker_source,
            "true" if self._apply_mode else "false",
        )

        if self._runtime.flow_running:
            logger.info("override skipped desired_mode=%s reason=flow_running", desired_mode)
            self._prev = snap
            return

        # A stable description of the action we'd take for this situation.
        action_signature = (
            desired_mode,
            self._apply_mode,
        )

        # Only use cooldown for repeated action attempts, not repeated polling of the same stable state.
        if self._last_action_signature == action_signature and self._last_decision_ts is not None:
            if (now - self._last_decision_ts) < DECISION_COOLDOWN_S:
                logger.info("override skipped desired_mode=%s reason=cooldown", desired_mode)
                self._prev = snap
                return

        self._last_decision = desired_mode
        self._last_decision_ts = now
        self._last_action_signature = action_signature

        if not self._apply_mode:
            logger.info("override apply suppressed by config desired_mode=%s", desired_mode)
            self._prev = snap
            return

        await self._runtime.set_mode(desired_mode, trigger="override")
        self._prev = snap

    def _snapshot(self) -> OverrideSnapshot:
        tv_on = None
        tv_last_change_age_s = None
        if self._tv is not None:
            try:
                s = self._tv.snapshot()
                tv_on = s.logical_on
                tv_last_change_age_s = s.last_change_age_s
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

        return OverrideSnapshot(
            tv_on=tv_on,
            tv_last_change_age_s=tv_last_change_age_s,
            speaker_playback=speaker_playback,
            speaker_source=speaker_source,
        )

    def _is_listen_signal(self, snap: OverrideSnapshot) -> bool:
        playback = (snap.speaker_playback or "").strip().lower()
        source = (snap.speaker_source or "").strip().lower()
        return playback in {"play", "load"} and source in LISTEN_SOURCES

    def _decide_mode(self, *, current_mode: str, snap: OverrideSnapshot) -> str | None:
        watch_claim = snap.tv_on is True
        listen_claim = self._is_listen_signal(snap)

        if watch_claim and not listen_claim:
            return "watch"
        if listen_claim and not watch_claim:
            return "listen"
        if not watch_claim and not listen_claim:
            return None

        # Equal-weight conflict: most recent meaningful transition wins.
        if self._last_tv_on_ts is not None and self._last_listen_ts is not None:
            if self._last_tv_on_ts > self._last_listen_ts:
                return "watch"
            if self._last_listen_ts > self._last_tv_on_ts:
                return "listen"

        # Ambiguous: stay put if already on one of the candidates.
        if current_mode in {"watch", "listen"}:
            return current_mode

        return None