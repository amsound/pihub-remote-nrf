"""Lightweight HTTP status and control endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import os
import shutil
import time

from typing import Any, Optional
from aiohttp import web

from .runtime import RuntimeEngine
from .ble_dongle import BleDongleLink
from .unifying_input import UnifyingReader
from .speaker import SpeakerLike
from .samsung_tv import TvController
from .history import HistoryStore

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
        speaker: Optional[SpeakerLike] = None,
        settings: Any = None,
        runtime: Optional[RuntimeEngine] = None,
        history: HistoryStore | None = None,
        speaker_backend: str | None = None,
        dispatcher: Any = None,
    ) -> None:
        self._host = host
        self._port = port
        self._ble = ble
        self._reader = reader
        self._tv = tv
        self._speaker = speaker
        self._settings = settings
        self._runtime = runtime
        self._history = history
        self._speaker_backend = str(speaker_backend or "").strip().lower()
        self._dispatcher = dispatcher

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
                web.get("/settings", self._handle_settings),
                web.get("/history", self._handle_history),
                web.get("/remote", self._handle_remote_page),
                web.get("/history/events", self._handle_history_events),
                web.get("/history/flows", self._handle_history_flows),

                web.post("/history/clear", self._handle_history_clear),
                web.post("/settings/save", self._handle_settings_save),

                web.post("/flow/run/{name}", self._handle_flow_run),
                web.post("/mode/set/{name}", self._handle_mode_set),
                web.post("/command", self._handle_command),

                web.post("/remote/edge", self._handle_remote_edge),
                web.post("/remote/tap", self._handle_remote_tap),

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
        return web.json_response(snapshot, status=200)

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

    async def _handle_history_events(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50

        if self._history is None:
            return web.json_response({"ok": True, "events": []})

        return web.json_response(
            {
                "ok": True,
                "events": self._history.list_events(limit=limit),
            }
        )

    async def _handle_history_flows(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20

        if self._history is None:
            return web.json_response({"ok": True, "flows": []})

        return web.json_response(
            {
                "ok": True,
                "flows": self._history.list_flow_reports(limit=limit),
            }
        )

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
      {link("Remote", "/remote", "remote")}
      {link("Settings", "/settings", "settings")}
      {link("History", "/history", "history")}
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
        snapshot = self.snapshot()
        hostname = snapshot.get("pihub_id") or socket.gethostname()

        runtime = snapshot.get("runtime") or {}
        current_mode = str(runtime.get("mode") or "")
        current_flow = str(runtime.get("last_flow") or "")
        last_trigger = str(runtime.get("last_trigger") or "")
        last_result = str(runtime.get("last_result") or "")

        def active_class(value: str, current: str) -> str:
            return "active" if value == current else ""

        def display_value(value: str, fallback: str = "—") -> str:
            text = str(value or "").strip()
            return self._html_escape(text if text else fallback)
        
        import json
        snapshot_json = json.dumps(snapshot)

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

.raw-toggle {{
  margin-top: 1rem;
}}

.raw-toggle details {{
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel-2);
  overflow: hidden;
}}

.raw-toggle summary {{
  cursor: pointer;
  padding: 0.85rem 1rem;
  font-weight: 600;
}}

.raw-toggle .raw-body {{
  border-top: 1px solid var(--border);
  padding: 1rem;
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

.command-form.busy button {{
  opacity: 0.7;
  cursor: wait;
}}

.modal-backdrop {{
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.58);
  display: none;
  align-items: center;
  justify-content: center;
  padding: 1rem;
  z-index: 50;
}}

.modal-backdrop.open {{
  display: flex;
}}

.modal {{
  width: min(640px, 100%);
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
}}

.modal-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  padding: 1rem;
  border-bottom: 1px solid var(--border);
}}

.modal-title {{
  font-size: 1.05rem;
  font-weight: 700;
}}

.modal-body {{
  padding: 1rem;
}}

.modal-actions {{
  display: flex;
  justify-content: flex-end;
  gap: 0.75rem;
  padding: 1rem;
  border-top: 1px solid var(--border);
}}

.modal-status {{
  margin-bottom: 0.85rem;
}}

.modal-json {{
  margin-top: 0.85rem;
}}

.small-button {{
  min-width: 0;
  padding: 0.7rem 0.9rem;
  font-size: 0.95rem;
}}

@media (max-width: 640px) {{
  button {{
    width: 100%;
    min-width: 0;
  }}
  .modal-actions {{
    flex-direction: column;
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
          <h3>Last flow</h3>
          <div class="meta-value">{display_value(current_flow)}</div>
        </div>
        <div class="meta-card">
          <h3>Current mode</h3>
          <div class="meta-value">{display_value(current_mode)}</div>
        </div>
        <div class="meta-card">
          <h3>Last trigger</h3>
          <div class="meta-value">{display_value(last_trigger)}</div>
        </div>
        <div class="meta-card">
          <h3>Runtime result</h3>
          <div class="meta-value">{display_value(last_result)}</div>
        </div>
      </div>
    </section>

    <div class="tools-grid">
      <section class="section">
        <div class="section-header">
          <h2>Run Flow</h2>
          <span class="badge">{display_value(current_flow)}</span>
        </div>
        <div class="row">
          <form method="post" action="/flow/run/watch" class="command-form {active_class('watch', current_flow)}" data-refresh="1" data-title="Run watch">
            <button type="submit">Run watch</button>
          </form>
          <form method="post" action="/flow/run/listen" class="command-form {active_class('listen', current_flow)}" data-refresh="1" data-title="Run listen">
            <button type="submit">Run listen</button>
          </form>
          <form method="post" action="/flow/run/power_off" class="command-form {active_class('power_off', current_flow)}" data-refresh="1" data-title="Run power_off">
            <button type="submit">Run power_off</button>
          </form>
        </div>
      </section>

      <section class="section">
        <div class="section-header">
          <h2>Set Mode</h2>
          <span class="badge">{display_value(current_mode)}</span>
        </div>
        <div class="row">
          <form method="post" action="/mode/set/watch" class="command-form {active_class('watch', current_mode)}" data-refresh="1" data-title="Set mode watch">
            <button type="submit">Set mode watch</button>
          </form>
          <form method="post" action="/mode/set/listen" class="command-form {active_class('listen', current_mode)}" data-refresh="1" data-title="Set mode listen">
            <button type="submit">Set mode listen</button>
          </form>
          <form method="post" action="/mode/set/power_off" class="command-form {active_class('power_off', current_mode)}" data-refresh="1" data-title="Set mode power_off">
            <button type="submit">Set mode power_off</button>
          </form>
        </div>
      </section>

      <section class="section">
        <h2>Refresh</h2>
        <div class="row">
          <form method="post" action="/refresh/tv" class="command-form" data-refresh="1" data-title="Refresh TV">
            <button type="submit">Refresh TV</button>
          </form>
          <form method="post" action="/refresh/speaker" class="command-form" data-refresh="1" data-title="Refresh Speaker">
            <button type="submit">Refresh Speaker</button>
          </form>
          <form method="post" action="/refresh/networked" class="command-form" data-refresh="1" data-title="Refresh Networked">
            <button type="submit">Refresh Networked</button>
          </form>
        </div>
      </section>

      <section class="section danger">
        <h2>Admin</h2>
        <div class="row">
          <form method="post" action="/admin/restart" class="command-form" data-refresh="1" data-title="Restart PiHub">
            <button type="submit">Restart PiHub</button>
          </form>
        </div>
        <p class="muted">Exits the process and relies on the container restart policy to bring it back.</p>
      </section>
    </div>

    <section class="section raw-toggle">
      <h2>Raw health JSON</h2>
      <details>
        <summary>Show raw /health JSON</summary>
        <div class="raw-body">
          <p><a href="/health">Open raw /health</a></p>
          <pre id="tools-raw-json" class="json"></pre>
        </div>
      </details>
    </section>
  </main>

  <div id="command-modal-backdrop" class="modal-backdrop" aria-hidden="true">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="command-modal-title">
      <div class="modal-header">
        <div id="command-modal-title" class="modal-title">Command result</div>
      </div>
      <div class="modal-body">
        <div id="command-modal-status" class="modal-status"></div>
        <div id="command-modal-message" class="muted"></div>
        <div class="modal-json">
          <pre id="command-modal-json" class="json"></pre>
        </div>
      </div>
      <div class="modal-actions">
        <button id="command-modal-close" type="button" class="small-button">OK</button>
      </div>
    </div>
  </div>

  <script>
    (function () {{
      const rawPre = document.getElementById("tools-raw-json");
      const snapshot = JSON.parse({json.dumps(snapshot_json)});
      if (rawPre) {{
        rawPre.textContent = JSON.stringify(snapshot, null, 2);
      }}

      const forms = Array.from(document.querySelectorAll(".command-form"));
      const backdrop = document.getElementById("command-modal-backdrop");
      const modalTitle = document.getElementById("command-modal-title");
      const modalStatus = document.getElementById("command-modal-status");
      const modalMessage = document.getElementById("command-modal-message");
      const modalJson = document.getElementById("command-modal-json");
      const modalClose = document.getElementById("command-modal-close");

      let refreshOnClose = false;

      function badgeHtml(ok) {{
        const cls = ok ? "status-ok" : "status-error";
        const label = ok ? "Success" : "Failed";
        return '<span class="status-badge ' + cls + '">' + label + '</span>';
      }}

      function openModal(title, ok, message, payload, shouldRefresh) {{
        refreshOnClose = !!shouldRefresh;
        if (modalTitle) modalTitle.textContent = title || "Command result";
        if (modalStatus) modalStatus.innerHTML = badgeHtml(ok);
        if (modalMessage) modalMessage.textContent = message || "";
        if (modalJson) modalJson.textContent = JSON.stringify(payload, null, 2);
        if (backdrop) {{
          backdrop.classList.add("open");
          backdrop.setAttribute("aria-hidden", "false");
        }}
      }}

      function closeModal() {{
        if (backdrop) {{
          backdrop.classList.remove("open");
          backdrop.setAttribute("aria-hidden", "true");
        }}
        if (refreshOnClose) {{
          window.location.reload();
        }}
      }}

      if (modalClose) {{
        modalClose.addEventListener("click", closeModal);
      }}

      if (backdrop) {{
        backdrop.addEventListener("click", function (event) {{
          if (event.target === backdrop) {{
            closeModal();
          }}
        }});
      }}

      forms.forEach(function (form) {{
        form.addEventListener("submit", async function (event) {{
          event.preventDefault();

          const button = form.querySelector("button");
          const title = form.getAttribute("data-title") || "Command result";
          const shouldRefresh = form.getAttribute("data-refresh") === "1";

          form.classList.add("busy");
          if (button) button.disabled = true;

          try {{
            const response = await fetch(form.action, {{
              method: "POST",
              headers: {{
                "Accept": "application/json"
              }}
            }});

            let payload = null;
            try {{
              payload = await response.json();
            }} catch (_err) {{
              payload = {{}};
            }}

            const ok = !!(payload && payload.ok);
            let message = "";
            if (payload && payload.error) {{
              message = String(payload.error);
            }} else if (payload && payload.reason) {{
              message = String(payload.reason);
            }} else if (payload && payload.action) {{
              message = String(payload.action);
            }} else {{
              message = response.ok ? "Request completed." : "Request failed.";
            }}

            openModal(title, ok && response.ok, message, payload, shouldRefresh);
          }} catch (err) {{
            openModal(title, false, String(err), {{"ok": false, "error": String(err)}}, false);
          }} finally {{
            form.classList.remove("busy");
            if (button) button.disabled = false;
          }}
        }});
      }});
    }})();
  </script>
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
                if key not in details:
                    continue

                if key == "conn_params" and isinstance(details.get(key), dict):
                    cp = details.get(key) or {}
                    interval_ms = cp.get("interval_ms")
                    latency = cp.get("latency")
                    timeout_ms = cp.get("timeout_ms")
                    compact = f"{interval_ms} / {latency} / {timeout_ms}"
                    rows.append(kv_row("Conn params", compact))
                    continue

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

.raw-toggle {{
  margin-top: 1rem;
}}

.raw-toggle details {{
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel-2);
  overflow: hidden;
}}

.raw-toggle summary {{
  cursor: pointer;
  padding: 0.85rem 1rem;
  font-weight: 600;
}}

.raw-toggle .raw-body {{
  border-top: 1px solid var(--border);
  padding: 1rem;
}}
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
          <h3>PiHub System</h3>
          <div class="kv">
            {kv_row("CPU / SoC", f"{system.get('cpu_temp_c')} °C" if system.get("cpu_temp_c") is not None else "unknown")}
            {kv_row("Container uptime", system.get("process_uptime_human"))}
            {kv_row("System uptime", system.get("system_uptime_human"))}
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>Domains</h2>
      <div class="grid domains">
        {domain_card("USB", snapshot.get("usb") or {{}}, ["paired_remote", "reader_running", "input_open", "grabbed"])}
        {domain_card("BLE", snapshot.get("ble") or {{}}, ["transport_open", "advertising", "connected", "proto_report", "last_disc_reason", "conn_params"])}
        {domain_card("TV", snapshot.get("tv") or {{}}, ["initialised", "presence_on", "presence_source", "last_change_age_s", "ws_connected", "token_present"])}
        {domain_card("Speaker", snapshot.get("speaker") or {{}}, ["backend", "reachable", "connected", "ready", "playback_status", "source", "volume_pct", "muted", "update_age_s"])}
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
        <div class="card">
          <h3>Power / Temp</h3>
          <div class="kv">
            {kv_row("CPU temp", f"{system.get('cpu_temp_c')} °C" if system.get("cpu_temp_c") is not None else "unknown")}
            {kv_row("Power status", (system.get("throttling") or {{}}).get("status"))}
            {kv_row("Undervoltage now", (system.get("throttling") or {{}}).get("undervoltage_now"))}
            {kv_row("Throttled now", (system.get("throttling") or {{}}).get("throttled_now"))}
            {kv_row("Historical events", (
                (
                    (system.get("throttling") or {{}}).get("undervoltage_occurred")
                    or (system.get("throttling") or {{}}).get("freq_capped_occurred")
                    or (system.get("throttling") or {{}}).get("throttled_occurred")
                    or (system.get("throttling") or {{}}).get("temp_limit_occurred")
                )
                if (system.get("throttling") or {{}}).get("available")
                else "unknown"
            ))}
          </div>
        </div>
      </div>
    </section>

    {attention_html}

    <section class="section raw-toggle">
      <h2>Raw health JSON</h2>
      <details>
        <summary>Show raw /health JSON</summary>
        <div class="raw-body">
          <p><a href="/health">Open raw /health</a></p>
          <pre class="json">{self._html_escape(pretty_json)}</pre>
        </div>
      </details>
    </section>

  </main>
</body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_settings(self, request: web.Request) -> web.Response:
        if self._settings is None:
            return web.Response(text="settings unavailable", status=503)

        saved = request.query.get("saved") == "1"
        error = request.query.get("error") or ""

        snapshot = self.snapshot()
        hostname = snapshot.get("pihub_id") or socket.gethostname()
        settings = self._settings.snapshot()

        backend = self._speaker_backend
        is_soundbar = backend == "samsung_soundbar"

        def selected(name: str, value: str) -> str:
            return ' selected="selected"' if str(settings.get(name)) == value else ""

        def field(name: str) -> str:
            return self._html_escape(settings.get(name, ""))

        saved_html = (
            '<div class="section"><div class="chip" style="background:#123222;border-color:#1d4d32;color:#86efac;">Settings saved</div></div>'
            if saved else ""
        )
        error_html = (
            f'<div class="section"><div class="error-line"><strong>Save failed:</strong> {self._html_escape(error)}</div></div>'
            if error else ""
        )

        listen_target_html = ""
        stream_urls_html = ""
        backend_note_html = ""

        if is_soundbar:
            backend_note_html = """
    <section class="section">
      <div class="chip">Samsung Soundbar backend detected</div>
      <p class="muted" style="margin-top:0.75rem;">Listen target preset and stream URL settings are hidden because they are not used by the SmartThings soundbar backend.</p>
    </section>
