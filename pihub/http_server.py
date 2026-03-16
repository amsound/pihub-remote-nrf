"""Lightweight HTTP status and control endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import os
import shutil
import time
import json
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

class HttpServer:
    """Expose a small HTTP control plane."""

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

        self._process_start_monotonic = time.monotonic()

    async def start(self) -> None:
        if self._runner is not None:
            return

        app = web.Application()
        app.add_routes(
            [
                web.get("/health", self._handle_health),
                web.get("/dashboard", self._handle_dashboard),
                web.get("/tools", self._handle_tools),

                web.post("/flow/run/{name}", self._handle_flow_run),
                web.post("/mode/set/{name}", self._handle_mode_set),
                web.post("/command", self._handle_command),

                web.post("/refresh/tv", self._handle_refresh_tv),
                web.post("/refresh/speaker", self._handle_refresh_speaker),
                web.post("/refresh/networked", self._handle_refresh_networked),

                web.post("/admin/restart", self._handle_restart),
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

    def _status_badge_html(self, status: str) -> str:
        safe = self._html_escape(status)
        cls = {
            "ok": "status-ok",
            "degraded": "status-degraded",
            "disabled": "status-disabled",
        }.get(status, "status-error")
        return f'<span class="status-badge {cls}">{safe}</span>'

    @staticmethod
    def _html_escape(value: object) -> str:
        import html
        return html.escape(str(value))

    def _nav_html(self, *, current: str, hostname: str) -> str:
        def link(label: str, href: str, key: str) -> str:
            cls = "nav-link active" if current == key else "nav-link"
            return f'<a class="{cls}" href="{href}">{label}</a>'

        return f"""
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">PiHub — {self._html_escape(hostname)}</div>
    <nav class="nav">
      {link("Dashboard", "/dashboard", "dashboard")}
      {link("Tools", "/tools", "tools")}
      {link("Raw Health", "/health", "health")}
    </nav>
  </div>
</header>
"""

    def _shared_dark_css(self) -> str:
        return """
:root {
  --bg: #0f1115;
  --panel: #171a21;
  --panel-2: #1d2330;
  --border: #2b3342;
  --text: #e8ecf3;
  --muted: #aab4c3;
  --link: #8ab4ff;
  --ok-bg: #123222;
  --ok-fg: #86efac;
  --deg-bg: #3b2d12;
  --deg-fg: #fcd34d;
  --dis-bg: #2a2f39;
  --dis-fg: #cbd5e1;
  --err-bg: #3b1717;
  --err-fg: #fca5a5;
  --accent: #60a5fa;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
}

a {
  color: var(--link);
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgba(15, 17, 21, 0.94);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--border);
}

.topbar-inner {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0.9rem 1rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}

.brand {
  font-size: 1.1rem;
  font-weight: 700;
}

.nav {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.nav-link {
  display: inline-block;
  padding: 0.5rem 0.8rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel);
  color: var(--text);
}
.nav-link.active {
  border-color: var(--accent);
  background: #162236;
}

.page {
  max-width: 1400px;
  margin: 0 auto;
  padding: 1rem;
}

.section {
  margin-bottom: 1rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 1rem;
}

.section h1, .section h2, .section h3 {
  margin-top: 0;
}

.grid {
  display: grid;
  gap: 1rem;
}

