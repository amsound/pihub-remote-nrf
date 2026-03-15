"""Lightweight HTTP health/status endpoint for external monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Optional

from aiohttp import web

from .runtime import RuntimeEngine

from .ble_dongle import BleDongleLink
from .unifying_input import UnifyingReader
from .audiopro_speaker import AudioProSpeaker
from .samsung_tv import TvController



class HealthServer:
    """Expose a simple JSON health snapshot for scrapers and system logic."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ble: Optional[BleDongleLink] = None,
        reader: Optional[UnifyingReader] = None,
        tv: Optional[TvController] = None,
        speaker: Optional[AudioProSpeaker] = None,
        runtime: Optional[RuntimeEngine] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._ble = ble
        self._reader = reader
        self._tv = tv
        self._speaker = speaker
        self._runtime = runtime

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        if self._runner is not None:
            return

        app = web.Application()
        app.add_routes(
            [
                web.get("/health", self._handle_health),
                web.post("/flow/run/{name}", self._handle_flow_run),
                web.post("/mode/set/{name}", self._handle_mode_set),
                web.post("/command", self._handle_command),
            ]
        )

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        self._site = None
        if runner is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner.cleanup()

    async def _handle_health(self, _: web.Request) -> web.Response:
        snapshot = self.snapshot()
        status = 200 if snapshot["status"] == "ok" else 503
        return web.json_response(snapshot, status=status)

    async def _handle_flow_run(self, request: web.Request) -> web.Response:
        if self._runtime is None:
            return web.json_response({"ok": False, "error": "runtime unavailable"}, status=503)

        name = (request.match_info.get("name") or "").strip()
        payload = await self._maybe_json(request)
        trigger = str((payload or {}).get("trigger") or "http.flow")
        result = await self._runtime.run_flow(name, trigger=trigger)
        status = 200 if result.get("ok") else 409 if result.get("reason") == "runner_busy" else 400
        return web.json_response(result, status=status)

    async def _handle_mode_set(self, request: web.Request) -> web.Response:
        if self._runtime is None:
            return web.json_response({"ok": False, "error": "runtime unavailable"}, status=503)

        name = (request.match_info.get("name") or "").strip()
        payload = await self._maybe_json(request)
        trigger = str((payload or {}).get("trigger") or "http.mode")
        result = await self._runtime.set_mode(name, trigger=trigger)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def _handle_command(self, request: web.Request) -> web.Response:
        if self._runtime is None:
            return web.json_response({"ok": False, "error": "runtime unavailable"}, status=503)

        payload = await self._maybe_json(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "json body required"}, status=400)

        result = await self._runtime.on_cmd(payload)
        status = 200 if result.get("ok") else 409 if result.get("reason") == "runner_busy" else 400
        return web.json_response(result, status=status)

    async def _maybe_json(self, request: web.Request) -> dict | None:
        if request.content_length in (None, 0):
            return {}
        try:
            data = await request.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _domain_status(*, enabled: bool, degraded: bool) -> str:
        if not enabled:
            return "disabled"
        return "degraded" if degraded else "ok"

    def snapshot(self) -> dict:
        pihub_id = socket.gethostname()
    
        runtime_state = (
            self._runtime.snapshot()
            if self._runtime is not None
            else {
                "mode": None,
                "last_flow": None,
                "flow_running": False,
                "last_trigger": None,
            }
        )

        degraded_reasons: list[str] = []

        usb_raw = self._reader.status
        usb_present = bool(usb_raw.get("receiver_present"))
        usb_path = usb_raw.get("input_path")
        usb_link_up = bool(usb_raw.get("input_open"))
        usb_link_ready = bool(
            usb_raw.get("input_open")
            and usb_raw.get("reader_running")
            and usb_raw.get("grabbed")
            and usb_raw.get("paired_remote")
        )

        usb_reasons: list[str] = []
        if not usb_present:
            usb_reasons.append("usb.receiver_not_detected")
        if not bool(usb_raw.get("paired_remote")):
            usb_reasons.append("usb.no_paired_remote")
        if not bool(usb_raw.get("reader_running")):
            usb_reasons.append("usb.reader_not_running")
        if not bool(usb_raw.get("input_open")):
            usb_reasons.append("usb.input_not_open")
        if not bool(usb_raw.get("grabbed")):
            usb_reasons.append("usb.not_grabbed")

        usb_error = bool(usb_raw.get("error")) if "error" in usb_raw else (not usb_link_up)

        usb_state = {
            "status": self._domain_status(enabled=True, degraded=bool(usb_reasons)),
            "reasons": usb_reasons,
            "present": usb_present,
            "path": usb_path,
            "link_up": usb_link_up,
            "link_ready": usb_link_ready,
            "error": usb_error,
            "details": {
                "paired_remote": bool(usb_raw.get("paired_remote")),
                "reader_running": bool(usb_raw.get("reader_running")),
                "input_open": bool(usb_raw.get("input_open")),
                "grabbed": bool(usb_raw.get("grabbed")),
            },
        }
        degraded_reasons.extend(usb_reasons)

        ble_raw = self._ble.status
        conn_params = ble_raw.get("conn_params") or {}

        ble_present = bool(ble_raw.get("adapter_present"))
        ble_path = ble_raw.get("active_port") or ble_raw.get("device")
        ble_connected = bool(ble_raw.get("connected"))
        ble_advertising = bool(ble_raw.get("advertising"))
        ble_link_ready = bool(ble_raw.get("ready"))
        ble_transport_up = ble_present
        ble_link_up = ble_connected

        ble_reasons: list[str] = []
        if not ble_present:
            ble_reasons.append("ble.adapter_missing")
        elif ble_link_ready:
            pass
        elif ble_connected:
            ble_reasons.append("ble.connected_not_ready")
        elif ble_advertising:
            ble_reasons.append("ble.advertising")
        else:
            ble_reasons.append("ble.idle")

        ble_state = {
            "status": self._domain_status(enabled=True, degraded=bool(ble_reasons)),
            "reasons": ble_reasons,
            "present": ble_present,
            "path": ble_path,
            "link_up": ble_link_up,
            "link_ready": ble_link_ready,
            "error": bool(ble_raw.get("error")),
            "details": {
                "transport_up": ble_transport_up,
                "advertising": ble_advertising,
                "connected": ble_connected,
                "last_disc_reason": ble_raw.get("last_disc_reason"),
                "conn_params": conn_params or None,
            },
        }

        degraded_reasons.extend(ble_reasons)

        if self._tv is None:
            tv_state = {
                "status": "disabled",
                "reasons": [],
                "present": False,
                "link_up": False,
                "link_ready": False,
                "error": False,
                "details": {},
            }
        else:
            s = self._tv.snapshot()
            tv_enabled = bool(s.token_present)
            tv_link_up = bool(s.presence_on)
            tv_link_ready = bool(s.ws_connected and s.presence_on)
            tv_error = bool(s.last_error)

            tv_reasons: list[str] = []
            if not s.token_present:
                tv_reasons.append("tv.token_missing")
            if tv_error:
                tv_reasons.append("tv.error")

            tv_state = {
                "status": self._domain_status(enabled=tv_enabled, degraded=bool(tv_reasons)),
                "reasons": tv_reasons,
                "present": tv_enabled,
                "link_up": tv_link_up,
                "link_ready": tv_link_ready,
                "error": tv_error,
                "details": {
                    "initialized": bool(s.initialized),
                    "presence_on": s.presence_on,
                    "presence_source": s.presence_source,
                    "last_change_age_s": s.last_change_age_s,
                    "ws_connected": bool(s.ws_connected),
                    "token_present": bool(s.token_present),
                    "last_error": s.last_error,
                },
            }
        degraded_reasons.extend(tv_state["reasons"])

        if self._speaker is None or not getattr(self._speaker, "enabled", False):
            speaker_state = {
                "status": "disabled",
                "reasons": [],
                "present": False,
                "link_up": False,
                "link_ready": False,
                "error": False,
                "details": {},
            }
        else:
            speaker_enabled = True
            snap = self._speaker.snapshot()
            sstate = getattr(self._speaker, "state", None)
            reachable = bool(getattr(sstate, "reachable", False))
            connected = bool(getattr(sstate, "connected", False))
            ready = bool(getattr(sstate, "ready", False))

            sp_link_up = connected
            sp_link_ready = ready
            sp_error = bool(getattr(sstate, "last_error", None))

            sp_reasons: list[str] = []
            if not reachable:
                sp_reasons.append("speaker.not_reachable")
            if reachable and not connected:
                sp_reasons.append("speaker.not_connected")
            if connected and not ready:
                sp_reasons.append("speaker.not_ready")

            speaker_state = {
                "status": self._domain_status(enabled=speaker_enabled, degraded=bool(sp_reasons)),
                "reasons": sp_reasons,
                "present": speaker_enabled,
                "link_up": sp_link_up,
                "link_ready": sp_link_ready,
                "error": sp_error,
                "details": snap,
            }
        degraded_reasons.extend(speaker_state["reasons"])

        status = "ok" if not degraded_reasons else "degraded"
        domains = {
            "usb": usb_state["status"],
            "ble": ble_state["status"],
            "tv": tv_state["status"],
            "speaker": speaker_state["status"],
        }

        return {
            "pihub_id": pihub_id,
            "status": status,
            "degraded_reasons": degraded_reasons,
            "domains": domains,
            "runtime": runtime_state,
            "usb": usb_state,
            "ble": ble_state,
            "tv": tv_state,
            "speaker": speaker_state,
        }