"""
        else:
            listen_target_html = f"""
        <h2 style="margin-top:1.25rem;">Listen Flow Preset</h2>
        <div class="form-grid">
          <div class="field">
            <label for="listen_target_type">Type</label>
            <select id="listen_target_type" name="listen_target_type">
              <option value="preset"{selected('listen_target_type', 'preset')}>Speaker native preset</option>
              <option value="stream"{selected('listen_target_type', 'stream')}>Stream URL</option>
            </select>
          </div>
          <div class="field" id="listen-target-preset-field">
            <label for="listen_target_preset">Speaker preset (1–6)</label>
            <input id="listen_target_preset" name="listen_target_preset" type="number" min="1" max="6" value="{field('listen_target_preset')}">
          </div>
          <div class="field" id="listen-target-stream-field">
            <label for="listen_target_stream">Stream URL slot (1–4)</label>
            <input id="listen_target_stream" name="listen_target_stream" type="number" min="1" max="4" value="{field('listen_target_stream')}">
          </div>
        </div>
"""
            stream_urls_html = f"""
        <h2 style="margin-top:1.25rem;">Extra Stream URLs</h2>
        <div class="form-grid">
          <div class="field">
            <label for="stream_url_1">Key 7 stream URL</label>
            <input id="stream_url_1" name="stream_url_1" type="url" value="{field('stream_url_1')}">
          </div>
          <div class="field">
            <label for="stream_url_2">Key 8 stream URL</label>
            <input id="stream_url_2" name="stream_url_2" type="url" value="{field('stream_url_2')}">
          </div>
          <div class="field">
            <label for="stream_url_3">Key 9 stream URL</label>
            <input id="stream_url_3" name="stream_url_3" type="url" value="{field('stream_url_3')}">
          </div>
          <div class="field">
            <label for="stream_url_4">Key 0 stream URL</label>
            <input id="stream_url_4" name="stream_url_4" type="url" value="{field('stream_url_4')}">
          </div>
        </div>
