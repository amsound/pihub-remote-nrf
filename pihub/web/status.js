import { getJSON, postJSON, esc, title, MODE, clock, duration, dotClass, renderTopBar, poll } from "/web/common.js";

const ICONS = {
  watch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/></svg>',
  listen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>',
  power_off: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>',
  speaker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="14" r="4"/><circle cx="12" cy="6" r="1"/></svg>',
  appletv: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="7" width="16" height="10" rx="2"/><path d="M9 20h6"/></svg>',
  remote: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="20" rx="3"/><circle cx="12" cy="8" r="1.5"/><path d="M11 13h2M11 16h2"/></svg>',
};
const SOURCE = { remote: "Remote", device: "Automatic", web: "Web", startup: "Startup", flow: "Flow" };
const TV_BACKEND = { frame: "Samsung Frame", samsung: "Samsung" };
const SPEAKER_BACKEND = { audiopro: "Audio Pro", samsung_soundbar: "Samsung soundbar" };
const SPEAKER_SOURCE = { hdmi: "HDMI", wifi: "Wi-Fi", airplay: "AirPlay", "multiroom-secondary": "Multiroom", optical: "Optical", bluetooth: "Bluetooth", "line-in": "Line in", idle: "Idle" };
const SEEN_VIA = {
  ip_control_power_on: "IP control", ip_control_power_off: "IP control", ip_control_power: "IP control",
  msearch: "network search", ssdp_alive: "network announcement", ssdp_byebye: "network announcement",
  probe_http_up: "probe", probe_http_down: "probe",
};
const $ = (id) => document.getElementById(id);

let current = null;

function triggerSource(d) {
  // "remote.rem_mode_2" etc: say who, and the flow it led to (if any yet).
  const who = SOURCE[d.last_trigger_source] || title(d.last_trigger_source);
  return d.last_flow ? `${who} · ${MODE[d.last_flow] || title(d.last_flow)}` : who;
}

function render(d) {
  current = d;
  renderTopBar(document.querySelector(".top"), "/status", d.name);
  document.title = `${d.name} · PiHub`;
  $("overall").textContent = d.status === "ok" ? "All good" : "Needs attention";
  $("overall-dot").className = "dot " + dotClass(d.status);

  $("mode").textContent = MODE[d.mode] || title(d.mode);
  $("mode-icon").innerHTML = ICONS[d.mode] || "";
  $("mode-sub").textContent = d.flow_running
    ? "Flow running…"
    : d.last_result === "failed"
      ? `Last flow failed: ${MODE[d.last_flow] || title(d.last_flow)}`
      : `Last flow: ${MODE[d.last_flow] || title(d.last_flow)}`;
  $("trigger").textContent = d.last_trigger ? triggerSource(d) : "—";
  $("trigger-sub").textContent = d.last_trigger ? `${clock(d.last_trigger_at)} · ${d.last_trigger}` : "";

  const s = d.system;
  $("cpu").innerHTML = s.cpu_temp_c != null ? `${Math.round(s.cpu_temp_c)}<small>°C</small>` : "—";
  $("load").textContent = s.load_1m != null ? `load ${s.load_1m.toFixed(2)}` : "";
  $("mem").innerHTML = s.memory_used_pct != null ? `${s.memory_used_pct}<small>%</small>` : "—";
  $("power").innerHTML = s.throttled ? '<span style="color:var(--bad)">Undervoltage</span>' : s.throttled === false ? "Power OK" : "";
  $("up").innerHTML = duration(s.uptime_s);
  $("pup").innerHTML = duration(s.pihub_uptime_s);

  const tv = d.tv;
  $("tv").innerHTML = !tv.backend
    ? `<div class="dev-head">${ICONS.watch}<h2>TV</h2></div><div class="muted">No TV configured</div>`
    : `<div class="dev-head">${ICONS.watch}<h2>TV</h2><span class="tag">${TV_BACKEND[tv.backend] || tv.backend}</span></div>
       <div class="state"><span class="dot ${tv.on ? "ok" : ""}"></span>${tv.on == null ? "Unknown" : tv.on ? "On" : "Off"}</div>
       <dl class="kv">
         <dt>Since</dt><dd>${clock(tv.changed_at)}</dd>
         <dt>Seen via</dt><dd>${esc(SEEN_VIA[tv.on_via] || tv.on_via || "—")}</dd>
         <dt>Control</dt><dd>${tv.control_ready ? "Ready" : tv.on ? "Not ready" : "Idle (TV off)"}</dd>
       </dl>
       ${tv.error ? `<div class="err">${esc(tv.error)}</div>` : ""}`;

  const sp = d.speaker;
  const playing = sp.playback === "play" || sp.playback === "playing";
  const spState = sp.state === "disabled" ? "Not configured"
    : sp.state !== "ok" ? "Unavailable"
    : sp.source ? (SPEAKER_SOURCE[sp.source] || title(sp.source)) + (playing ? " · playing" : "")
    : "Idle";
  $("speaker").innerHTML = `
    <div class="dev-head">${ICONS.speaker}<h2>Speaker</h2><span class="tag">${SPEAKER_BACKEND[sp.backend] || sp.backend || "—"}</span></div>
    <div class="state"><span class="dot ${dotClass(sp.state)}"></span>${spState}</div>
    <div class="vol"><span class="bar"><i style="width:${sp.volume ?? 0}%"></i></span><span>${sp.muted ? "Muted" : sp.volume != null ? sp.volume + "%" : "—"}</span></div>
    <dl class="kv"><dt>Since</dt><dd>${clock(sp.changed_at)}</dd></dl>
    ${sp.error ? `<div class="err">${esc(sp.error)}</div>` : ""}`;

  $("appletv").innerHTML = `${ICONS.appletv}<div><div class="t">Apple TV</div><div class="s">${d.apple_tv.connected ? "Bluetooth connected" : "Not connected"}</div></div><span class="dot ${dotClass(d.apple_tv.state)}"></span>`;
  const r = d.remote;
  $("remote").innerHTML = `${ICONS.remote}<div><div class="t">Harmony remote</div><div class="s">${r.paired ? "Paired" : r.receiver ? "Receiver, no remote" : "No receiver"}${r.battery ? " · battery " + esc(r.battery) : ""}</div></div><span class="dot ${dotClass(r.state)}"></span>`;

  $("problems").innerHTML = d.problems.length ? d.problems.map(esc).join("<br>") : "None";
  $("built").textContent = `${d.host} · ${d.ip || ""} · built ${d.built}`;

  renderRooms(d);
}

