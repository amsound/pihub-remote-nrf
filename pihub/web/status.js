import { getJSON, postJSON, esc, title, MODE, FLOW, clock, duration, dotClass, renderTopBar, renderFooter, roomStrip, poll, triggerText } from "/web/common.js";

const ICONS = {
  watch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/></svg>',
  listen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>',
  power_off: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>',
  speaker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="14" r="4"/><circle cx="12" cy="6" r="1"/></svg>',
  bluetooth: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7l10 10-5 5V2l5 5L7 17"/></svg>',
  remote: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="20" rx="3"/><circle cx="12" cy="8" r="1.5"/><path d="M11 13h2M11 16h2"/></svg>',
};
const TV_BACKEND = { frame: "Samsung Frame", samsung: "Samsung" };
const SPEAKER_BACKEND = { audiopro: "Audio Pro", samsung_soundbar: "Samsung soundbar" };
const SPEAKER_SOURCE = { hdmi: "HDMI", wifi: "Wi-Fi", airplay: "AirPlay", "multiroom-secondary": "Multiroom", optical: "Optical", bluetooth: "Bluetooth", "line-in": "Line in", idle: "Idle" };
const SEEN_VIA = {
  ip_control_power_on: "IP control", ip_control_power_off: "IP control", ip_control_power: "IP control",
  msearch: "Network search", ssdp_alive: "Network announcement", ssdp_byebye: "Network announcement",
  probe_http_up: "Probe", probe_http_down: "Probe",
};
const $ = (id) => document.getElementById(id);
const since = (ts) => (ts ? `since ${clock(ts)}` : "");
const PLAYBACK = { play: "Playing", playing: "Playing", pause: "Paused", paused: "Paused", stop: "Stopped", stopped: "Stopped", load: "Loading", loading: "Loading", idle: "Idle" };
const cap = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t);
const rooms = roomStrip(document.getElementById("rooms"));

let current = null;
let pendingFlow = false;

function render(d) {
  current = d;
  renderTopBar(document.querySelector(".top"), "/status", d.name);
  document.title = `${d.name} · PiHub`;
  $("overall").textContent = d.status === "ok" ? "OK" : "Not OK";
  $("overall-dot").className = "dot " + dotClass(d.status);
  $("overall-pill").title = d.problems.length ? d.problems.join("\n") : "";
  $("power-pill").hidden = !d.system.throttled;

  $("mode").textContent = MODE[d.mode] || title(d.mode);
  $("trigger").textContent = triggerText(d.last_trigger) || "None yet";
  $("trigger-at").textContent = d.last_trigger_at ? clock(d.last_trigger_at) : "";

  const s = d.system;
  $("cpu").textContent = s.cpu_temp_c != null ? `${Math.round(s.cpu_temp_c)}°C` : "n/a";
  $("mem").textContent = s.memory_used_pct != null ? `${s.memory_used_pct}%` : "n/a";
  $("up").textContent = duration(s.uptime_s, false);
  $("pup").textContent = duration(s.pihub_uptime_s, false);

  const tv = d.tv;
  const tvHead = `<div class="dev-head">${ICONS.watch}<h2>TV</h2>${tv.backend ? `<span class="tag">${TV_BACKEND[tv.backend] || esc(tv.backend)}</span>` : ""}</div>`;
  $("tv").innerHTML = !tv.backend
    ? `${tvHead}<div class="dev-body"><div class="state"><span class="dot"></span>Not configured</div></div>`
    : tv.on == null
      // Fresh start: nothing has reported the TV's state yet.
      ? `${tvHead}<div class="dev-body"><div class="state"><span class="dot"></span>Not seen yet</div>
         ${tv.error ? `<div class="err">${esc(tv.error)}</div>` : ""}</div>`
      : `${tvHead}<div class="dev-body">
         <div class="state"><span class="dot ${tv.on ? "ok" : ""}"></span>${tv.on ? "On" : "Off"}<span class="since">${since(tv.changed_at)}</span></div>
         <dl class="kv">
           <dt>Seen via</dt><dd>${esc(SEEN_VIA[tv.on_via] || title(tv.on_via))}</dd>
           <dt>Control</dt><dd>${tv.control_ready ? "Ready" : tv.on ? "Not ready" : "Idle, TV off"}</dd>
         </dl>
         ${tv.error ? `<div class="err">${esc(tv.error)}</div>` : ""}</div>`;

  const sp = d.speaker;
  const playback = PLAYBACK[sp.playback] || cap(sp.playback);
  const spState = sp.state === "disabled" ? "Not configured"
    : sp.state !== "ok" ? "Unavailable"
    : sp.source ? (SPEAKER_SOURCE[sp.source] || cap(title(sp.source))) + (playback ? ` · ${playback}` : "")
    : "Idle";
  $("speaker").innerHTML = `
    <div class="dev-head">${ICONS.speaker}<h2>Speaker</h2><span class="tag">${SPEAKER_BACKEND[sp.backend] || esc(sp.backend || "")}</span></div>
    <div class="dev-body">
      <div class="state"><span class="dot ${dotClass(sp.state)}"></span>${spState}<span class="since">${since(sp.changed_at)}</span></div>
      <div class="vol"><span class="bar"><i style="width:${sp.volume ?? 0}%"></i></span><span>${sp.muted ? "Muted" : sp.volume != null ? sp.volume + "%" : ""}</span></div>
      ${sp.error ? `<div class="err">${esc(sp.error)}</div>` : ""}
    </div>`;

  const a = d.apple_tv;
  const atv = !a.dongle ? "Bluetooth dongle not found"
    : a.connected ? `Connected${a.interval_ms ? `, ${a.interval_ms} ms interval` : ""}`
    : a.advertising ? "Advertising, waiting for the Apple TV"
    : "Not connected";
  const drop = a.last_disconnect_reason && !a.connected ? `, last drop reason ${esc(a.last_disconnect_reason)}` : "";
  const r = d.remote;
  const rem = !r.receiver ? "USB receiver not found"
    : !r.paired ? "Receiver found, remote not paired"
    : `Paired${r.grabbed ? "" : ", input not exclusive"}${r.battery ? `, battery ${esc(r.battery)}` : ""}`;
  $("connections").innerHTML = `
    <div class="conn">${ICONS.bluetooth}<span class="n">Apple TV</span><span class="dot ${dotClass(a.state)}"></span><span class="d">${atv}${drop}</span></div>
    <div class="conn">${ICONS.remote}<span class="n">Harmony remote</span><span class="dot ${dotClass(r.state)}"></span><span class="d">${rem}</span></div>`;

  if (!pendingFlow) $("seg").dataset.mode = d.mode || "";
  renderFooter(document.querySelector(".foot"), d);
  rooms.update(d);
}

