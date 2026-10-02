"""A device's first reading after start-up, tracked the same way by every backend.

Each TV and speaker backend owns one of these as `.initial`:

  received   True once the device has given a definite first reading
             (TV: on or off; speaker: its first status). It stays True across
             later disconnects: this is about start-up, not the live link.
  settled    received, or the first attempt to get a reading has failed
             (unreachable, no token). Either way the backend has had its go.

`await initial.wait(timeout)` returns once settled (or at the timeout) and says
whether a reading was received. Each outcome is logged once.
"""

from __future__ import annotations

import asyncio
import logging


class InitialStatus:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._settled = asyncio.Event()
        self.received = False

    @property
    def settled(self) -> bool:
        return self._settled.is_set()

    def mark_received(self, summary: str) -> None:
        if self.received:
            return
        self.received = True
        self._settled.set()
        self._logger.info("initial status received: %s", summary)

    def mark_failed(self, reason: str) -> None:
        """The first attempt ended without a reading. A later reading still counts as received."""
        if self._settled.is_set():
            return
        self._settled.set()
        self._logger.info("no initial status yet: %s", reason)

    async def wait(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._settled.wait(), timeout_s)
        except asyncio.TimeoutError:
            pass
        return self.received