"""

        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PiHub Settings — {self._html_escape(hostname)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
{self._shared_dark_css()}
input, select {{
  width: 100%;
  padding: 0.75rem 0.8rem;
  min-height: 48px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--text);
  font: inherit;
}}

select {{
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  line-height: 1.2;
}}
label {{
  display: block;
  margin-bottom: 0.35rem;
  color: var(--muted);
  font-size: 0.95rem;
}}
.form-grid {{
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}}
.field {{
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}}
button {{
  padding: 0.8rem 1rem;
  font-size: 1rem;
  cursor: pointer;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--text);
}}
button:hover {{
  border-color: var(--accent);
  background: #1b2740;
}}
.hidden {{
  display: none !important;
}}
  </style>
</head>
<body>
  {self._nav_html(current="settings", hostname=str(hostname))}
  <main class="page">
    <section class="section">
      <h1>Settings</h1>
      <p class="muted">Changes apply immediately to future flows and future stream URL button presses. Running flows keep their existing start snapshot.</p>
    </section>

    {saved_html}
    {error_html}
    {backend_note_html}

    <section class="section">
      <form method="post" action="/settings/save">
        <h2>Speaker Levels</h2>
        <div class="form-grid">
          <div class="field">
            <label for="watch_volume_pct">Watch volume (0–100)</label>
            <input id="watch_volume_pct" name="watch_volume_pct" type="number" min="0" max="100" value="{field('watch_volume_pct')}">
          </div>
          <div class="field">
            <label for="listen_volume_pct">Listen volume (0–100)</label>
            <input id="listen_volume_pct" name="listen_volume_pct" type="number" min="0" max="100" value="{field('listen_volume_pct')}">
          </div>
        </div>

        {listen_target_html}
        {stream_urls_html}

        <div style="margin-top:1.25rem;">
          <button type="submit">Save settings</button>
        </div>
      </form>
    </section>
  </main>
  <script>
    (function () {{
      const typeSelect = document.getElementById("listen_target_type");
      const presetField = document.getElementById("listen-target-preset-field");
      const streamField = document.getElementById("listen-target-stream-field");

      function updateListenTargetFields() {{
        const mode = typeSelect ? typeSelect.value : "";
        if (!presetField || !streamField) return;

        if (mode === "preset") {{
          presetField.classList.remove("hidden");
          streamField.classList.add("hidden");
        }} else if (mode === "stream") {{
          presetField.classList.add("hidden");
          streamField.classList.remove("hidden");
        }} else {{
          presetField.classList.remove("hidden");
          streamField.classList.remove("hidden");
        }}
      }}

      if (typeSelect) {{
        typeSelect.addEventListener("change", updateListenTargetFields);
        updateListenTargetFields();
      }}
    }})();
  </script>
</body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_settings_save(self, request: web.Request) -> web.Response:
        if self._settings is None:
            return web.Response(text="settings unavailable", status=503)

        data = await request.post()
        payload = dict(data)

        try:
            self._settings.save_from_payload(payload, speaker_backend=self._speaker_backend)
        except Exception as exc:
            from urllib.parse import quote
            raise web.HTTPFound(location=f"/settings?error={quote(str(exc))}")

        raise web.HTTPFound(location="/settings?saved=1")

    async def _handle_history(self, request: web.Request) -> web.Response:
        snapshot = self.snapshot()
        hostname = snapshot.get("pihub_id") or socket.gethostname()
        cleared = request.query.get("cleared") == "1"

        flows = self._history.list_flow_reports(limit=20) if self._history is not None else []
        events = self._history.list_events(limit=50) if self._history is not None else []

        problem_events = [
            event for event in events
            if str(event.get("level") or "").lower() in {"warning", "error"}
        ]

        cleared_html = (
            '<div class="chip" style="background:#123222;border-color:#1d4d32;color:#86efac;">History cleared</div>'
            if cleared else ""
        )

        def badge_html(result: str) -> str:
            safe = self._html_escape(result)
            cls = {
                "ok": "status-ok",
                "ok_with_warnings": "status-degraded",
                "failed": "status-error",
                "running": "status-disabled",
            }.get(result, "status-disabled")
            return f'<span class="status-badge {cls}">{safe}</span>'

        def fmt_duration(ms: object) -> str:
            try:
                value = int(ms)
            except Exception:
                return ""
            if value < 1000:
                return f"{value} ms"
            return f"{value / 1000.0:.2f} s"

        def step_meta(step: dict) -> str:
            parts: list[str] = []

            status = str(step.get("status") or "").strip()
            if status:
                parts.append(f'<span class="small-badge step-badge">{self._html_escape(status)}</span>')

            outcome_status = str(step.get("outcome_status") or "").strip()
            if outcome_status:
                parts.append(
                    f'<span class="small-badge outcome-badge">{self._html_escape("outcome " + outcome_status)}</span>'
                )

            duration = fmt_duration(step.get("duration_ms"))
            if duration:
                parts.append(f'<span class="small-meta">{self._html_escape(duration)}</span>')

            outcome_duration = fmt_duration(step.get("outcome_duration_ms"))
            if outcome_duration:
                parts.append(f'<span class="small-meta">{self._html_escape("settled " + outcome_duration)}</span>')

            return "".join(parts)

        def step_lines(step: dict) -> str:
            lines: list[str] = []

            reason = str(step.get("reason") or "").strip()
            if reason:
                lines.append(f'<div class="step-line muted">{self._html_escape(reason)}</div>')

            error = str(step.get("error") or "").strip()
            if error:
                lines.append(f'<div class="step-line error-line"><strong>Error:</strong> {self._html_escape(error)}</div>')

            outcome_error = str(step.get("outcome_error") or "").strip()
            if outcome_error:
                lines.append(
                    f'<div class="step-line error-line"><strong>Dispatch warning:</strong> {self._html_escape(outcome_error)}</div>'
                )

            started = self._format_time_only(step.get("iso_ts_started"))
            finished = self._format_time_only(step.get("iso_ts_finished"))
            outcome = self._format_time_only(step.get("iso_ts_outcome"))

            status = str(step.get("status") or "").strip()
            duration = fmt_duration(step.get("duration_ms"))
            outcome_duration = fmt_duration(step.get("outcome_duration_ms"))

            if started:
                if status == "dispatched":
                    lines.append(f'<div class="step-line muted">Dispatched: {self._html_escape(started)}</div>')
                else:
                    lines.append(f'<div class="step-line muted">Started: {self._html_escape(started)}</div>')

            if outcome:
                line = f"Outcome: {outcome}"
                if outcome_duration:
                    line += f" ({outcome_duration})"
                lines.append(f'<div class="step-line muted">{self._html_escape(line)}</div>')
            elif status != "dispatched":
                if duration:
                    lines.append(f'<div class="step-line muted">Took: {self._html_escape(duration)}</div>')
                elif finished and finished != started:
                    lines.append(f'<div class="step-line muted">Finished: {self._html_escape(finished)}</div>')

            return "".join(lines)

        def render_step(step: dict) -> str:
            step_id = str(step.get("step_id") or "").strip()
            domain = str(step.get("domain") or "").strip()
            action = str(step.get("action") or "").strip()

            title = " · ".join(part for part in [step_id, f"{domain}.{action}" if domain and action else ""] if part)

            css_classes = ["step-item"]
            if str(step.get("status") or "") == "skipped":
                css_classes.append("step-skipped")

            return f"""
