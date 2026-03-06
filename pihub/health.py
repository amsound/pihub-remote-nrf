"""Lightweight HTTP health/status endpoint for external monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Optional

from aiohttp import web

from .ha_ws import HAWS
from .input_ble_dongle import BleDongleLink
from .input_unifying import UnifyingReader
from .speaker_linkplay import LinkPlaySpeaker
from .tv import TvController


class HealthServer:
    """Expose a simple JSON health snapshot for scrapers and system logic."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ws: HAWS,
        bt: BleDongleLink,
        reader: UnifyingReader,
        tv: Optional[TvController] = None,
        speaker: Optional[LinkPlaySpeaker] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._ws = ws
        self._bt = bt
        self._reader = reader
        self._tv = tv
        self._speaker = speaker

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        if self._runner is not None:
            return

        app = web.Application()
        app.add_routes([web.get("/health", self._handle_health)])

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

    @staticmethod
    def _domain_status(*, enabled: bool, degraded: bool) -> str:
        if not enabled:
            return "disabled"
        return "degraded" if degraded else "ok"

    def snapshot(self) -> dict:
        pihub_id = socket.gethostname()

        degraded_reasons: list[str] = []

        # ---------------- Home Assistant (ws) ----------------
        ha_connected = bool(self._ws.is_connected)
        ha_reasons: list[str] = []
        if not ha_connected:
            ha_reasons.append("ha.not_connected")
        ha_state = {
            "status": self._domain_status(enabled=True, degraded=bool(ha_reasons)),
            "reasons": ha_reasons,
            "present": True,
            "link_up": ha_connected,
            "link_ready": ha_connected,
            "error": not ha_connected,
            "details": {
                "connected": ha_connected,
                "last_activity": self._ws.last_activity,
            },
        }
        degraded_reasons.extend(ha_reasons)

        # ---------------- USB ----------------
        usb_raw = self._reader.status

        usb_present = bool(usb_raw.get("receiver_present"))
        usb_path = usb_raw.get("input_path")
        usb_link_up = bool(usb_raw.get("input_open"))

        # For USB, there isn't a firmware-computed "ready" signal, so we provide a
        # practical "link_ready" derived from explicit flags.
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

        # ---------------- BLE ----------------
        ble_raw = self._bt.status
        conn_params = ble_raw.get("conn_params") or {}

        ble_present = bool(ble_raw.get("adapter_present"))
        ble_path = ble_raw.get("active_port") or ble_raw.get("device")
        ble_connected = bool(ble_raw.get("connected"))
        ble_advertising = bool(ble_raw.get("advertising"))
        ble_link_ready = bool(ble_raw.get("ready"))  # firmware READY

        ble_reasons: list[str] = []
        if not ble_present:
            ble_reasons.append("ble.adapter_missing")
        else:
            if ble_link_ready:
                pass
            elif ble_advertising:
                ble_reasons.append("ble.advertising")
            else:
                ble_reasons.append("ble.not_ready")

        ble_state = {
            "status": self._domain_status(enabled=True, degraded=bool(ble_reasons)),
            "reasons": ble_reasons,
            "present": ble_present,
            "path": ble_path,
            "link_up": ble_present,          # serial open == link up for CDC ACM
            "link_ready": ble_link_ready,    # firmware READY
            "error": bool(ble_raw.get("error")),
            "details": {
                "advertising": ble_advertising,
                "connected": ble_connected,
                "last_disc_reason": ble_raw.get("last_disc_reason"),
                "conn_params": conn_params or None,
            },
        }
        degraded_reasons.extend(ble_reasons)

        # ---------------- TV -----------------
        if self._tv is None:
            tv_enabled = False
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
            tv_enabled = bool(s.token_present)  # token is a must for configured/usable TV
            tv_link_up = bool(s.logical_on)
            tv_link_ready = bool(s.ws_connected and s.logical_on)
            tv_error = bool(s.last_error)

            tv_reasons: list[str] = []
            if not s.token_present:
                tv_reasons.append("tv.token_missing")
            if tv_enabled and not s.logical_on:
                tv_reasons.append("tv.logical_off")
            if tv_enabled and s.logical_on and not s.ws_connected:
                tv_reasons.append("tv.ws_not_connected")
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
                    "logical_on": bool(s.logical_on),
                    "logical_source": s.logical_source,
                    "last_change_age_s": s.last_change_age_s,
                    "ws_connected": bool(s.ws_connected),
                    "token_present": bool(s.token_present),
                    "last_error": s.last_error,
                },
            }
        degraded_reasons.extend(tv_state["reasons"])

        # ---------------- Speaker -------------
        if self._speaker is None or not getattr(self._speaker, "enabled", False):
            speaker_enabled = False
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

            # Link flags come from the driver's state (details no longer repeat reachable/subscribed)
            sstate = getattr(self._speaker, "state", None)
            subscribed = bool(getattr(sstate, "subscribed", False))
            reachable = bool(getattr(sstate, "reachable", False))
            connected = bool(getattr(sstate, "connected", False))

            sp_link_up = subscribed
            sp_link_ready = connected
            sp_error = speaker_enabled and (not reachable)

            sp_reasons: list[str] = []
            if not subscribed:
                sp_reasons.append("speaker.not_subscribed")
            if subscribed and not reachable:
                sp_reasons.append("speaker.not_reachable")
            if subscribed and reachable and not connected:
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

        # Overall status: OK only when no degraded reasons.
        status = "ok" if not degraded_reasons else "degraded"

        # Lightweight domain summary for dashboards/scrapers.
        domains = {
            "ha": ha_state["status"],
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
            "ha": ha_state,
            "usb": usb_state,
            "ble": ble_state,
            "tv": tv_state,
            "speaker": speaker_state,
        }
