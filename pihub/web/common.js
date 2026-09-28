// Shared helpers for the PiHub pages.

export async function getJSON(url, { timeoutMs = 4000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function postJSON(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  return { ok: res.ok && data.ok !== false, status: res.status, data };
}

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const title = (s) => (s ? String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "");

export const MODE = { watch: "Watch", listen: "Listen", power_off: "Off" };
// Flow names as shown: the automatic variants read the same, the trigger says "Automatic".
export const FLOW = { ...MODE, watch_signal: "Watch", listen_signal: "Listen" };

// A change time (Unix seconds) as a clock time: "10:28", "yesterday 22:14", "28 Sep 10:28".
export function clock(ts) {
  if (ts == null) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((day(now) - day(d)) / 86400000);
  if (diffDays === 0) return hm;
  if (diffDays === 1) return `yesterday ${hm}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${hm}`;
}

// Uptime-style durations: "52d 7h", "2h 25m", "14m".
export function duration(s, small = true) {
  if (s == null) return small ? "<small>n/a</small>" : "n/a";
  const u = (t) => (small ? `<small>${t}</small>` : t);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}${u("d")} ${h}${u("h")}`;
  if (h) return `${h}${u("h")} ${m}${u("m")}`;
  return `${m}${u("m")}`;
}

export const dotClass = (state) =>
  state === "ok" ? "ok" : state === "degraded" ? "warn" : state === "disabled" || state == null ? "" : "bad";

// Theme: "" follows the system; "light" / "dark" are remembered per browser.
const THEME_KEY = "pihub-theme";
export function getTheme() {
  try { return localStorage.getItem(THEME_KEY) || ""; } catch { return ""; }
}
export function setTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  try { theme ? localStorage.setItem(THEME_KEY, theme) : localStorage.removeItem(THEME_KEY); } catch { /* private mode */ }
}
const THEME_ICONS = {
  "": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></svg>',
  light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>',
};
const THEME_LABELS = { "": "Follow system", light: "Light", dark: "Dark" };

// Top bar shared by Status, Settings and History: room name with a health dot
// (hover it for any problems), the room strip, and the menu.
export function renderTopBar(el, current, d) {
  const tab = (href, label) => `<a href="${href}" class="${current === href ? "on" : ""}">${label}</a>`;
  const h1 = el.querySelector("h1");
  const problems = [...(d.problems || []), ...(d.system?.throttled ? ["Undervoltage"] : [])];
  const health = d.system?.throttled ? "bad" : dotClass(d.status);
  h1.innerHTML = `${esc(d.name || "PiHub")}<span class="dot ${health}" title="${esc(problems.join("\n") || "OK")}"></span>`;
  const nav = el.querySelector("nav.tabs");
  if (!nav.children.length) {
    nav.innerHTML = tab("/status", "Status") + tab("/remote", "Remote") + tab("/settings", "Settings") + tab("/history", "History");
  }
}

// Footer shared by Status, Settings and History: host details and the theme switch.
export function renderFooter(el, d) {
  el.querySelector(".info").textContent = d ? `${d.host}, ${d.ip || ""}, built ${d.built}` : "";
  let theme = el.querySelector(".theme");
  if (!theme.children.length) {
    theme.innerHTML = Object.keys(THEME_ICONS)
      .map((t) => `<button type="button" data-theme="${t}" title="${THEME_LABELS[t]}" aria-label="${THEME_LABELS[t]}">${THEME_ICONS[t]}</button>`).join("");
    theme.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      setTheme(b.dataset.theme);
      theme.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    });
  }
  const currentTheme = getTheme();
  theme.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.theme === currentTheme));
}

// What set things off, in words: "Remote: Watch", "Automatic: Watch", "Web remote".
const REMOTE_KEYS = { rem_mode_1: "Listen", rem_mode_2: "Watch", rem_power_off: "Off" };
export function triggerText(trigger) {
  const t = String(trigger || "");
  if (!t) return "";
  if (t.startsWith("remote.")) return REMOTE_KEYS[t.slice(7)] ? `Remote: ${REMOTE_KEYS[t.slice(7)]}` : "Remote";
  if (t === "device_state_change.watch") return "Automatic: Watch";
  if (t === "device_state_change.listen") return "Automatic: Listen";
  if (t === "http.remote.flow" || t === "http.remote") return "Web remote";
  if (t === "http.status") return "Status page";
  if (t.startsWith("http.")) return "HTTP request";
  if (t.startsWith("startup")) return "Startup";
  return title(t.replace(/\./g, " "));
}

// Room strip (Status, Settings, History): every room's mode and health, linking to
// the same page on that room's PiHub. This room's entry uses the page's own status;
// the others are read from their /api/status every 10 s.
export function roomStrip(el) {
  const others = new Map();
  let current = null;

  const render = () => {
    if (!current || !current.rooms || !current.rooms.length) { el.innerHTML = ""; return; }
    el.innerHTML = current.rooms.map((room) => {
      const here = new URL(room.url).host === location.host;
      const rd = here ? current : others.get(room.url);
      const mode = rd ? MODE[rd.mode] || title(rd.mode) : "Offline";
      return `<a class="room ${here ? "here" : ""}" href="${esc(room.url)}${location.pathname}"><span class="dot ${rd ? dotClass(rd.status) : ""}"></span>${esc(room.name)}<span class="m">${esc(mode)}</span></a>`;
    }).join("");
  };

  const siblings = poll(async () => {
    if (!current || !current.rooms) return;
    await Promise.all(current.rooms
      .filter((room) => new URL(room.url).host !== location.host)
      .map(async (room) => {
        try { others.set(room.url, await getJSON(`${room.url}/api/status`, { timeoutMs: 2500 })); }
        catch { others.delete(room.url); }
      }));
    render();
  }, 10000);

  return {
    update(d) {
      const first = !current;
      current = d;
      render();
      if (first) siblings.now(); // fetch the other rooms straight away
    },
  };
}

// Poll only while the page is visible.
export function poll(fn, everyMs) {
  let timer = null;
  const tick = async () => {
    try { await fn(); } catch { /* next tick retries */ }
    timer = setTimeout(tick, everyMs);
  };
  const start = () => { if (!timer) tick(); };
  const stop = () => { clearTimeout(timer); timer = null; };
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  start();
  return { now: () => { stop(); start(); } };
}