// ---- Recent flows, with what triggered each ----
async function refreshFlows() {
  const { flows = [] } = await getJSON("/history/flows?limit=6");
  $("flows").innerHTML = flows.length ? flows.map((f) => {
    const secs = f.duration_ms != null ? (f.duration_ms / 1000).toFixed(1) + "s" : "";
    const dot = f.result === "running" ? "" : f.result === "ok" ? "ok" : "bad";
    return `<div class="flow"><span class="when">${clock(f.ts_started)}</span><span class="name">${esc(FLOW[f.flow_name] || title(f.flow_name))}</span>
      <span class="muted">${esc(triggerText(f.trigger))}</span><span class="ms">${secs} <span class="dot ${dot}" style="vertical-align:middle;margin-left:4px"></span></span></div>`;
  }).join("") : '<div class="muted">No flows yet</div>';
}

// ---- Control ----
const statusPoll = poll(async () => render(await getJSON("/api/status")), 3000);
poll(refreshFlows, 10000);

function note(text, cls = "") {
  $("action-note").className = "note " + cls;
  $("action-note").textContent = text;
}

document.querySelectorAll("#seg button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (pendingFlow) return;
    pendingFlow = true;
    btn.classList.add("pending");
    note("");
    try {
      const { ok, data } = await postJSON(`/flow/run/${btn.dataset.flow}`, { trigger: "http.status" });
      if (!ok) note(`${btn.textContent}: ${data.error || data.reason || "failed"}`, "bad");
    } catch {
      note(`${btn.textContent}: no response`, "bad");
    } finally {
      btn.classList.remove("pending");
      pendingFlow = false;
      statusPoll.now();
      refreshFlows();
    }
  });
});

document.querySelectorAll(".rows [data-mode], .rows [data-post]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    const url = btn.dataset.mode ? `/mode/set/${btn.dataset.mode}` : btn.dataset.post;
    const label = btn.closest(".row").querySelector(".k").textContent + " " + btn.textContent;
    btn.disabled = true;
    try {
      const { ok, data } = await postJSON(url, { trigger: "http.status" });
      note(ok ? `${label}: done` : `${label}: ${data.error || data.reason || "failed"}`, ok ? "ok" : "bad");
    } catch {
      note(`${label}: no response`, "bad");
    } finally {
      btn.disabled = false;
      statusPoll.now();
    }
  });
});