<div class="{' '.join(css_classes)}" data-step-status="{self._html_escape(str(step.get('status') or ''))}">
  <div class="step-head">
    <div class="step-title">{self._html_escape(title)}</div>
    <div class="step-meta">{step_meta(step)}</div>
  </div>
  <div class="step-body">
    {step_lines(step)}
  </div>
</div>
"""

        def render_flow(flow: dict) -> str:
            flow_name = str(flow.get("flow_name") or "").strip()
            result = str(flow.get("result") or "").strip()
            trigger = self._prettify_trigger(flow.get("trigger"))
            source = str(flow.get("source") or "").strip().replace("_", " ")
            started = self._format_flow_started(flow.get("iso_ts_started"))
            duration = fmt_duration(flow.get("duration_ms"))
            warnings = flow.get("warnings") or []
            error = str(flow.get("error") or "").strip()

            summary_bits: list[str] = []
            if trigger:
                summary_bits.append(f"Trigger: {trigger}")
            if source:
                summary_bits.append(f"Source: {source}")
            if started:
                summary_bits.append(f"Started: {started}")
            if duration:
                summary_bits.append(f"Duration: {duration}")

            summary_html = " · ".join(self._html_escape(bit) for bit in summary_bits)

            warnings_html = ""
            if warnings:
                items = "".join(
                    f'<li>{self._html_escape(str(item))}</li>'
                    for item in warnings
                    if str(item).strip()
                )
                if items:
                    warnings_html = f"""
<div class="flow-warning-box">
  <div class="flow-subheading">Warnings</div>
  <ul class="flow-list">{items}</ul>
</div>
"""

            error_html = ""
            if error:
                error_html = f'<div class="error-line"><strong>Flow error:</strong> {self._html_escape(error)}</div>'

            steps = flow.get("steps") or []
            steps_html = "".join(render_step(step) for step in steps)

            warning_count_html = ""
            if warnings:
                warning_count_html = f'<span class="small-badge warn-count">{len(warnings)} warning{"s" if len(warnings) != 1 else ""}</span>'

            return f"""
<details class="flow-card">
  <summary class="flow-summary">
    <div class="flow-summary-left">
      <div class="flow-title-row">
        <span class="flow-title">{self._html_escape(flow_name)}</span>
        {badge_html(result)}
        {warning_count_html}
      </div>
      <div class="flow-summary-meta">{summary_html}</div>
    </div>
  </summary>

  <div class="flow-body">
    {warnings_html}
    {error_html}

    <div class="flow-subheading">Steps</div>
    <div class="steps">
      {steps_html}
    </div>
  </div>
</details>
"""

        def render_problem_event(event: dict) -> str:
            event_time = self._format_flow_started(event.get("iso_ts"))
            kind = str(event.get("kind") or "").strip().replace("_", " ")
            level = str(event.get("level") or "").strip()
            message = str(event.get("message") or "").strip()
            flow_name = str(event.get("flow_name") or "").strip()

            meta_bits: list[str] = []
            if event_time:
                meta_bits.append(event_time)
            if flow_name:
                meta_bits.append(f"flow={flow_name}")
            if kind:
                meta_bits.append(f"kind={kind}")

            meta_html = " · ".join(self._html_escape(bit) for bit in meta_bits)

            level_cls = {
                "warning": "status-degraded",
                "error": "status-error",
            }.get(level, "status-disabled")

            return f"""
<div class="issue-item">
  <div class="issue-head">
    <span class="status-badge {level_cls}">{self._html_escape(level)}</span>
    <span class="issue-message">{self._html_escape(message)}</span>
  </div>
  <div class="issue-meta">{meta_html}</div>
