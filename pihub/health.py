"""Lightweight HTTP health/status endpoint for external monitoring."""

from __future__ import annotations

import asyncio
import contextlib
from aiohttp import web
from typing import Optional

from .ha_ws import HAWS
from .bt_le.controller import BTLEController
from .input_unifying import UnifyingReader


class HealthServer:
    """Expose a simple JSON health snapshot for Home Assistant or probes."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ws: HAWS,
        bt: BTLEController,
        reader: UnifyingReader,
    ) -> None:
        self._host = host
        self._port = port
        self._ws = ws
        self._bt = bt
        self._reader = reader

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

    def snapshot(self) -> dict:
        ws_connected = self._ws.is_connected
        ws_state = {"connected": ws_connected, "last_activity": self._ws.last_activity}

        usb_state = self._reader.status

        ble_raw = self._bt.status
        conn_params = ble_raw.get("conn_params") or {}

        ble_state = {
            "serial_open": bool(ble_raw.get("adapter_present")),
            "port": ble_raw.get("active_port"),
            "ready": bool(ble_raw.get("ready")),
            "advertising": bool(ble_raw.get("advertising")),
            "connected": bool(ble_raw.get("connected")),
            "sec": ble_raw.get("sec"),
            "proto_boot": bool(ble_raw.get("proto_boot")),
            "error": bool(ble_raw.get("error")),
            "conn_params": conn_params or None,
            "phy": ble_raw.get("phy"),
            "last_disc_reason": ble_raw.get("last_disc_reason"),
        }

        degraded_reasons = []

        if not ws_state["connected"]:
            degraded_reasons.append("ws.not_connected")

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

        if not ble_state["serial_open"]:
            degraded_reasons.append("ble.adapter_missing")

        # Consider BLE "usable" if connected OR advertising (ready to connect).
        if not ble_state["connected"] and not ble_state["advertising"]:
            degraded_reasons.append("ble.not_advertising")

        if not ble_state["connected"]:
            degraded_reasons.append("ble.not_connected")

        return {
            "status": "ok" if not degraded_reasons else "degraded",
            "degraded_reasons": degraded_reasons,
            "ws": ws_state,
            "usb": usb_state,
            "ble": ble_state,
        }