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

def _norm_error(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None

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
    def _domain_status(*, configured: bool, enabled: bool, degraded: bool) -> str:
        if not configured or not enabled:
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

        # ---------------- USB ----------------
        if self._reader is None:
            usb_state = {
                "status": "disabled",
                "configured": False,
                "enabled": False,
                "reasons": [],
                "present": False,
                "path": None,
                "link_up": False,
                "link_ready": False,
                "error": False,
                "last_error": None,
                "details": {},
            }
        else:
            usb_raw = self._reader.status
            usb_configured = True
            usb_enabled = True
            usb_present = bool(usb_raw.get("receiver_present"))
            usb_path = usb_raw.get("input_path")
            usb_link_up = bool(usb_raw.get("input_open"))
            usb_link_ready = bool(
                usb_raw.get("input_open")
                and usb_raw.get("reader_running")
                and usb_raw.get("grabbed")
                and usb_raw.get("paired_remote")
            )

            usb_last_error = _norm_error(usb_raw.get("last_error"))
            usb_error = bool(usb_raw.get("error")) if "error" in usb_raw else bool(usb_last_error)

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
            if usb_error:
                usb_reasons.append("usb.error")

            usb_state = {
                "status": self._domain_status(
                    configured=usb_configured,
                    enabled=usb_enabled,
                    degraded=bool(usb_reasons),
                ),
                "configured": usb_configured,
                "enabled": usb_enabled,
                "reasons": usb_reasons,
                "present": usb_present,
                "path": usb_path,
                "link_up": usb_link_up,
                "link_ready": usb_link_ready,
                "error": usb_error,
                "last_error": usb_last_error,
                "details": {
                    "paired_remote": bool(usb_raw.get("paired_remote")),
                    "reader_running": bool(usb_raw.get("reader_running")),
                    "input_open": bool(usb_raw.get("input_open")),
                    "grabbed": bool(usb_raw.get("grabbed")),
                },
            }

        if usb_state["status"] == "degraded":
            degraded_reasons.extend(usb_state["reasons"])

        # ---------------- BLE ----------------
        if self._ble is None:
            ble_state = {
                "status": "disabled",
                "configured": False,
                "enabled": False,
                "reasons": [],
                "present": False,
                "path": None,
                "link_up": False,
                "link_ready": False,
                "error": False,
                "last_error": None,
                "details": {},
            }
        else:
            ble_raw = self._ble.status
            conn_params = ble_raw.get("conn_params") or {}

            ble_configured = True
            ble_enabled = True
            ble_present = bool(ble_raw.get("adapter_present"))
            ble_path = ble_raw.get("active_port")
            ble_transport_up = bool(ble_raw.get("transport_open"))
            ble_connected = bool(ble_raw.get("connected"))
            ble_advertising = bool(ble_raw.get("advertising"))
            ble_link_ready = bool(ble_raw.get("ready"))
            ble_link_up = bool(ble_transport_up or ble_connected or ble_link_ready)
            ble_last_error = _norm_error(ble_raw.get("last_error"))
            ble_error = bool(ble_raw.get("error")) or bool(ble_last_error)

            ble_reasons: list[str] = []
            if not ble_present:
                ble_reasons.append("ble.adapter_missing")
            elif ble_link_ready:
                pass
            elif not ble_transport_up:
                ble_reasons.append("ble.transport_down")
            elif ble_connected:
                ble_reasons.append("ble.connected_not_ready")
            elif ble_advertising:
                ble_reasons.append("ble.advertising")
            else:
                ble_reasons.append("ble.idle")
            if ble_error:
                ble_reasons.append("ble.error")

            ble_state = {
                "status": self._domain_status(
                    configured=ble_configured,
                    enabled=ble_enabled,
                    degraded=bool(ble_reasons),
                ),
                "configured": ble_configured,
                "enabled": ble_enabled,
                "reasons": ble_reasons,
                "present": ble_present,
                "path": ble_path,
                "link_up": ble_link_up,
                "link_ready": ble_link_ready,
                "error": ble_error,
                "last_error": ble_last_error,
                "details": {
                    "transport_open": ble_transport_up,
                    "advertising": ble_advertising,
                    "connected": ble_connected,
                    "proto_report": ble_raw.get("proto_report"),
                    "last_disc_reason": ble_raw.get("last_disc_reason"),
                    "conn_params": conn_params or None,
                },
            }

        if ble_state["status"] == "degraded":
            degraded_reasons.extend(ble_state["reasons"])

        # ---------------- TV ----------------
        if self._tv is None:
            tv_state = {
                "status": "disabled",
                "configured": False,
                "enabled": False,
                "reasons": [],
                "present": False,
                "link_up": False,
                "link_ready": False,
                "error": False,
                "last_error": None,
                "details": {},
            }
        else:
            s = self._tv.snapshot()

            tv_configured = True
            tv_enabled = True
            tv_present = s.presence_on is not None
            tv_link_up = bool(s.presence_on is True)
            tv_link_ready = bool(s.ws_connected and s.presence_on is True)
            tv_last_error = _norm_error(s.last_error)
            tv_error = bool(tv_last_error)

            tv_reasons: list[str] = []
            if not bool(s.token_present):
                tv_reasons.append("tv.token_missing")
            if not tv_present:
                tv_reasons.append("tv.presence_unknown")
            elif not tv_link_up:
                tv_reasons.append("tv.off")
            elif not tv_link_ready:
                tv_reasons.append("tv.ws_not_ready")
            if tv_error:
                tv_reasons.append("tv.error")

            tv_state = {
                "status": self._domain_status(
                    configured=tv_configured,
                    enabled=tv_enabled,
                    degraded=bool(tv_reasons),
                ),
                "configured": tv_configured,
                "enabled": tv_enabled,
                "reasons": tv_reasons,
                "present": tv_present,
                "link_up": tv_link_up,
                "link_ready": tv_link_ready,
                "error": tv_error,
                "last_error": tv_last_error,
                "details": {
                    "initialized": bool(s.initialized),
                    "presence_on": s.presence_on,
                    "presence_source": s.presence_source,
                    "last_change_age_s": s.last_change_age_s,
                    "ws_connected": bool(s.ws_connected),
                    "token_present": bool(s.token_present),
                },
            }

        if tv_state["status"] == "degraded":
            degraded_reasons.extend(
                reason for reason in tv_state["reasons"]
                if reason != "tv.presence_unknown"
            )

        # ---------------- Speaker ----------------
        if self._speaker is None or not getattr(self._speaker, "enabled", False):
            speaker_state = {
                "status": "disabled",
                "configured": bool(self._speaker is not None),
                "enabled": False,
                "reasons": [],
                "present": False,
                "link_up": False,
                "link_ready": False,
                "error": False,
                "last_error": None,
                "details": {},
            }
        else:
            snap = self._speaker.snapshot()
            sstate = getattr(self._speaker, "state", None)

            reachable = bool(getattr(sstate, "reachable", False))
            connected = bool(getattr(sstate, "connected", False))
            ready = bool(getattr(sstate, "ready", False))
            speaker_last_error = _norm_error(getattr(sstate, "last_error", None))
            speaker_error = bool(speaker_last_error)

            speaker_configured = True
            speaker_enabled = True
            sp_present = reachable
            sp_link_up = connected
            sp_link_ready = ready

            sp_reasons: list[str] = []
            if not reachable:
                sp_reasons.append("speaker.not_reachable")
            elif not connected:
                sp_reasons.append("speaker.not_connected")
            elif not ready:
                sp_reasons.append("speaker.not_ready")
            if speaker_error:
                sp_reasons.append("speaker.error")

            speaker_state = {
                "status": self._domain_status(
                    configured=speaker_configured,
                    enabled=speaker_enabled,
                    degraded=bool(sp_reasons),
                ),
                "configured": speaker_configured,
                "enabled": speaker_enabled,
                "reasons": sp_reasons,
                "present": sp_present,
                "link_up": sp_link_up,
                "link_ready": sp_link_ready,
                "error": speaker_error,
                "last_error": speaker_last_error,
                "details": dict(snap),
            }

        if speaker_state["status"] == "degraded":
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