</div>
"""

        flows_html = "".join(render_flow(flow) for flow in flows)
        if not flows_html:
            flows_html = '<div class="muted">No flow history yet.</div>'

        issues_html = "".join(render_problem_event(event) for event in problem_events)
        if not issues_html:
            issues_html = '<div class="muted">No recent warnings or errors.</div>'

        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PiHub History — {self._html_escape(str(hostname))}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
{self._shared_dark_css()}

.controls {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}}

.controls label {{
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--muted);
  font-size: 0.95rem;
}}

.controls input[type="checkbox"] {{
  transform: scale(1.1);
}}

.secondary-button {{
  padding: 0.65rem 0.9rem;
  font-size: 0.95rem;
  cursor: pointer;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--text);
}}

.secondary-button:hover {{
  border-color: #6c2525;
  background: #2a1818;
}}

.flow-card {{
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 14px;
  margin-bottom: 0.85rem;
  overflow: hidden;
}}

.flow-summary {{
  list-style: none;
  cursor: pointer;
  padding: 1rem;
}}

.flow-summary::-webkit-details-marker {{
  display: none;
}}

.flow-summary-left {{
  min-width: 0;
}}

.flow-title-row {{
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-bottom: 0.35rem;
}}

.flow-title {{
  font-size: 1.02rem;
  font-weight: 700;
}}

.flow-summary-meta {{
  color: var(--muted);
  font-size: 0.92rem;
  line-height: 1.4;
  word-break: break-word;
}}

.flow-body {{
  border-top: 1px solid var(--border);
  padding: 1rem;
}}

.flow-subheading {{
  font-weight: 700;
  margin-bottom: 0.6rem;
}}

.flow-warning-box {{
  margin-bottom: 1rem;
  padding: 0.8rem;
  border-radius: 12px;
  background: #2b2414;
  border: 1px solid #6b4f1d;
}}

.flow-list {{
  margin: 0.4rem 0 0 1.1rem;
  padding: 0;
}}

.warn-count {{
  background: var(--deg-bg);
  color: var(--deg-fg);
  border-color: #6b4f1d;
}}

.steps {{
  display: grid;
  gap: 0.6rem;
}}

.step-item {{
  padding: 0.85rem;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: #151b26;
}}

.step-skipped {{
  opacity: 0.8;
}}

.step-head {{
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: flex-start;
  flex-wrap: wrap;
}}

.step-title {{
  font-weight: 600;
  word-break: break-word;
}}

.step-meta {{
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
  align-items: center;
}}

.step-body {{
  margin-top: 0.55rem;
  display: grid;
  gap: 0.35rem;
}}

.step-line {{
  font-size: 0.92rem;
  line-height: 1.4;
}}

.small-meta {{
  color: var(--muted);
  font-size: 0.85rem;
}}

.step-badge {{
  background: #223047;
  color: #bfdbfe;
  border-color: #324a6d;
}}

.outcome-badge {{
  background: #1d2d21;
  color: #86efac;
  border-color: #2f5a3a;
}}

.issue-item {{
  padding: 0.85rem;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: #151b26;
  margin-bottom: 0.7rem;
}}

.issue-head {{
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
  margin-bottom: 0.35rem;
}}

.issue-message {{
  font-weight: 600;
}}

.issue-meta {{
  color: var(--muted);
  font-size: 0.9rem;
  line-height: 1.4;
  word-break: break-word;
}}

.hidden-by-filter {{
  display: none !important;
}}
  </style>
</head>
<body>
  {self._nav_html(current="history", hostname=str(hostname))}
  <main class="page">
    <section class="section">
      <h1>History</h1>
      <p class="muted">Recent flows first. Empty fields are omitted. Skipped steps are hidden by default.</p>
      <div class="controls">
        <label><input id="toggle-skipped" type="checkbox"> Show skipped steps</label>
        <form method="post" action="/history/clear" onsubmit="return confirm('Clear all persisted history?');" style="margin:0;">
          <button type="submit" class="secondary-button">Clear history</button>
        </form>
        {cleared_html}
      </div>
    </section>

    <section class="section">
      <h2>Recent Flows</h2>
      {flows_html}
    </section>

    <section class="section">
      <h2>Recent Issues</h2>
      {issues_html}
    </section>
  </main>

  <script>
    (function () {{
      const toggle = document.getElementById("toggle-skipped");

      function applySkippedFilter() {{
        const showSkipped = !!(toggle && toggle.checked);
        document.querySelectorAll('[data-step-status="skipped"]').forEach((el) => {{
          if (showSkipped) {{
            el.classList.remove("hidden-by-filter");
          }} else {{
            el.classList.add("hidden-by-filter");
          }}
        }});
      }}

      if (toggle) {{
        toggle.addEventListener("change", applySkippedFilter);
      }}

      applySkippedFilter();
    }})();
  </script>
</body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_history_clear(self, _: web.Request) -> web.Response:
        if self._history is not None:
            self._history.clear()
        raise web.HTTPFound(location="/history?cleared=1")

    async def _handle_remote_page(self, request: web.Request) -> web.Response:
        import json

        snapshot = self.snapshot()
        hostname = snapshot.get("pihub_id") or socket.gethostname()
        runtime = snapshot.get("runtime") or {}
        status = str(snapshot.get("status") or "unknown")

        remote_snapshot = {
            "status": status,
            "mode": runtime.get("mode"),
            "last_flow": runtime.get("last_flow"),
            "flow_running": runtime.get("flow_running"),
            "last_result": runtime.get("last_result"),
            "last_trigger": runtime.get("last_trigger"),
        }
        remote_snapshot_json = json.dumps(remote_snapshot)

        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PiHub Remote — {self._html_escape(hostname)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
{self._shared_dark_css()}

.remote-wrap {{
  max-width: 560px;
  margin: 0 auto;
}}

.status-grid {{
  display: grid;
  gap: 0.55rem;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-bottom: 0.85rem;
}}

.status-card {{
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 0.7rem 0.8rem;
}}

.status-card .label {{
  color: var(--muted);
  font-size: 0.8rem;
  margin-bottom: 0.2rem;
}}

.status-card .value {{
  font-size: 0.95rem;
  font-weight: 700;
  word-break: break-word;
}}

.remote-panel {{
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 0.9rem;
}}

.remote-grid {{
  display: grid;
  gap: 0.75rem;
}}

.remote-top {{
  display: grid;
  gap: 0.65rem;
  grid-template-columns: 1fr 1fr 1fr;
}}

.remote-cluster {{
  display: grid;
  gap: 0.65rem;
  grid-template-columns: 1fr 1fr;
}}

.remote-button {{
  appearance: none;
  width: 100%;
  min-height: 58px;
  border-radius: 16px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--text);
  font: inherit;
  font-size: 0.98rem;
  font-weight: 700;
  cursor: pointer;
  transition: transform 0.06s ease, border-color 0.12s ease, background 0.12s ease;
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
  touch-action: manipulation;
}}

.remote-button:hover {{
  border-color: var(--accent);
  background: #1b2740;
}}

.remote-button.is-pressed {{
  transform: scale(0.98);
  border-color: var(--accent);
  background: #162236;
}}

.remote-button.is-active {{
  border: 2px solid var(--accent);
  background: #162236;
}}

.remote-button.power {{
  border-color: #6c2525;
}}

.remote-button.power.is-active {{
  border-color: #ef4444;
  background: #2a1818;
}}

.remote-button.power:hover {{
  background: #2a1818;
}}

.remote-main {{
  display: grid;
  gap: 0.75rem;
  grid-template-columns: 88px 1fr 88px;
  align-items: center;
}}

.side-stack {{
  display: grid;
  gap: 0.65rem;
}}

.side-button {{
  min-height: 62px;
}}

.dpad-wrap {{
  display: grid;
  gap: 0.65rem;
  justify-items: center;
}}

.dpad-row {{
  display: grid;
  gap: 0.65rem;
  grid-template-columns: 84px 84px 84px;
  align-items: center;
}}

.dpad-row.center {{
  grid-template-columns: 84px 104px 84px;
}}

.dpad-spacer {{
  width: 84px;
  height: 58px;
}}

.dpad-button {{
  min-height: 58px;
}}

.ok-button {{
  min-height: 76px;
  border-radius: 999px;
}}

.bottom-row {{
  display: grid;
  gap: 0.65rem;
  grid-template-columns: 1fr 1fr;
}}

.toast {{
  position: fixed;
  left: 50%;
  bottom: 1rem;
  transform: translateX(-50%);
  background: var(--panel);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.8rem 1rem;
  border-radius: 12px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease, transform 0.15s ease;
  z-index: 50;
  max-width: min(92vw, 520px);
}}

.toast.show {{
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}}

.toast.error {{
  border-color: #6c2525;
  background: #2a1818;
  color: #fecaca;
}}

.remote-help {{
  margin-top: 0.65rem;
  color: var(--muted);
  font-size: 0.88rem;
  line-height: 1.4;
}}

@media (max-width: 640px) {{
  .remote-wrap {{
    max-width: 100%;
  }}

  .status-grid {{
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.45rem;
  }}

  .status-card {{
    padding: 0.6rem 0.65rem;
  }}

  .status-card .label {{
    font-size: 0.76rem;
  }}

  .status-card .value {{
    font-size: 0.9rem;
  }}

  .remote-top {{
    gap: 0.55rem;
  }}

  .remote-cluster {{
    gap: 0.55rem;
  }}

  .remote-button {{
    min-height: 52px;
    font-size: 0.94rem;
  }}

  .remote-main {{
    grid-template-columns: 74px 1fr 74px;
    gap: 0.6rem;
  }}

  .side-stack {{
    gap: 0.55rem;
  }}

  .side-button {{
    min-height: 56px;
    font-size: 0.88rem;
  }}

  .dpad-row {{
    gap: 0.55rem;
    grid-template-columns: 72px 72px 72px;
  }}

  .dpad-row.center {{
    grid-template-columns: 72px 88px 72px;
  }}

  .dpad-spacer {{
    width: 72px;
    height: 52px;
  }}

  .dpad-button {{
    min-height: 52px;
    font-size: 0.88rem;
  }}

  .ok-button {{
    min-height: 68px;
  }}

  .bottom-row {{
    gap: 0.55rem;
  }}
}}
  </style>
</head>
<body>
  {self._nav_html(current="remote", hostname=str(hostname))}
  <main class="page">
    <div class="remote-wrap">
      <section class="section">
        <h1>Remote</h1>
        <p class="muted">First pass virtual remote. Mode buttons use safe tap actions. Volume and channel use edge down/up so hold works naturally later.</p>
      </section>

      <section class="section">
        <div class="status-grid">
          <div class="status-card">
            <div class="label">Overall status</div>
            <div class="value" id="remote-status">{self._html_escape(status)}</div>
          </div>
          <div class="status-card">
            <div class="label">Current mode</div>
            <div class="value" id="remote-mode">{self._html_escape(runtime.get("mode") or "—")}</div>
          </div>
          <div class="status-card">
            <div class="label">Last flow</div>
            <div class="value" id="remote-last-flow">{self._html_escape(runtime.get("last_flow") or "—")}</div>
          </div>
          <div class="status-card">
            <div class="label">Flow running</div>
            <div class="value" id="remote-flow-running">{self._html_escape("true" if runtime.get("flow_running") else "false")}</div>
          </div>
        </div>
      </section>

      <section class="remote-panel">
        <div class="remote-grid">
          <div class="remote-top">
            <button
              type="button"
              class="remote-button power"
              data-kind="tap"
              data-key="rem_power_off"
              data-refresh="1"
              data-target-mode="power_off"
              id="btn-power-off"
            >
              Off
            </button>

            <div class="remote-cluster" style="grid-column: span 2;">
              <button
                type="button"
                class="remote-button"
                data-kind="tap"
                data-key="rem_mode_1"
                data-refresh="1"
                data-target-mode="listen"
                id="btn-listen"
              >
                Listen
              </button>
              <button
                type="button"
                class="remote-button"
                data-kind="tap"
                data-key="rem_mode_2"
                data-refresh="1"
                data-target-mode="watch"
                id="btn-watch"
              >
                Watch
              </button>
            </div>
          </div>

          <div class="remote-main">
            <div class="side-stack">
              <button
                type="button"
                class="remote-button side-button"
                data-kind="edge"
                data-key="rem_vol_up"
              >
                Vol+
              </button>
              <button
                type="button"
                class="remote-button side-button"
                data-kind="edge"
                data-key="rem_vol_down"
              >
                Vol-
              </button>
            </div>

            <div class="dpad-wrap">
              <div class="dpad-row">
                <div class="dpad-spacer"></div>
                <button type="button" class="remote-button dpad-button" data-kind="tap" data-key="rem_dir_up">Up</button>
                <div class="dpad-spacer"></div>
              </div>

              <div class="dpad-row center">
                <button type="button" class="remote-button dpad-button" data-kind="tap" data-key="rem_dir_left">Left</button>
                <button type="button" class="remote-button ok-button" data-kind="tap" data-key="rem_ok">OK</button>
                <button type="button" class="remote-button dpad-button" data-kind="tap" data-key="rem_dir_right">Right</button>
              </div>

              <div class="dpad-row">
                <div class="dpad-spacer"></div>
                <button type="button" class="remote-button dpad-button" data-kind="tap" data-key="rem_dir_down">Down</button>
                <div class="dpad-spacer"></div>
              </div>
            </div>

            <div class="side-stack">
              <button
                type="button"
                class="remote-button side-button"
                data-kind="edge"
                data-key="rem_ch_up"
              >
                Ch+
              </button>
              <button
                type="button"
                class="remote-button side-button"
                data-kind="edge"
                data-key="rem_ch_down"
              >
                Ch-
              </button>
            </div>
          </div>

          <div class="bottom-row">
            <button
              type="button"
              class="remote-button"
              data-kind="tap"
              data-key="rem_back"
            >
              Home
            </button>
            <button
              type="button"
              class="remote-button"
              data-kind="tap"
              data-key="rem_menu"
            >
              Menu
            </button>
          </div>
        </div>

        <div class="remote-help">
          Tap buttons send a safe logical remote tap. Volume and channel buttons use real down/up edges and release automatically if the pointer is cancelled or the page loses focus.
        </div>
      </section>
    </div>
  </main>

  <div id="remote-toast" class="toast" aria-live="polite"></div>

  <script>
    (function () {{
      const initialState = JSON.parse({json.dumps(remote_snapshot_json)});
      const toastEl = document.getElementById("remote-toast");

      const statusEl = document.getElementById("remote-status");
      const modeEl = document.getElementById("remote-mode");
      const lastFlowEl = document.getElementById("remote-last-flow");
      const flowRunningEl = document.getElementById("remote-flow-running");

      const activeModeButtons = {{
        power_off: document.getElementById("btn-power-off"),
        listen: document.getElementById("btn-listen"),
        watch: document.getElementById("btn-watch")
      }};

      const pressedEdgeButtons = new Map();

      function showToast(message, isError) {{
        if (!toastEl) return;
        toastEl.textContent = message || "";
        toastEl.classList.toggle("error", !!isError);
        toastEl.classList.add("show");
        window.clearTimeout(showToast._timer);
        showToast._timer = window.setTimeout(function () {{
          toastEl.classList.remove("show");
        }}, 1800);
      }}

      function setPressed(el, pressed) {{
        if (!el) return;
        el.classList.toggle("is-pressed", !!pressed);
      }}

      function setModeHighlight(mode) {{
        Object.entries(activeModeButtons).forEach(function ([name, el]) {{
          if (!el) return;
          el.classList.toggle("is-active", name === mode);
        }});
      }}

      function updateStatusUi(data) {{
        const status = String((data && data.status) || "unknown");
        const runtime = (data && data.runtime) || {{}};

        if (statusEl) statusEl.textContent = status;
        if (modeEl) modeEl.textContent = String(runtime.mode || "—");
        if (lastFlowEl) lastFlowEl.textContent = String(runtime.last_flow || "—");
        if (flowRunningEl) flowRunningEl.textContent = runtime.flow_running ? "true" : "false";

        setModeHighlight(String(runtime.mode || ""));
      }}

      async function fetchHealth() {{
        const response = await fetch("/health", {{
          method: "GET",
          headers: {{
            "Accept": "application/json"
          }}
        }});

        let data = {{}};
        try {{
          data = await response.json();
        }} catch (_err) {{
          throw new Error("health refresh failed");
        }}

        return data;
      }}

      async function settleModeRefresh(expectedMode) {{
        const deadline = Date.now() + 6000;
        let last = null;

        while (Date.now() < deadline) {{
          try {{
            last = await fetchHealth();
            updateStatusUi(last);

            const runtime = (last && last.runtime) || {{}};
            const mode = String(runtime.mode || "");
            const running = !!runtime.flow_running;

            if (expectedMode) {{
              if (mode === expectedMode && !running) {{
                return;
              }}
            }} else if (!running) {{
              return;
            }}
          }} catch (_err) {{
          }}

          await new Promise(function (resolve) {{
            window.setTimeout(resolve, 350);
          }});
        }}

        if (last) {{
          updateStatusUi(last);
        }}
      }}

      async function postJson(url, payload) {{
        const response = await fetch(url, {{
          method: "POST",
          headers: {{
            "Accept": "application/json",
            "Content-Type": "application/json"
          }},
          body: JSON.stringify(payload || {{}})
        }});

        let data = {{}};
        try {{
          data = await response.json();
        }} catch (_err) {{
          data = {{}};
        }}

        if (!response.ok || !data.ok) {{
          throw new Error(String(data.error || data.reason || "request failed"));
        }}

        return data;
      }}

      async function handleTapButton(el) {{
        const key = el.getAttribute("data-key") || "";
        const refresh = el.getAttribute("data-refresh") === "1";
        const targetMode = el.getAttribute("data-target-mode") || "";

        setPressed(el, true);
        try {{
          await postJson("/remote/tap", {{
            key: key
          }});

          if (navigator.vibrate) {{
            navigator.vibrate(10);
          }}

          if (refresh) {{
            if (targetMode) {{
              setModeHighlight(targetMode);
            }}
            await settleModeRefresh(targetMode);
          }}
        }} catch (err) {{
          showToast(String(err), true);
        }} finally {{
          window.setTimeout(function () {{
            setPressed(el, false);
          }}, 90);
        }}
      }}

      async function sendEdge(key, edge) {{
        return await postJson("/remote/edge", {{
          key: key,
          edge: edge
        }});
      }}

      async function releaseEdgeButton(el) {{
        if (!el) return;
        const key = el.getAttribute("data-key") || "";
        if (!pressedEdgeButtons.has(el)) return;

        pressedEdgeButtons.delete(el);
        setPressed(el, false);

        try {{
          await sendEdge(key, "up");
        }} catch (err) {{
          showToast(String(err), true);
        }}
      }}

      function attachTap(el) {{
        el.addEventListener("click", function () {{
          handleTapButton(el);
        }});
      }}

      function attachEdge(el) {{
        const key = el.getAttribute("data-key") || "";

        el.addEventListener("pointerdown", async function (event) {{
          event.preventDefault();
          if (pressedEdgeButtons.has(el)) return;

          pressedEdgeButtons.set(el, true);
          setPressed(el, true);

          try {{
            el.setPointerCapture(event.pointerId);
          }} catch (_err) {{
          }}

          try {{
            await sendEdge(key, "down");
          }} catch (err) {{
            pressedEdgeButtons.delete(el);
            setPressed(el, false);
            showToast(String(err), true);
          }}
        }});

        const finish = function () {{
          releaseEdgeButton(el);
        }};

        el.addEventListener("pointerup", finish);
        el.addEventListener("pointercancel", finish);
        el.addEventListener("pointerleave", function (event) {{
          if (event.buttons === 0) {{
            finish();
          }}
        }});
      }}

      document.querySelectorAll(".remote-button").forEach(function (el) {{
        const kind = el.getAttribute("data-kind");
        if (kind === "edge") {{
          attachEdge(el);
        }} else {{
          attachTap(el);
        }}
      }});

      window.addEventListener("blur", function () {{
        Array.from(pressedEdgeButtons.keys()).forEach(function (el) {{
          releaseEdgeButton(el);
        }});
      }});

      document.addEventListener("visibilitychange", function () {{
        if (document.hidden) {{
          Array.from(pressedEdgeButtons.keys()).forEach(function (el) {{
            releaseEdgeButton(el);
          }});
        }}
      }});

      updateStatusUi({{
        status: initialState.status,
        runtime: {{
          mode: initialState.mode,
          last_flow: initialState.last_flow,
          flow_running: initialState.flow_running
        }}
      }});
    }})();
  </script>
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
    
    @staticmethod
    def _parse_iso_dt(value: object):
        from datetime import datetime

        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    def _format_flow_started(self, iso_value: object) -> str:
        dt = self._parse_iso_dt(iso_value)
        if dt is None:
            return ""
        return dt.strftime("%d-%m-%Y %H:%M:%S")

    def _format_time_only(self, iso_value: object) -> str:
        dt = self._parse_iso_dt(iso_value)
        if dt is None:
            return ""
        return dt.strftime("%H:%M:%S.%f")[:-3]

    @staticmethod
    def _prettify_trigger(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""

        mapping = {
            "http.flow": "Web UI flow",
            "http.mode": "Web UI mode set",
            "http.command": "Web UI command",
            "startup_reconcile": "Startup reconcile",
        }
        if text in mapping:
            return mapping[text]

        if text.startswith("device_state_change."):
            suffix = text.split(".", 1)[1].replace("_", " ").strip()
            return f"Device state: {suffix}"

        if text.startswith("remote."):
            suffix = text.split(".", 1)[1].replace("_", " ").strip()
            return f"Remote: {suffix}"

        if text.startswith("flow."):
            suffix = text.split(".", 1)[1].replace("_", " ").strip()
            return f"Flow: {suffix}"

        return text.replace("_", " ")

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
    
    async def _handle_remote_edge(self, request: web.Request) -> web.Response:
        if self._dispatcher is None:
            return web.json_response({"ok": False, "error": "dispatcher unavailable"}, status=503)

        payload = await self._maybe_json(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "json body required"}, status=400)

        key = self._norm_remote_key(payload.get("key"))
        edge = self._norm_remote_edge(payload.get("edge"))
        trigger = str(payload.get("trigger") or "http.remote.edge")

        key_error = self._validate_remote_key(key)
        if key_error:
            return web.json_response({"ok": False, "error": key_error}, status=400)

        if edge not in {"down", "up"}:
            return web.json_response({"ok": False, "error": "edge must be 'down' or 'up'"}, status=400)

        try:
            await self._dispatcher.on_usb_edge(key, edge)
        except Exception as exc:
            return web.json_response(
                {
                    "ok": False,
                    "error": str(exc),
                    "key": key,
                    "edge": edge,
                    "trigger": trigger,
                },
                status=500,
            )

        return web.json_response(
            {
                "ok": True,
                "action": "remote_edge",
                "key": key,
                "edge": edge,
                "trigger": trigger,
            }
        )

    async def _handle_remote_tap(self, request: web.Request) -> web.Response:
        if self._dispatcher is None:
            return web.json_response({"ok": False, "error": "dispatcher unavailable"}, status=503)

        payload = await self._maybe_json(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "json body required"}, status=400)

        key = self._norm_remote_key(payload.get("key"))
        trigger = str(payload.get("trigger") or "http.remote.tap")

        key_error = self._validate_remote_key(key)
        if key_error:
            return web.json_response({"ok": False, "error": key_error}, status=400)

        hold_ms_raw = payload.get("hold_ms", 80)
        try:
            hold_ms = int(hold_ms_raw)
        except Exception:
            return web.json_response({"ok": False, "error": "hold_ms must be an integer"}, status=400)

        hold_ms = max(20, min(hold_ms, 2000))

        try:
            await self._dispatcher.on_usb_edge(key, "down")
            try:
                await asyncio.sleep(hold_ms / 1000.0)
            finally:
                await self._dispatcher.on_usb_edge(key, "up")
        except Exception as exc:
            return web.json_response(
                {
                    "ok": False,
                    "error": str(exc),
                    "key": key,
                    "hold_ms": hold_ms,
                    "trigger": trigger,
                },
                status=500,
            )

        return web.json_response(
            {
                "ok": True,
                "action": "remote_tap",
                "key": key,
                "hold_ms": hold_ms,
                "trigger": trigger,
            }
        )

    async def _maybe_json(self, request: web.Request) -> dict | None:
        if request.content_length in (None, 0):
            return {}
        try:
            data = await request.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _norm_remote_key(value: object) -> str:
        key = str(value or "").strip()
        return key

    @staticmethod
    def _norm_remote_edge(value: object) -> str:
        edge = str(value or "").strip().lower()
        return edge

    def _validate_remote_key(self, key: str) -> str | None:
        if not key:
            return "key required"
        if not key.startswith("rem_"):
            return "key must start with rem_"
        if self._dispatcher is None:
            return "dispatcher unavailable"

        known = set(getattr(self._dispatcher, "scancode_map", {}).values())
        if known and key not in known:
            return f"unknown remote key: {key}"

        return None

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
    
    @staticmethod
    def _read_cpu_temp_c() -> float | None:
        candidates = [
            "/sys/class/thermal/thermal_zone0/temp",
        ]

        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                value = int(raw)
                # Raspberry Pi thermal_zone temp is usually millidegrees C
                if value > 1000:
                    return round(value / 1000.0, 1)
                return round(float(value), 1)
            except Exception:
                continue

        return None
    
    @staticmethod
    def _primary_ip() -> str | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return str(s.getsockname()[0])
            finally:
                s.close()
        except Exception:
            try:
                ip = socket.gethostbyname(socket.gethostname())
                return None if ip.startswith("127.") else ip
            except Exception:
                return None

    @staticmethod
    def _read_throttling() -> dict:
        import subprocess

        try:
            cp = subprocess.run(
                ["vcgencmd", "get_throttled"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            out = (cp.stdout or "").strip()
            if cp.returncode != 0 or "throttled=" not in out:
                return {
                    "available": False,
                    "raw": None,
                    "status": "unknown",
                }

            raw = out.split("throttled=", 1)[1].strip()
            value = int(raw, 16)

            data = {
                "available": True,
                "raw": raw,
                "undervoltage_now": bool(value & (1 << 0)),
                "freq_capped_now": bool(value & (1 << 1)),
                "throttled_now": bool(value & (1 << 2)),
                "temp_limit_now": bool(value & (1 << 3)),
                "undervoltage_occurred": bool(value & (1 << 16)),
                "freq_capped_occurred": bool(value & (1 << 17)),
                "throttled_occurred": bool(value & (1 << 18)),
                "temp_limit_occurred": bool(value & (1 << 19)),
            }

            any_flag = any(v for k, v in data.items() if k.endswith("_now") or k.endswith("_occurred"))
            data["status"] = "warning" if any_flag else "ok"
            return data

        except Exception:
            return {
                "available": False,
                "raw": None,
                "status": "unknown",
            }

    def _system_snapshot(self) -> dict:
        hostname = socket.gethostname()
        primary_ip = self._primary_ip()
        
        system_uptime_s = self._read_system_uptime_s()
        process_uptime_s = time.monotonic() - self._process_start_monotonic
        cpu_temp_c = self._read_cpu_temp_c()
        throttling = self._read_throttling()

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
            "hostname": hostname,
            "primary_ip": primary_ip,
            "system_uptime_s": int(system_uptime_s) if system_uptime_s is not None else None,
            "system_uptime_human": self._format_duration(system_uptime_s),
            "process_uptime_s": int(process_uptime_s),
            "process_uptime_human": self._format_duration(process_uptime_s),
            "cpu_temp_c": cpu_temp_c,
            "throttling": throttling,
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
                    "initialised": bool(s.initialised),
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
        
        ha = {
            "overall_status": status,
            "current_mode": runtime_state.get("mode"),
            "last_flow": runtime_state.get("last_flow"),
            "flow_running": runtime_state.get("flow_running"),
            "last_result": runtime_state.get("last_result"),
            "last_error": runtime_state.get("last_error"),
            "degraded_reasons": degraded_reasons,
            "binary_sensors": {
                "runtime_error": bool(runtime_state.get("error")),
                "flow_running": bool(runtime_state.get("flow_running")),
                "ble_ready": bool(ble_state.get("link_ready")),
                "tv_on": (tv_state.get("details") or {}).get("presence_on"),
                "speaker_ready": bool(speaker_state.get("link_ready")),
            },
            "domains": {
                "usb": usb_state.get("status"),
                "ble": ble_state.get("status"),
                "tv": tv_state.get("status"),
                "speaker": speaker_state.get("status"),
            },
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
            "ha": ha,
        }