// ---- Room strip: this room's status plus each sibling's /api/status ----
const roomData = new Map();

function renderRooms(d) {
  const el = $("rooms");
  if (!d.rooms || !d.rooms.length) { el.innerHTML = ""; return; }
  el.innerHTML = d.rooms.map((room) => {
    const here = new URL(room.url).host === location.host;
    const rd = here ? d : roomData.get(room.url);
    const dot = rd ? dotClass(rd.status) : "";
    const mode = rd ? MODE[rd.mode] || title(rd.mode) : "offline";
    return `<a class="room ${here ? "here" : ""}" href="${esc(room.url)}/status"><span class="dot ${dot}"></span>${esc(room.name)}<span class="m">${esc(mode)}</span></a>`;
  }).join("");
}

async function refreshRooms() {
  if (!current || !current.rooms) return;
  await Promise.all(current.rooms
    .filter((room) => new URL(room.url).host !== location.host)
    .map(async (room) => {
      try { roomData.set(room.url, await getJSON(`${room.url}/api/status`, { timeoutMs: 2500 })); }
      catch { roomData.delete(room.url); }
    }));
  renderRooms(current);
}

// ---- Recent flows ----
async function refreshFlows() {
  const { flows = [] } = await getJSON("/history/flows?limit=5");
  $("flows").innerHTML = flows.length ? flows.map((f) => {
    const source = f.trigger?.startsWith("remote.") ? "Remote"
      : f.trigger?.startsWith("device_state_change.") ? "Automatic"
      : f.trigger?.startsWith("http.") ? "Web" : title(f.trigger);
    const ok = f.result === "ok";
    const secs = f.duration_ms != null ? (f.duration_ms / 1000).toFixed(1) + "s" : "…";
    return `<div class="flow"><span class="when">${clock(f.ts_started)}</span><span class="name">${esc(MODE[f.flow_name] || title(f.flow_name))}</span>
      <span class="by">${esc(source)}</span><span class="ms">${secs} <span class="dot ${f.result === "running" ? "" : ok ? "ok" : "bad"}" style="vertical-align:middle;margin-left:4px"></span></span></div>`;
  }).join("") : '<div class="muted">No flows yet</div>';
}

// ---- Actions ----
const statusPoll = poll(async () => render(await getJSON("/api/status")), 3000);
poll(refreshFlows, 10000);
poll(refreshRooms, 10000);

document.querySelectorAll("[data-flow],[data-mode],[data-post]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    const url = btn.dataset.flow ? `/flow/run/${btn.dataset.flow}`
      : btn.dataset.mode ? `/mode/set/${btn.dataset.mode}` : btn.dataset.post;
    const note = $("action-note");
    btn.disabled = true;
    note.className = "note";
    note.textContent = `${btn.textContent.trim()}…`;
    try {
      const { ok, data } = await postJSON(url, { trigger: "http.status" });
      note.className = "note " + (ok ? "ok" : "bad");
      note.textContent = ok ? `${btn.textContent.trim()}: done` : `${btn.textContent.trim()}: ${data.error || data.reason || "failed"}`;
    } catch {
      note.className = "note bad";
      note.textContent = `${btn.textContent.trim()}: no response`;
    } finally {
      btn.disabled = false;
      statusPoll.now();
      refreshFlows();
    }
  });
});
