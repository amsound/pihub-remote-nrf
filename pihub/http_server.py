"""HTTP control plane: JSON API plus the web UI (static files in pihub/web/)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from .ble_dongle import BleDongleLink
from .history import HistoryStore
from .runtime import RuntimeEngine
from .speaker import SpeakerLike
from .status import StatusReporter
from .unifying_reader import UnifyingReader

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
PAGES = {"/status": "status.html", "/remote": "remote.html", "/settings": "settings.html", "/history": "history.html"}

# A key pressed from the web page is released automatically after this long if
# its "up" never arrives (phone locked, network drop), so nothing can stick.
WEB_KEY_MAX_HOLD_S = 8.0


class HttpServer:
    """Expose the JSON API and serve the web UI."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ble: BleDongleLink,
        reader: UnifyingReader,
        tv: Any = None,
        speaker: Optional[SpeakerLike] = None,
        settings: Any = None,
        runtime: Optional[RuntimeEngine] = None,
        history: HistoryStore | None = None,
        speaker_backend: str | None = None,
        dispatcher: Any = None,
        room_name: str = "",
        rooms: list[tuple[str, str]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._tv = tv
        self._speaker = speaker
        self._settings = settings
        self._runtime = runtime
        self._history = history
        self._speaker_backend = str(speaker_backend or "").strip().lower()
        self._dispatcher = dispatcher
        self._status = StatusReporter(
            ble=ble,
            reader=reader,
            tv=tv,
            speaker=speaker,
            runtime=runtime,
            dispatcher=dispatcher,
            room_name=room_name,
            rooms=rooms,
            http_port=port,
        )

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._web_key_release: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        if self._runner is not None:
            return

        app = web.Application()
        app.add_routes(
            [
                # Pages
                web.get("/", self._redirect_to("/status")),
                *[web.get(path, self._page(name)) for path, name in PAGES.items()],
                web.get("/dashboard", self._redirect_to("/status")),
                web.get("/tools", self._redirect_to("/status")),
                web.static("/web", WEB_DIR),

                # JSON API: everything a program calls lives under /api/
                web.get("/api/status", self._handle_api_status),

                web.get("/api/history/events", self._handle_history_events),
                web.get("/api/history/flows", self._handle_history_flows),
                web.post("/api/history/clear", self._handle_history_clear),

                web.get("/api/settings", self._handle_settings_get),
                web.post("/api/settings", self._handle_settings_save),

                web.post("/api/flow/{name}", self._handle_flow_run),
                web.post("/api/mode/{name}", self._handle_mode_set),
                web.post("/api/command", self._handle_command),
                web.post("/api/key/edge", self._handle_remote_edge),
                web.post("/api/key/tap", self._handle_remote_tap),
                web.post("/api/refresh/tv", self._handle_refresh_tv),
                web.post("/api/refresh/speaker", self._handle_refresh_speaker),
                web.post("/api/restart", self._handle_restart),
            ]
        )

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        for task in self._web_key_release.values():
            task.cancel()
        self._web_key_release.clear()

        runner, self._runner = self._runner, None
        self._site = None
        if runner is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner.cleanup()

    # ---- pages ----

    @staticmethod
    def _redirect_to(location: str):
        async def handler(_: web.Request) -> web.Response:
            raise web.HTTPFound(location=location)

        return handler

    @staticmethod
    def _page(name: str):
        path = WEB_DIR / name

        async def handler(_: web.Request) -> web.StreamResponse:
            return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

        return handler

    # ---- status ----

    async def _handle_api_status(self, _: web.Request) -> web.Response:
        # Other rooms' Status pages read this too (the room strip).
        return web.json_response(
            self._status.api_status(),
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # ---- history ----

    async def _handle_history_events(self, request: web.Request) -> web.Response:
        limit = _int_query(request, "limit", 50)
        events = self._history.list_events(limit=limit) if self._history is not None else []
        return web.json_response({"ok": True, "events": events})

    async def _handle_history_flows(self, request: web.Request) -> web.Response:
        limit = _int_query(request, "limit", 20)
        flows = self._history.list_flow_reports(limit=limit) if self._history is not None else []
        return web.json_response({"ok": True, "flows": flows})

    async def _handle_history_clear(self, _: web.Request) -> web.Response:
        if self._history is not None:
            self._history.clear()
        return web.json_response({"ok": True})

    # ---- settings ----

    async def _handle_settings_get(self, _: web.Request) -> web.Response:
        if self._settings is None:
            return web.json_response({"ok": False, "error": "settings unavailable"}, status=503)
        return web.json_response(
            {"ok": True, "backend": self._speaker_backend, "settings": self._settings.snapshot()}
        )

    async def _handle_settings_save(self, request: web.Request) -> web.Response:
        if self._settings is None:
            return web.json_response({"ok": False, "error": "settings unavailable"}, status=503)

        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "json body required"}, status=400)

        try:
            saved = self._settings.save_from_payload(payload, speaker_backend=self._speaker_backend)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "settings": saved})

    # ---- control ----

    async def _handle_flow_run(self, request: web.Request) -> web.Response:
        if self._runtime is None:
            return web.json_response({"ok": False, "error": "runtime unavailable"}, status=503)

        name = (request.match_info.get("name") or "").strip()
        payload = await _json_body(request) or {}
        trigger = str(payload.get("trigger") or "http.flow")
        result = await self._runtime.run_flow(name, trigger=trigger)
        status = 200 if result.get("ok") else 409 if result.get("reason") == "runner_busy" else 400
        return web.json_response(result, status=status)

    async def _handle_mode_set(self, request: web.Request) -> web.Response:
        if self._runtime is None:
            return web.json_response({"ok": False, "error": "runtime unavailable"}, status=503)

        name = (request.match_info.get("name") or "").strip()
        payload = await _json_body(request) or {}
        trigger = str(payload.get("trigger") or "http.mode")
        result = await self._runtime.set_mode(name, trigger=trigger)
        return web.json_response(result, status=200 if result.get("ok") else 400)

    async def _handle_command(self, request: web.Request) -> web.Response:
        if self._runtime is None:
            return web.json_response({"ok": False, "error": "runtime unavailable"}, status=503)

        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "json body required"}, status=400)

        result = await self._runtime.on_cmd(payload)
        status = 200 if result.get("ok") else 409 if result.get("reason") == "runner_busy" else 400
        return web.json_response(result, status=status)

    async def _handle_remote_edge(self, request: web.Request) -> web.Response:
        if self._dispatcher is None:
            return web.json_response({"ok": False, "error": "dispatcher unavailable"}, status=503)

        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "json body required"}, status=400)

        key = str(payload.get("key") or "").strip()
        edge = str(payload.get("edge") or "").strip().lower()
        key_error = self._validate_remote_key(key)
        if key_error:
            return web.json_response({"ok": False, "error": key_error}, status=400)
        if edge not in {"down", "up"}:
            return web.json_response({"ok": False, "error": "edge must be 'down' or 'up'"}, status=400)

        pending = self._web_key_release.pop(key, None)
        if pending is not None:
            pending.cancel()

        try:
            await self._dispatcher.on_usb_edge(key, edge)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc), "key": key, "edge": edge}, status=500)

        if edge == "down":
            self._web_key_release[key] = asyncio.create_task(
                self._release_web_key_later(key), name=f"web_key_release:{key}"
            )

        return web.json_response({"ok": True, "action": "remote_edge", "key": key, "edge": edge})

    async def _release_web_key_later(self, key: str) -> None:
        await asyncio.sleep(WEB_KEY_MAX_HOLD_S)
        if self._web_key_release.get(key) is asyncio.current_task():
            del self._web_key_release[key]
            logger.info("web key %s released automatically (no key-up received)", key)
            with contextlib.suppress(Exception):
                await self._dispatcher.on_usb_edge(key, "up")

    async def _handle_remote_tap(self, request: web.Request) -> web.Response:
        if self._dispatcher is None:
            return web.json_response({"ok": False, "error": "dispatcher unavailable"}, status=503)

        payload = await _json_body(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "json body required"}, status=400)

        key = str(payload.get("key") or "").strip()
        key_error = self._validate_remote_key(key)
        if key_error:
            return web.json_response({"ok": False, "error": key_error}, status=400)

        try:
            hold_ms = max(20, min(int(payload.get("hold_ms", 80)), 2000))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "hold_ms must be an integer"}, status=400)

        try:
            await self._dispatcher.on_usb_edge(key, "down")
            try:
                await asyncio.sleep(hold_ms / 1000.0)
            finally:
                await self._dispatcher.on_usb_edge(key, "up")
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc), "key": key}, status=500)

        return web.json_response({"ok": True, "action": "remote_tap", "key": key, "hold_ms": hold_ms})

    def _validate_remote_key(self, key: str) -> str | None:
        if not key:
            return "key required"
        if not key.startswith("rem_"):
            return "key must start with rem_"
        known = set(self._dispatcher.scancode_map.values())
        if key not in known:
            return f"unknown remote key: {key}"
        return None

    async def _handle_refresh_tv(self, _: web.Request) -> web.Response:
        if self._tv is None:
            return web.json_response(
                {"ok": False, "domain": "tv", "action": "refresh", "error": "tv unavailable", "outcome": "unavailable"},
                status=503,
            )
        try:
            result = await self._tv.reconcile_presence()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "domain": "tv", "action": "refresh", "error": str(exc), "outcome": "error"}
            )
        outcome = str(result.get("outcome") or "unknown")
        return web.json_response(
            {"ok": outcome in {"present_true", "present_false"}, "domain": "tv", "action": "refresh", **result}
        )

    async def _handle_refresh_speaker(self, _: web.Request) -> web.Response:
        if self._speaker is None or not self._speaker.enabled:
            return web.json_response(
                {"ok": False, "domain": "speaker", "action": "refresh", "outcome": "unavailable", "error": "speaker unavailable"},
                status=503,
            )
        try:
            result = await self._speaker.request_refresh()
        except Exception as exc:
            result = {"ok": False, "outcome": "error", "error": str(exc)}
        return web.json_response({"domain": "speaker", "action": "refresh", **result})

    async def _handle_restart(self, _: web.Request) -> web.Response:
        async def _delayed_exit() -> None:
            await asyncio.sleep(0.25)
            os._exit(0)  # Docker's restart policy brings the container back

        asyncio.create_task(_delayed_exit(), name="pihub_restart")
        return web.json_response({"ok": True, "action": "restart"})


def _int_query(request: web.Request, name: str, default: int) -> int:
    try:
        return int(request.query.get(name, default))
    except (TypeError, ValueError):
        return default


async def _json_body(request: web.Request) -> dict | None:
    """The request's JSON object; {} for an empty body, None if it isn't a JSON object."""
    if request.content_length in (None, 0):
        return {}
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None
