"""Named PiHub flows."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

TV_WAIT_TIMEOUT_S = 20.0
SPEAKER_WAIT_TIMEOUT_S = 5.0
TV_CEC_SETTLE_S = 3.0
TV_OFF_SETTLE_S = 3.0


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

    async def run(self, *, name: str, trigger: str) -> bool:
        name = (name or "").strip()
        if name == "watch":
            return await self._run_watch(trigger=trigger)
        if name == "listen":
            return await self._run_listen(trigger=trigger)
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
            await self._wait_for_tv_on(timeout_s=TV_WAIT_TIMEOUT_S)
            await asyncio.sleep(TV_CEC_SETTLE_S)

        if self._ble is not None:
            logger.info("flow watch: powering on ble target")
            await self._ble.power_on()

        if self._speaker is not None:
            logger.info("flow watch: setting speaker source=hdmi volume=30")
            await self._speaker.set_source("hdmi")
            await self._wait_for_speaker_source("hdmi", timeout_s=SPEAKER_WAIT_TIMEOUT_S)
            await self._set_speaker_volume_pct(30)

        return True

    async def _run_listen(self, *, trigger: str) -> bool:
        del trigger
        tv_was_on = self._tv_is_on()

        await self._runtime.set_mode("listen", trigger="flow.listen")

        if self._tv is not None and tv_was_on:
            logger.info("flow listen: powering off tv")
            await self._tv.power_off()
            await self._wait_for_tv_off(timeout_s=TV_WAIT_TIMEOUT_S)

        if self._ble is not None and tv_was_on:
            logger.info("flow listen: returning ble target home")
            await self._ble.return_home()

        if self._speaker is not None:
            logger.info("flow listen: preset=1 volume=20")
            await self._speaker.preset(1)
            await self._set_speaker_volume_pct(20)

        return True

    async def _run_power_off(self, *, trigger: str) -> bool:
        del trigger
        tv_was_on = self._tv_is_on()

        await self._runtime.set_mode("power_off", trigger="flow.power_off")

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
        if self._tv is None:
            return False
        try:
            snap = self._tv.snapshot()
            return snap.logical_on is True
        except Exception:
            return False

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

    async def _wait_for_speaker_source(self, source: str, *, timeout_s: float) -> bool:
        if self._speaker is None:
            return False
        want = (source or "").strip().lower()
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                snap = self._speaker.snapshot()
                got = (snap.get("source") or "").strip().lower()
                if got == want:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def _set_speaker_volume_pct(self, pct: int) -> None:
        if self._speaker is None:
            return

        target = max(0, min(100, int(pct)))
        snap = self._speaker.snapshot()
        current = snap.get("volume_pct")
        if not isinstance(current, int):
            current = 0

        steps = abs(target - current) // 2
        if steps == 0:
            return

        fn = self._speaker.volume_up if target > current else self._speaker.volume_down
        for _ in range(steps):
            await fn()