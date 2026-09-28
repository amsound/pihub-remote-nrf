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

export const title = (s) => (s ? String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "—");

export const MODE = { watch: "Watch", listen: "Listen", power_off: "Off" };

// A change time (Unix seconds) as a clock time: "10:28", "yesterday 22:14", "28 Sep 10:28".
export function clock(ts) {
  if (ts == null) return "—";
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
  if (s == null) return "—";
  const u = (t) => (small ? `<small>${t}</small>` : t);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}${u("d")} ${h}${u("h")}`;
  if (h) return `${h}${u("h")} ${m}${u("m")}`;
  return `${m}${u("m")}`;
}

export const dotClass = (state) =>
  state === "ok" ? "ok" : state === "degraded" ? "warn" : state === "disabled" || state == null ? "" : "bad";

// Top bar shared by Status, Settings and History.
export function renderTopBar(el, current, name) {
  const tab = (href, label) => `<a href="${href}" class="${current === href ? "on" : ""}">${label}</a>`;
  el.querySelector("h1").textContent = name || "PiHub";
  el.querySelector("nav.tabs").innerHTML =
    tab("/status", "Status") + tab("/remote", "Remote") + tab("/settings", "Settings") + tab("/history", "History");
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
