"""PiHub flows: watch, listen, power_off and their device-state variants.

Each flow is a plain async function per speaker backend, read top to bottom.
A flow works from one snapshot taken at its start (after the TV state has been
refreshed), so every "if the TV is on" in it agrees.

Step rules (the same for every flow):
- `await ctx.step(...)` runs a step and waits for it.
- `ctx.step(..., background=True)` starts it and moves straight on; it is
  checked when the flow ends (10 s unless the step has its own timeout).
- `when="<condition>"` skips the step (recorded as skipped) if the snapshot
  says otherwise.
- A failing step never stops the flow; the flow fails at the end, and the
  mode only changes when it succeeds (runtime.py).
- Every step lands in the flow report shown on the History page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .history import FlowRunReport, FlowStepReport
from .settings import SettingsData
from .slots import listen_slot, play_slot, resolve_slot

logger = logging.getLogger(__name__)

LISTEN_SOURCES = {"wifi", "airplay", "multiroom-secondary"}
BACKGROUND_SETTLE_TIMEOUT_S = 10.0
# Upper bound on the TV state check at the start of a flow (normally milliseconds).
TV_REFRESH_TIMEOUT_S = 3.5
TV_POWER_TIMEOUT_S = 8.0
MULTIROOM_SETTLE_S = 1.0
_FLOW_DEFAULTS = SettingsData()

# Samsung soundbar: after the TV comes on, HDMI-CEC/ARC takes a few seconds to
# hand the soundbar over; a volume set before then is overridden by the TV.
SOUNDBAR_TV_WAKE_TIMEOUT_S = 30.0
SOUNDBAR_ARC_SETTLE_S = 5.0


class FlowStepFailures(RuntimeError):
    def __init__(self, *, sequence_name: str, failures: list[dict[str, str]]) -> None:
        self.sequence_name = sequence_name
        self.failures = list(failures)
        detail = ", ".join(
            f"{item.get('step_id', '?')}: {item.get('error', 'failed')}"
            for item in self.failures
        )
        super().__init__(f"flow_failed: {detail}")


class FlowWaitTimeout(RuntimeError):
    def __init__(self, *, kind: str, timeout_s: float) -> None:
        self.kind = kind
        self.timeout_s = timeout_s
        super().__init__(f"{kind}_timeout")


# Conditions a step can depend on, evaluated against the flow's start snapshot.
_CONDITIONS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "tv_is_on": lambda snap: snap["tv_is_on"],
    "tv_is_off": lambda snap: not snap["tv_is_on"],
    "speaker_is_on_listen_source": lambda snap: snap["speaker_source"] in LISTEN_SOURCES,
    "speaker_is_in_multiroom": lambda snap: (
        snap["speaker_source"] in LISTEN_SOURCES
        and (snap["speaker_is_multiroom_guest"] or snap["speaker_is_multiroom_host"])
    ),
    "speaker_should_stop": lambda snap: (
        snap["speaker_source"] in LISTEN_SOURCES and not snap["speaker_is_multiroom_guest"]
    ),
}


class FlowContext:
    """One flow run: the snapshot, the devices, and step bookkeeping."""

    def __init__(
        self,
        *,
        flow_name: str,
        snapshot: dict[str, Any],
        report: FlowRunReport | None,
        tv: Any,
        speaker: Any,
        ble: Any,
        settings: Any,
        speaker_backend: str,
    ) -> None:
        self.flow_name = flow_name
        self.snapshot = snapshot
        self._report = report
        self._tv = tv
        self._speaker = speaker
        self._ble = ble
        self._settings = settings
        self._speaker_backend = speaker_backend
        self.failures: list[dict[str, str]] = []
        self._background: list[tuple[str, str, str, float | None, FlowStepReport | None, asyncio.Task]] = []

    # ---- step running ----

    async def step(
        self,
        step_id: str,
        domain: str,
        action: str,
        run: Callable[[], Awaitable[Any]],
        *,
        when: str | None = None,
        timeout_s: float | None = None,
        background: bool = False,
    ) -> None:
        step_report = None
        if self._report is not None:
            step_report = self._report.add_step(
                step_id=step_id,
                domain=domain,
                action=action,
                mode="dispatch" if background else "await",
            )

        if when and not _CONDITIONS[when](self.snapshot):
            logger.debug("flow %s step %s skipped (%s is false)", self.flow_name, step_id, when)
            if step_report is not None:
                step_report.finish(status="skipped", reason=f"when_false:{when}")
            return

        async def _run() -> None:
            if timeout_s is None:
                await run()
            else:
                await asyncio.wait_for(run(), timeout=timeout_s)

        logger.debug("flow %s step %s start (%s.%s)", self.flow_name, step_id, domain, action)

        if background:
            task = asyncio.create_task(_run(), name=f"flow:{self.flow_name}:{step_id}")
            if step_report is not None:
                step_report.mark_dispatched()
            self._background.append((step_id, domain, action, timeout_s, step_report, task))
            return

        try:
            await _run()
        except asyncio.TimeoutError:
            self._fail(step_id, domain, action, "step_run", f"timeout:{timeout_s:g}s", step_report)
        except Exception as exc:
            self._fail(step_id, domain, action, "step_run", str(exc), step_report)
        else:
            if step_report is not None:
                step_report.finish(status="ok")

    async def settle_background(self) -> None:
        """Wait for background steps; each gets its own timeout, or 10 s."""
        for step_id, domain, action, timeout_s, step_report, task in self._background:
            try:
                if timeout_s is None:
                    await asyncio.wait_for(task, timeout=BACKGROUND_SETTLE_TIMEOUT_S)
                else:
                    await task
            except asyncio.TimeoutError:
                if timeout_s is None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    error = f"dispatch_settle_timeout:{BACKGROUND_SETTLE_TIMEOUT_S:g}s"
                else:
                    error = f"timeout:{timeout_s:g}s"
                self._fail(step_id, domain, action, "dispatch_settle", error, step_report, outcome=True)
            except asyncio.CancelledError:
                self._fail(step_id, domain, action, "dispatch_settle", "cancelled", step_report, outcome=True)
            except Exception as exc:
                self._fail(step_id, domain, action, "dispatch_settle", str(exc), step_report, outcome=True)
            else:
                if step_report is not None:
                    step_report.settle_outcome(status="ok")

    def _fail(
        self,
        step_id: str,
        domain: str,
        action: str,
        phase: str,
        error: str,
        step_report: FlowStepReport | None,
        *,
        outcome: bool = False,
    ) -> None:
        logger.warning(
            "flow %s step %s failed (%s.%s): %s; continuing",
            self.flow_name, step_id, domain, action, error,
        )
        if step_report is not None:
            if outcome:
                step_report.settle_outcome(status="failed", error=error)
            else:
                step_report.finish(status="failed", error=error)
        self.failures.append(
            {"step_id": step_id, "domain": domain, "action": action, "phase": phase, "error": error}
        )

    # ---- readiness ----

    def _require_speaker(self) -> Any:
        sp = self._speaker
        if sp is None:
            raise RuntimeError("speaker_unavailable")
        st = sp.state
        if st.ready:
            return sp
        detail = f": {st.last_error}" if st.last_error else ""
        if not st.reachable:
            raise RuntimeError(f"speaker_not_reachable{detail}")
        if not st.connected:
            raise RuntimeError(f"speaker_not_connected{detail}")
        raise RuntimeError(f"speaker_not_ready{detail}")

    def _require_ble(self) -> Any:
        ble = self._ble
        if ble is None:
            raise RuntimeError("ble_unavailable")
        status = ble.status
        if status.get("ready"):
            return ble
        detail = f": {status['last_error']}" if status.get("last_error") else ""
        if not status.get("transport_open"):
            raise RuntimeError(f"ble_transport_down{detail}")
        if not status.get("connected"):
            raise RuntimeError(f"ble_not_connected{detail}")
        raise RuntimeError(f"ble_not_ready{detail}")

    def _require_tv(self) -> Any:
        if self._tv is None:
            raise RuntimeError("tv_unavailable")
        if not self._tv.snapshot().token_present:
            raise RuntimeError("tv_token_missing")
        return self._tv

    # ---- actions ----

    async def apple_tv_home(self) -> None:
        await self._require_ble().return_home()

    async def apple_tv_on(self) -> None:
        await self._require_ble().power_on()

    async def apple_tv_off(self) -> None:
        await self._require_ble().power_off()

    async def tv_on(self) -> None:
        if not await self._require_tv().power_on():
            raise RuntimeError("tv_power_on_failed")

    async def tv_off(self) -> None:
        if not await self._require_tv().power_off():
            raise RuntimeError("tv_power_off_failed")

    async def watch_volume(self) -> None:
        await self._require_speaker().set_volume(self._volume("watch"))

    async def listen_volume(self) -> None:
        await self._require_speaker().set_volume(self._volume("listen"))

    async def play_listen_target(self) -> None:
        sp = self._require_speaker()
        if self._settings is None:
            raise ValueError("listen target configured but settings unavailable")
        slot = listen_slot(self._settings, self._speaker_backend)
        await play_slot(sp, resolve_slot(self._settings, self._speaker_backend, slot))

    async def speaker_stop(self) -> None:
        await self._require_speaker().stop_playback()

    async def speaker_off(self) -> None:
        await self._require_speaker().power_off()

    async def speaker_hdmi(self) -> None:
        await self._require_speaker().set_source("hdmi")

    async def speaker_leave_group(self) -> None:
        await self._require_speaker().leave_native_multiroom_if_needed()

    async def leave_cast(self) -> None:
        await self._require_speaker().leave_cast()

    async def wait_tv_on(self, timeout_s: float) -> None:
        if self._tv is None:
            logger.debug("skipping wait_tv_on: no tv domain")
            return
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            if self._tv.snapshot().presence_on is True:
                return
            await asyncio.sleep(0.2)
        raise FlowWaitTimeout(kind="tv_on", timeout_s=timeout_s)

    def _volume(self, which: str) -> int:
        watch = which == "watch"
        if self._settings is None:
            return _FLOW_DEFAULTS.watch_volume_pct if watch else _FLOW_DEFAULTS.listen_volume_pct
        return self._settings.get_watch_volume_pct() if watch else self._settings.get_listen_volume_pct()


# =====================================================================
# Audio Pro / LinkPlay: PiHub drives the TV, the Apple TV and the speaker.
# =====================================================================

async def _audiopro_stop_speaker(ctx: FlowContext) -> None:
    """Stop our own listening, and leave a native multiroom group."""
    await ctx.step("speaker_stop", "speaker", "stop_playback", ctx.speaker_stop,
                   when="speaker_should_stop")
    await ctx.step("speaker_leave_group", "speaker", "leave_native_multiroom_if_needed",
                   ctx.speaker_leave_group, when="speaker_is_in_multiroom")
    await ctx.step("speaker_settle_after_group", "system", "sleep",
                   lambda: asyncio.sleep(MULTIROOM_SETTLE_S), when="speaker_is_in_multiroom")


async def _audiopro_listen(ctx: FlowContext) -> None:
    await ctx.step("apple_tv_return_home", "ble", "return_home", ctx.apple_tv_home,
                   when="tv_is_on", background=True)
    await ctx.step("tv_power_off", "tv", "power_off", ctx.tv_off,
                   when="tv_is_on", timeout_s=TV_POWER_TIMEOUT_S)
    await ctx.step("speaker_listen_volume", "speaker", "set_volume", ctx.listen_volume,
                   background=True)
    await ctx.step("speaker_play_listen_target", "speaker", "play_listen_target",
                   ctx.play_listen_target)


async def _audiopro_watch(ctx: FlowContext) -> None:
    await _audiopro_stop_speaker(ctx)
    await ctx.step("apple_tv_power_on", "ble", "power_on", ctx.apple_tv_on,
                   when="tv_is_off", background=True)
    await ctx.step("tv_power_on", "tv", "power_on", ctx.tv_on,
                   when="tv_is_off", timeout_s=TV_POWER_TIMEOUT_S)
    await ctx.step("speaker_watch_volume", "speaker", "set_volume", ctx.watch_volume,
                   background=True)
    await ctx.step("speaker_hdmi_source", "speaker", "set_source", ctx.speaker_hdmi)


async def _audiopro_listen_signal(ctx: FlowContext) -> None:
    await ctx.step("tv_power_off", "tv", "power_off", ctx.tv_off,
                   when="tv_is_on", timeout_s=TV_POWER_TIMEOUT_S, background=True)
    await ctx.step("apple_tv_return_home", "ble", "return_home", ctx.apple_tv_home,
                   when="tv_is_on")


async def _audiopro_watch_signal(ctx: FlowContext) -> None:
    await _audiopro_stop_speaker(ctx)
    await ctx.step("tv_power_on", "tv", "power_on", ctx.tv_on,
                   when="tv_is_off", timeout_s=TV_POWER_TIMEOUT_S)
    await ctx.step("speaker_watch_volume", "speaker", "set_volume", ctx.watch_volume,
                   background=True)
    await ctx.step("speaker_hdmi_source", "speaker", "set_source", ctx.speaker_hdmi)


async def _audiopro_power_off(ctx: FlowContext) -> None:
    await ctx.step("apple_tv_return_home", "ble", "return_home", ctx.apple_tv_home,
                   when="tv_is_on", background=True)
    await ctx.step("tv_power_off", "tv", "power_off", ctx.tv_off,
                   when="tv_is_on", timeout_s=TV_POWER_TIMEOUT_S)
    await _audiopro_stop_speaker(ctx)
    await ctx.step("speaker_power_off", "speaker", "power_off", ctx.speaker_off,
                   when="speaker_is_on_listen_source")


# =====================================================================
# Samsung soundbar: the Apple TV and HDMI-CEC switch the TV and soundbar;
# PiHub never sends TV power commands.
# =====================================================================

async def _soundbar_listen(ctx: FlowContext) -> None:
    await ctx.step("apple_tv_power_off", "ble", "power_off", ctx.apple_tv_off,
                   when="tv_is_on", background=True)
    await ctx.step("speaker_listen_volume", "speaker", "set_volume", ctx.listen_volume,
                   background=True)
    await ctx.step("speaker_play_listen_target", "speaker", "play_listen_target",
                   ctx.play_listen_target, background=True)


async def _soundbar_watch(ctx: FlowContext) -> None:
    # Stop the radio first: while Cast holds the soundbar, HDMI-CEC cannot
    # switch it to the TV, and the volume change below would land on the music.
    await ctx.step("speaker_leave_cast", "speaker", "leave_cast", ctx.leave_cast)
    await ctx.step("apple_tv_power_on", "ble", "power_on", ctx.apple_tv_on,
                   when="tv_is_off")
    await ctx.step("wait_tv_on", "wait", "tv_on",
                   lambda: ctx.wait_tv_on(SOUNDBAR_TV_WAKE_TIMEOUT_S), when="tv_is_off")
    await ctx.step("arc_settle", "system", "sleep",
                   lambda: asyncio.sleep(SOUNDBAR_ARC_SETTLE_S), when="tv_is_off")
    await ctx.step("speaker_watch_volume", "speaker", "set_volume", ctx.watch_volume,
                   background=True)


async def _soundbar_listen_signal(ctx: FlowContext) -> None:
    await ctx.step("apple_tv_power_off", "ble", "power_off", ctx.apple_tv_off,
                   when="tv_is_on")


async def _soundbar_watch_signal(ctx: FlowContext) -> None:
    # The TV came on by other means: hand it the soundbar too.
    await ctx.step("speaker_leave_cast", "speaker", "leave_cast", ctx.leave_cast)
    await ctx.step("arc_settle", "system", "sleep", lambda: asyncio.sleep(SOUNDBAR_ARC_SETTLE_S))
    await ctx.step("speaker_watch_volume", "speaker", "set_volume", ctx.watch_volume)


async def _soundbar_power_off(ctx: FlowContext) -> None:
    await ctx.step("apple_tv_power_off", "ble", "power_off", ctx.apple_tv_off,
                   when="tv_is_on")
    await ctx.step("speaker_stop", "speaker", "stop_playback", ctx.speaker_stop,
                   when="speaker_is_on_listen_source")


FlowFn = Callable[[FlowContext], Awaitable[None]]

_FLOWS: dict[str, dict[str, FlowFn]] = {
    "audiopro": {
        "listen": _audiopro_listen,
        "watch": _audiopro_watch,
        "listen_signal": _audiopro_listen_signal,
        "watch_signal": _audiopro_watch_signal,
        "power_off": _audiopro_power_off,
    },
    "samsung_soundbar": {
        "listen": _soundbar_listen,
        "watch": _soundbar_watch,
        "listen_signal": _soundbar_listen_signal,
        "watch_signal": _soundbar_watch_signal,
        "power_off": _soundbar_power_off,
    },
}

# The mode each flow leaves the remote in when it succeeds.
_TARGET_MODES = {
    "listen": "listen",
    "watch": "watch",
    "listen_signal": "listen",
    "watch_signal": "watch",
    "power_off": "power_off",
}


class FlowRunner:
    """Runs a named flow for the configured speaker backend."""

    def __init__(
        self,
        *,
        tv: Any = None,
        speaker: Any = None,
        ble: Any = None,
        settings: Any = None,
        speaker_backend: str | None = None,
    ) -> None:
        self._tv = tv
        self._speaker = speaker
        self._ble = ble
        self._settings = settings
        self._speaker_backend = (speaker_backend or "").strip().lower()
        profile = "samsung_soundbar" if self._speaker_backend == "samsung_soundbar" else "audiopro"
        self._flows = _FLOWS[profile]

    def target_mode(self, name: str) -> str | None:
        name = (name or "").strip()
        return _TARGET_MODES.get(name) if name in self._flows else None

    async def run(
        self,
        *,
        name: str,
        trigger: str,
        source: str = "intent",
        report: FlowRunReport | None = None,
    ) -> bool:
        """Run a flow. False for an unknown flow; FlowStepFailures if steps failed."""
        flow = self._flows.get((name or "").strip())
        if flow is None:
            logger.warning("unknown flow name=%s trigger=%s source=%s", name, trigger, source)
            return False

        await self._refresh_tv_presence()
        snapshot = self._snapshot()
        logger.debug("flow %s start trigger=%s source=%s snapshot=%s", name, trigger, source, snapshot)

        ctx = FlowContext(
            flow_name=name,
            snapshot=snapshot,
            report=report,
            tv=self._tv,
            speaker=self._speaker,
            ble=self._ble,
            settings=self._settings,
            speaker_backend=self._speaker_backend,
        )
        await flow(ctx)
        await ctx.settle_background()

        if ctx.failures:
            raise FlowStepFailures(sequence_name=name, failures=ctx.failures)
        return True

    async def _refresh_tv_presence(self) -> None:
        """Make "is the TV on?" current before the flow decides anything on it."""
        if self._tv is None:
            return
        try:
            await asyncio.wait_for(self._tv.refresh_presence(), timeout=TV_REFRESH_TIMEOUT_S)
        except Exception:
            logger.debug("tv presence refresh failed; using cached state", exc_info=True)

    def _snapshot(self) -> dict[str, Any]:
        speaker_snap = self._speaker.snapshot() if self._speaker is not None else {}
        tv_is_on = self._tv is not None and self._tv.snapshot().presence_on is True

        return {
            "tv_is_on": tv_is_on,
            "speaker_source": str(speaker_snap.get("source") or "").strip().lower(),
            "speaker_is_multiroom_guest": bool(speaker_snap.get("multiroom_guest_active")),
            "speaker_is_multiroom_host": bool(speaker_snap.get("multiroom_host_active")),
        }
