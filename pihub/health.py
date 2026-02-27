"""Lightweight HTTP health/status endpoint for external monitoring."""

from __future__ import annotations

import asyncio
import contextlib
from aiohttp import web
from typing import Optional
from .tv import TvController

from .ha_ws import HAWS
from .input_ble_dongle import BleDongleLink
from .input_unifying import UnifyingReader


class HealthServer:
    """Expose a simple JSON health snapshot for Home Assistant or probes."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ws: HAWS,
        bt: BleDongleLink,
        reader: UnifyingReader,
        tv: Optional[TvController] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._ws = ws
        self._bt = bt
        self._reader = reader

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        self._tv = tv

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

    def snapshot(self) -> dict:
        ws_connected = self._ws.is_connected
        ws_state = {"connected": ws_connected, "last_activity": self._ws.last_activity}

        # ---------------- USB ----------------
        usb_raw = self._reader.status

        usb_present = bool(usb_raw.get("receiver_present"))
        usb_path = usb_raw.get("input_path")
        usb_link_up = bool(usb_raw.get("input_open"))

        # For USB, there isn't a firmware-computed "ready" signal, so we provide a
        # practical "link_ready" derived from your existing explicit flags.
        usb_link_ready = bool(
            usb_raw.get("input_open")
            and usb_raw.get("reader_running")
            and usb_raw.get("grabbed")
            and usb_raw.get("paired_remote")
        )

        usb_state = {
            # canonical
            "present": usb_present,
            "path": usb_path,
            "link_up": usb_link_up,
            "link_ready": usb_link_ready,
            "error": bool(usb_raw.get("error")) if "error" in usb_raw else (not usb_link_up),

            # usb-specific passthrough (keep what you already use)
            "receiver_present": usb_present,
            "paired_remote": bool(usb_raw.get("paired_remote")),
            "reader_running": bool(usb_raw.get("reader_running")),
            "input_open": bool(usb_raw.get("input_open")),
            "input_path": usb_path,
            "grabbed": bool(usb_raw.get("grabbed")),
        }

        # ---------------- TV -----------------
        tv_state = None
        if self._tv is not None:
            s = self._tv.snapshot()
            tv_state = {
                "dmr_up": s.dmr_up,
                "ws_connected": s.ws_connected,
                "token_present": s.token_present,
                "last_error": s.last_error,
            }

        # ---------------- BLE ----------------
        ble_raw = self._bt.status
        conn_params = ble_raw.get("conn_params") or {}

        ble_present = bool(ble_raw.get("adapter_present"))
        ble_path = ble_raw.get("active_port") or ble_raw.get("device")
        ble_connected = bool(ble_raw.get("connected"))
        ble_advertising = bool(ble_raw.get("advertising"))

        # Firmware-computed READY: expose as link_ready (no extra semantics).
        ble_link_ready = bool(ble_raw.get("ready"))

        ble_state = {
            # canonical
            "present": ble_present,
            "path": ble_path,
            "link_up": ble_present,          # serial open == link up for CDC ACM
            "link_ready": ble_link_ready,    # firmware READY
            "error": bool(ble_raw.get("error")),

            # ble-specific passthrough (clean set)
            "advertising": ble_advertising,
            "connected": ble_connected,
            "conn_params": conn_params or None,
            "last_disc_reason": ble_raw.get("last_disc_reason"),
        }

        degraded_reasons = []

        if not ws_state["connected"]:
            degraded_reasons.append("ws.not_connected")

        # USB degraded reasons (keep your strict checks)
        if not usb_state["receiver_present"]:
            degraded_reasons.append("usb.receiver_not_detected")
        if not usb_state["paired_remote"]:
            degraded_reasons.append("usb.no_paired_remote")
        if not usb_state["reader_running"]:
            degraded_reasons.append("usb.reader_not_running")
        if not usb_state["input_open"]:
            degraded_reasons.append("usb.input_not_open")
        if not usb_state["grabbed"]:
            degraded_reasons.append("usb.not_grabbed")

        # BLE health (strict): OK only when link_ready (firmware READY).
        if not ble_state["present"]:
            degraded_reasons.append("ble.adapter_missing")
        else:
            if ble_state["link_ready"]:
                pass
            elif ble_state["advertising"]:
                degraded_reasons.append("ble.advertising")
            else:
                degraded_reasons.append("ble.not_ready")

        return {
            "status": "ok" if not degraded_reasons else "degraded",
            "degraded_reasons": degraded_reasons,
            "ws": ws_state,
            "usb": usb_state,
            "tv": tv_state,
            "ble": ble_state,
        }