.grid.summary {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

.grid.domains {
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}

.grid.system {
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.card {
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 1rem;
  min-width: 0;
}

.card h3 {
  margin: 0 0 0.7rem 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.status-badge,
.small-badge {
  display: inline-block;
  padding: 0.22rem 0.6rem;
  border-radius: 999px;
  font-size: 0.86rem;
  font-weight: 600;
  border: 1px solid transparent;
}

.status-ok { background: var(--ok-bg); color: var(--ok-fg); border-color: #1d4d32; }
.status-degraded { background: var(--deg-bg); color: var(--deg-fg); border-color: #6b4f1d; }
.status-disabled { background: var(--dis-bg); color: var(--dis-fg); border-color: #465063; }
.status-error { background: var(--err-bg); color: var(--err-fg); border-color: #6c2525; }

.kv {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.35rem 0.75rem;
  font-size: 0.96rem;
}
.kv .k { color: var(--muted); }
.kv .v { text-align: right; word-break: break-word; }

.muted {
  color: var(--muted);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.chip {
  display: inline-block;
  padding: 0.35rem 0.7rem;
  border-radius: 999px;
  background: #242b38;
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 0.9rem;
}

.error-line {
  margin-top: 0.8rem;
  padding: 0.7rem 0.8rem;
  border-radius: 10px;
  background: #2a1818;
  border: 1px solid #5b2727;
  color: #fecaca;
  font-size: 0.92rem;
  word-break: break-word;
}

pre.json {
  margin: 0;
  background: #0b0e13;
  color: #e5e7eb;
  padding: 1rem;
  border-radius: 12px;
  overflow-x: auto;
  font-size: 0.92rem;
  line-height: 1.45;
  white-space: pre;
  border: 1px solid var(--border);
}

@media (max-width: 640px) {
  .page {
    padding: 0.75rem;
  }
  .section {
    padding: 0.85rem;
  }
  .kv {
    grid-template-columns: 1fr;
  }
  .kv .v {
    text-align: left;
  }
}
"""

    async def _handle_tools(self, request: web.Request) -> web.Response:
        import json

        host = request.host or "localhost"

        snapshot = self.snapshot()
        hostname = snapshot.get("pihub_id") or socket.gethostname()
        pretty_json = json.dumps(snapshot, indent=2)

        runtime = snapshot.get("runtime") or {}
        current_mode = str(runtime.get("mode") or "")
        current_flow = str(runtime.get("last_flow") or "")

        def active_class(value: str, current: str) -> str:
            return "active" if value == current else ""

        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PiHub Tools — {self._html_escape(hostname)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
{self._shared_dark_css()}

.row {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
}}

form {{
  margin: 0;
}}

button {{
  padding: 0.8rem 1rem;
  font-size: 1rem;
  cursor: pointer;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--text);
  min-width: 180px;
  transition: 0.15s ease;
}}

button:hover {{
  border-color: var(--accent);
  background: #1b2740;
}}

form.active button {{
  border: 2px solid var(--accent);
  background: #162236;
  font-weight: 700;
}}

.section-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 0.75rem;
  flex-wrap: wrap;
}}

.badge {{
  display: inline-block;
  padding: 0.3rem 0.7rem;
  border-radius: 999px;
  font-size: 0.9rem;
  background: #242b38;
  border: 1px solid var(--border);
  color: var(--text);
  white-space: nowrap;
}}

.tools-grid {{
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}}

.meta-grid {{
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}}

.meta-card {{
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 1rem;
}}

.meta-card h3 {{
  margin: 0 0 0.4rem 0;
  font-size: 1rem;
}}

.meta-value {{
  font-size: 1.05rem;
  font-weight: 600;
}}

pre.json {{
  margin: 0;
  background: #0b0e13;
  color: #e5e7eb;
  padding: 1rem;
  border-radius: 12px;
  overflow-x: auto;
  font-size: 0.92rem;
  line-height: 1.45;
  white-space: pre;
  border: 1px solid var(--border);
}}

.danger button {{
  border-color: #6c2525;
}}
.danger button:hover {{
  background: #2a1818;
}}

@media (max-width: 640px) {{
  button {{
    width: 100%;
    min-width: 0;
  }}
}}
  </style>
</head>
<body>
  {self._nav_html(current="tools", hostname=str(hostname))}

  <main class="page">
    <section class="section">
      <h1>Tools</h1>
      <p class="muted">Direct operator controls for flows, modes, networked-domain refresh, and restart.</p>

      <div class="meta-grid">
        <div class="meta-card">
          <h3>Current mode</h3>
          <div class="meta-value">{self._html_escape(current_mode or "none")}</div>
        </div>
        <div class="meta-card">
          <h3>Current flow</h3>
          <div class="meta-value">{self._html_escape(current_flow or "none")}</div>
        </div>
        <div class="meta-card">
          <h3>Last trigger</h3>
          <div class="meta-value">{self._html_escape(runtime.get("last_trigger") or "none")}</div>
        </div>
        <div class="meta-card">
          <h3>Runtime result</h3>
          <div class="meta-value">{self._html_escape(runtime.get("last_result") or "none")}</div>
        </div>
      </div>
    </section>

    <div class="tools-grid">
      <section class="section">
        <div class="section-header">
          <h2>Flows</h2>
          <span class="badge">Current flow: {self._html_escape(current_flow or "none")}</span>
        </div>
        <div class="row">
          <form method="post" action="/flow/run/watch" class="{active_class('watch', current_flow)}">
            <button type="submit">Run watch</button>
          </form>
          <form method="post" action="/flow/run/listen" class="{active_class('listen', current_flow)}">
            <button type="submit">Run listen</button>
          </form>
          <form method="post" action="/flow/run/power_off" class="{active_class('power_off', current_flow)}">
            <button type="submit">Run power_off</button>
          </form>
        </div>
      </section>

      <section class="section">
        <div class="section-header">
          <h2>Modes</h2>
          <span class="badge">Current mode: {self._html_escape(current_mode or "none")}</span>
        </div>
        <div class="row">
          <form method="post" action="/mode/set/watch" class="{active_class('watch', current_mode)}">
            <button type="submit">Set mode watch</button>
          </form>
          <form method="post" action="/mode/set/listen" class="{active_class('listen', current_mode)}">
            <button type="submit">Set mode listen</button>
          </form>
          <form method="post" action="/mode/set/power_off" class="{active_class('power_off', current_mode)}">
            <button type="submit">Set mode power_off</button>
          </form>
        </div>
      </section>

      <section class="section">
        <h2>Refresh</h2>
        <div class="row">
          <form method="post" action="/refresh/tv">
            <button type="submit">Refresh TV</button>
          </form>
          <form method="post" action="/refresh/speaker">
            <button type="submit">Refresh Speaker</button>
          </form>
          <form method="post" action="/refresh/networked">
            <button type="submit">Refresh Networked</button>
          </form>
        </div>
      </section>

      <section class="section danger">
        <h2>Admin</h2>
        <div class="row">
          <form method="post" action="/admin/restart">
            <button type="submit">Restart PiHub</button>
          </form>
        </div>
        <p class="muted">Exits the process and relies on the container restart policy to bring it back.</p>
      </section>
    </div>

    <section class="section">
      <h2>Health snapshot</h2>
      <p><a href="/health">Open raw /health</a></p>
      <pre class="json">{self._html_escape(pretty_json)}</pre>
    </section>
  </main>
</body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        import json

        snapshot = self.snapshot()
        hostname = snapshot.get("pihub_id") or socket.gethostname()
        runtime = snapshot.get("runtime") or {}
        system = snapshot.get("system") or {}

        def kv_row(key: str, value: object) -> str:
            return (
                f'<div class="k">{self._html_escape(key)}</div>'
                f'<div class="v">{self._html_escape(self._fmt_value(value))}</div>'
            )

        def chips(items: list[object]) -> str:
            if not items:
                return '<span class="muted">None</span>'
            return "".join(
                f'<span class="chip">{self._html_escape(item)}</span>'
                for item in items
            )

        def domain_card(title: str, data: dict, details_keys: list[str]) -> str:
            status = str(data.get("status") or "unknown")
            details = data.get("details") or {}
            rows = [
                kv_row("Configured", data.get("configured")),
                kv_row("Enabled", data.get("enabled")),
                kv_row("Present", data.get("present")),
                kv_row("Link up", data.get("link_up")),
                kv_row("Link ready", data.get("link_ready")),
            ]

            if data.get("path") is not None:
                rows.append(kv_row("Path", data.get("path")))

            for key in details_keys:
                if key in details:
                    rows.append(kv_row(key.replace("_", " ").title(), details.get(key)))

            error_html = ""
            if data.get("last_error"):
                error_html = (
                    f'<div class="error-line"><strong>Last error:</strong> '
                    f'{self._html_escape(data.get("last_error"))}</div>'
                )

            reasons_html = chips(data.get("reasons") or [])

            return f"""
<div class="card">
  <h3>
    <span>{self._html_escape(title)}</span>
    {self._status_badge_html(status)}
  </h3>
  <div class="kv">
    {''.join(rows)}
  </div>
  <div style="margin-top:0.85rem;">
    <div class="muted" style="margin-bottom:0.35rem;">Reasons</div>
    <div class="chips">{reasons_html}</div>
  </div>
  {error_html}
</div>
"""

        runtime_error_html = ""
        if runtime.get("last_error"):
            runtime_error_html = (
                f'<div class="error-line"><strong>Last error:</strong> '
                f'{self._html_escape(runtime.get("last_error"))}</div>'
            )

        pretty_json = json.dumps(snapshot, indent=2)

        degraded_reasons = snapshot.get("degraded_reasons") or []

        attention_html = ""
        if degraded_reasons:
            attention_html = f"""
    <section class="section">
      <h2>Attention</h2>
      <div class="muted" style="margin-bottom:0.5rem;">App-wide degraded reasons</div>
      <div class="chips">{chips(degraded_reasons)}</div>
    </section>
"""

        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PiHub Dashboard — {self._html_escape(hostname)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
{self._shared_dark_css()}
  </style>
</head>
<body>
  {self._nav_html(current="dashboard", hostname=str(hostname))}
  <main class="page">

    <section class="section">
      <h1>Dashboard</h1>
      <div class="grid summary">
        <div class="card">
          <h3><span>Overall status</span>{self._status_badge_html(str(snapshot.get("status") or "unknown"))}</h3>
          <div class="muted">App-wide health after non-critical reasons are filtered.</div>
        </div>
        <div class="card">
          <h3>Current mode</h3>
          <div class="kv">
            {kv_row("Mode", runtime.get("mode"))}
            {kv_row("Last flow", runtime.get("last_flow"))}
            {kv_row("Flow running", runtime.get("flow_running"))}
          </div>
        </div>
        <div class="card">
          <h3>Runtime</h3>
          <div class="kv">
            {kv_row("Last trigger", runtime.get("last_trigger"))}
            {kv_row("Last result", runtime.get("last_result"))}
            {kv_row("Error", runtime.get("error"))}
          </div>
          {runtime_error_html}
        </div>
        <div class="card">
          <h3>PiHub uptime</h3>
          <div class="kv">
            {kv_row("Process uptime", system.get("process_uptime_human"))}
            {kv_row("System uptime", system.get("system_uptime_human"))}
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>Domains</h2>
      <div class="grid domains">
        {domain_card("USB", snapshot.get("usb") or {}, ["paired_remote", "reader_running", "input_open", "grabbed"])}
        {domain_card("BLE", snapshot.get("ble") or {}, ["transport_open", "advertising", "connected", "proto_report", "last_disc_reason"])}
        {domain_card("TV", snapshot.get("tv") or {}, ["initialized", "presence_on", "presence_source", "last_change_age_s", "ws_connected", "token_present"])}
        {domain_card("Speaker", snapshot.get("speaker") or {}, ["reachable", "connected", "ready", "playback_status", "source", "volume_pct", "muted", "update_age_s"])}
      </div>
    </section>

    <section class="section">
      <h2>System</h2>
      <div class="grid system">
        <div class="card">
          <h3>Load average</h3>
          <div class="kv">
            {kv_row("1m", (system.get("load") or {{}}).get("1m"))}
            {kv_row("5m", (system.get("load") or {{}}).get("5m"))}
            {kv_row("15m", (system.get("load") or {{}}).get("15m"))}
          </div>
        </div>
        <div class="card">
          <h3>Memory</h3>
          <div class="kv">
            {kv_row("Used", (system.get("memory") or {{}}).get("used_human"))}
            {kv_row("Available", (system.get("memory") or {{}}).get("available_human"))}
            {kv_row("Total", (system.get("memory") or {{}}).get("total_human"))}
          </div>
        </div>
        <div class="card">
          <h3>Disk</h3>
          <div class="kv">
            {kv_row("Path", (system.get("disk") or {{}}).get("path"))}
            {kv_row("Used", (system.get("disk") or {{}}).get("used_human"))}
            {kv_row("Free", (system.get("disk") or {{}}).get("free_human"))}
            {kv_row("Total", (system.get("disk") or {{}}).get("total_human"))}
          </div>
        </div>
      </div>
    </section>

    {attention_html}

    <section class="section">
      <h2>Health snapshot</h2>
      <p><a href="/health">Open raw /health</a></p>
      <pre class="json">{self._html_escape(pretty_json)}</pre>
    </section>

  </main>
</body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    def _status_badge_html(self, status: str) -> str:
        safe = self._html_escape(status)
        cls = {
            "ok": "status-ok",
            "degraded": "status-degraded",
            "disabled": "status-disabled",
        }.get(status, "status-error")
        return f'<span class="status-badge {cls}">{safe}</span>'

    @staticmethod
    def _fmt_value(value: object) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        if value is None:
            return "null"
        return str(value)

    async def _handle_refresh_tv(self, _: web.Request) -> web.Response:
        if self._tv is None:
            return web.json_response({"ok": False, "error": "tv unavailable"}, status=503)

        await self._tv.reconcile_presence()
        return web.json_response({"ok": True, "domain": "tv", "action": "refresh"})

    async def _handle_refresh_speaker(self, _: web.Request) -> web.Response:
        if self._speaker is None or not getattr(self._speaker, "enabled", False):
            return web.json_response({"ok": False, "error": "speaker unavailable"}, status=503)

        await self._speaker.request_refresh()
        return web.json_response({"ok": True, "domain": "speaker", "action": "refresh"})

    async def _handle_refresh_networked(self, _: web.Request) -> web.Response:
        result = {
            "ok": True,
            "action": "refresh_networked",
            "tv": None,
            "speaker": None,
        }

        if self._tv is not None:
            await self._tv.reconcile_presence()
            result["tv"] = {"ok": True}
        else:
            result["tv"] = {"ok": False, "error": "tv unavailable"}

        if self._speaker is not None and getattr(self._speaker, "enabled", False):
            await self._speaker.request_refresh()
            result["speaker"] = {"ok": True}
        else:
            result["speaker"] = {"ok": False, "error": "speaker unavailable"}

        return web.json_response(result)

    async def _handle_restart(self, _: web.Request) -> web.Response:
        async def _delayed_exit() -> None:
            await asyncio.sleep(0.25)
            import os
            os._exit(0)

        asyncio.create_task(_delayed_exit(), name="pihub_restart")
        return web.json_response({"ok": True, "action": "restart"})

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
    
    @staticmethod
    def _read_system_uptime_s() -> float | None:
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                first = f.read().strip().split()[0]
            return float(first)
        except Exception:
            return None

    @staticmethod
    def _format_duration(seconds: float | int | None) -> str | None:
        if seconds is None:
            return None
        total = int(seconds)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        if days:
            return f"{days}d {hours:02}:{minutes:02}:{secs:02}"
        return f"{hours:02}:{minutes:02}:{secs:02}"

    @staticmethod
    def _read_meminfo() -> dict[str, int]:
        out: dict[str, int] = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    key, rest = line.split(":", 1)
                    parts = rest.strip().split()
                    if not parts:
                        continue
                    try:
                        out[key] = int(parts[0]) * 1024  # kB -> bytes
                    except Exception:
                        continue
        except Exception:
            pass
        return out

    @staticmethod
    def _fmt_bytes(n: int | None) -> str | None:
        if n is None:
            return None
        value = float(n)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024.0 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024.0
        return None

    def _system_snapshot(self) -> dict:
        system_uptime_s = self._read_system_uptime_s()
        process_uptime_s = time.monotonic() - self._process_start_monotonic

        try:
            load1, load5, load15 = os.getloadavg()
            load = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
        except Exception:
            load = {"1m": None, "5m": None, "15m": None}

        meminfo = self._read_meminfo()
        mem_total = meminfo.get("MemTotal")
        mem_available = meminfo.get("MemAvailable")
        mem_used = (mem_total - mem_available) if (mem_total is not None and mem_available is not None) else None

        try:
            disk = shutil.disk_usage("/")
            disk_total = disk.total
            disk_used = disk.used
            disk_free = disk.free
        except Exception:
            disk_total = disk_used = disk_free = None

        return {
            "system_uptime_s": int(system_uptime_s) if system_uptime_s is not None else None,
            "system_uptime_human": self._format_duration(system_uptime_s),
            "process_uptime_s": int(process_uptime_s),
            "process_uptime_human": self._format_duration(process_uptime_s),
            "load": load,
            "memory": {
                "total_bytes": mem_total,
                "available_bytes": mem_available,
                "used_bytes": mem_used,
                "total_human": self._fmt_bytes(mem_total),
                "available_human": self._fmt_bytes(mem_available),
                "used_human": self._fmt_bytes(mem_used),
            },
            "disk": {
                "path": "/",
                "total_bytes": disk_total,
                "used_bytes": disk_used,
                "free_bytes": disk_free,
                "total_human": self._fmt_bytes(disk_total),
                "used_human": self._fmt_bytes(disk_used),
                "free_human": self._fmt_bytes(disk_free),
            },
        }

    def snapshot(self) -> dict:
        pihub_id = socket.gethostname()
        system_state = self._system_snapshot()

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
                usb_reasons.append("usb.receiver_missing")
            else:
                if not bool(usb_raw.get("paired_remote")):
                    usb_reasons.append("usb.no_paired_remote")
                if not bool(usb_raw.get("reader_running")):
                    usb_reasons.append("usb.reader_not_running")
                if not bool(usb_raw.get("input_open")):
                    usb_reasons.append("usb.input_not_open")
                if not bool(usb_raw.get("grabbed")):
                    usb_reasons.append("usb.not_grabbed")

            if usb_error and usb_last_error:
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
                ble_reasons.append("ble.dongle_missing")
            else:
                if ble_link_ready:
                    pass
                elif not ble_transport_up:
                    ble_reasons.append("ble.transport_down")
                elif ble_connected:
                    ble_reasons.append("ble.connected_not_ready")
                elif ble_advertising:
                    ble_reasons.append("ble.advertising")
                else:
                    ble_reasons.append("ble.idle")

            if ble_error and ble_last_error:
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
                reason
                for reason in tv_state["reasons"]
                if reason not in {"tv.presence_unknown", "tv.off"}
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
            "system": system_state,
        }