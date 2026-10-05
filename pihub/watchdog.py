"""Notice when the event loop stops running, say where, and restart if it stays stuck.

Everything in PiHub runs on one event loop, so anything that blocks it stops key
presses too. Two things watch it:

- A thread logs when the loop has been held up for more than SLOW_S, with the
  line of code it was in, and again when it comes back with how long it took.
- If the loop has not run for RESTART_AFTER_S, the process writes where every
  thread was to a file under /data and exits; Docker's restart policy brings it
  back. That part is faulthandler's own C thread, so it still fires when Python
  itself is stuck. The next start reports the file (log and History page) and
  keeps it.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from contextlib import suppress
from datetime import datetime
from typing import Any, TextIO

logger = logging.getLogger(__name__)

DEFAULT_STALL_PATH = "/data/watchdog-stall.txt"

TICK_S = 0.5                # how often the loop says it is alive
SLOW_S = 0.25               # a tick this late is logged
HISTORY_S = 1.0             # a hold-up this long also goes to the History page
REARM_EVERY_S = 5.0
RESTART_AFTER_S = 20.0      # counted from the last re-arm, so 15-20 s of silence
KEEP_STALL_FILES = 5
_REPORT_MAX_LINES = 30


def report_previous_stall(history: Any = None, *, path: str = DEFAULT_STALL_PATH) -> str | None:
    """If the last run was restarted by the watchdog, say so and keep its file.

    Call before LoopWatchdog.start(), which empties the file for this run.
    Returns the path the report was kept at, or None if there was none.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
        when = datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None
    if not text:
        return None

    root, ext = os.path.splitext(path)
    kept = f"{root}-{when:%Y%m%d-%H%M%S}{ext}"
    try:
        os.replace(path, kept)
    except OSError as exc:
        logger.warning("could not keep %s: %s", path, exc)
        kept = path
    _prune(root, ext)

    summary = (
        f"the previous run was restarted by the watchdog at {when:%Y-%m-%d %H:%M:%S}: "
        f"the event loop had not run for {RESTART_AFTER_S:.0f} s. report kept at {kept}"
    )
    stuck = _loop_thread_block(text.splitlines())
    shown = stuck[:_REPORT_MAX_LINES]
    if len(stuck) > len(shown):
        shown.append(f"... {len(stuck) - len(shown)} more lines in the report")
    logger.error("%s\nit was stuck here (most recent call first):\n%s", summary, "\n".join(shown))
    if history is not None:
        history.emit(
            kind="watchdog",
            level="error",
            message=summary,
            metadata={"error": stuck[0].strip() if stuck else "", "file": kept, "at": when.timestamp()},
        )
    return kept


def _loop_thread_block(lines: list[str]) -> list[str]:
    """The event loop thread's frames from a faulthandler report.

    faulthandler lists threads newest first, so the main thread (the loop) is last.
    """
    starts = [i for i, line in enumerate(lines) if line.startswith("Thread 0x")]
    if not starts:
        return []
    return [line for line in lines[starts[-1] + 1:] if line.strip()]


def _prune(root: str, ext: str) -> None:
    folder, stem = os.path.split(root)
    try:
        old = sorted(n for n in os.listdir(folder or ".") if n.startswith(stem + "-") and n.endswith(ext))
    except OSError:
        return
    for name in old[:-KEEP_STALL_FILES]:
        with suppress(OSError):
            os.remove(os.path.join(folder, name))


class LoopWatchdog:
    def __init__(self, history: Any = None, *, path: str = DEFAULT_STALL_PATH) -> None:
        self._history = history
        self._path = path
        self._file: TextIO | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._beat = threading.Event()
        self._stop = threading.Event()
        self._loop_thread_id = 0

    async def start(self) -> None:
        self._loop_thread_id = threading.get_ident()
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            self._file = open(self._path, "w", encoding="utf-8")
        except OSError as exc:
            # Still restart on a stall; the report goes to the container log instead.
            logger.warning("watchdog: cannot write %s (%s); a stall report would go to the log only", self._path, exc)
        self._arm()
        self._task = asyncio.create_task(self._ticker(), name="watchdog:tick")
        self._thread = threading.Thread(target=self._watch, name="watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        """Stand down, so a slow shutdown is not taken for a stall."""
        faulthandler.cancel_dump_traceback_later()
        self._stop.set()
        self._beat.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._file is not None:
            self._file.close()
            self._file = None

    def _arm(self) -> None:
        faulthandler.dump_traceback_later(RESTART_AFTER_S, exit=True, file=self._file or sys.stderr)

    async def _ticker(self) -> None:
        loop = asyncio.get_running_loop()
        armed_at = loop.time()
        while True:
            self._beat.set()
            if loop.time() - armed_at >= REARM_EVERY_S:
                self._arm()
                armed_at = loop.time()
            await asyncio.sleep(TICK_S)

    def _watch(self) -> None:
        last_beat = time.monotonic()
        while not self._stop.is_set():
            if self._beat.wait(TICK_S + SLOW_S):
                self._beat.clear()
                last_beat = time.monotonic()
                continue

            # The tick is late: the loop is in something that is not giving way.
            where = self._where()
            logger.warning("event loop held up for over %d ms, in: %s", SLOW_S * 1000, where)
            self._beat.wait()
            if self._stop.is_set():
                return
            held_s = max(0.0, time.monotonic() - last_beat - TICK_S)
            logger.warning("event loop running again after %.1f s", held_s)
            if held_s >= HISTORY_S and self._history is not None:
                self._history.emit(
                    kind="watchdog",
                    level="warning",
                    message=f"everything was held up for {held_s:.1f} s",
                    metadata={"error": where, "held_s": round(held_s, 1)},
                )

    def _where(self) -> str:
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return "unknown"
        # asyncio's own frames are always the same; show ours.
        stack = [f for f in traceback.extract_stack(frame) if f"{os.sep}asyncio{os.sep}" not in f.filename][-4:]
        return " < ".join(
            f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(stack)
        )
