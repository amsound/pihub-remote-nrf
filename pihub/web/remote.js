import { getJSON, postJSON, poll } from "/web/common.js";

const root = document.documentElement;
const MODE_ATTR = { power_off: "off", listen: "listen", watch: "watch" };

// ---- Mode: follow /api/status while the page is visible ----
const statusPoll = poll(async () => {
  const d = await getJSON("/api/status", { timeoutMs: 2500 });
  root.dataset.mode = MODE_ATTR[d.mode] || "off";
  document.getElementById("room").textContent = d.name || "";
  document.title = d.name ? `${d.name} Remote` : "Remote";
  document.getElementById("mute").classList.toggle("on", !!d.speaker?.muted);
}, 2000);

function haptic() {
  if (navigator.vibrate) navigator.vibrate(8);
}

// Off / Listen / Watch run the flow (again, if already in that mode: the same
// as the physical remote, e.g. to restart the radio). The selector moves once
// the flow has finished and the status says so.
document.querySelectorAll(".seg button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (btn.classList.contains("pending")) return;
    haptic();
    btn.classList.add("pending");
    try {
      await postJSON(`/flow/run/${btn.dataset.flow}`, { trigger: "http.remote.flow" });
    } catch { /* the status poll shows the real state */ }
    btn.classList.remove("pending");
    statusPoll.now();
  });
});

// ---- Keys: send the press and the release, in order, so holding volume repeats ----
let queue = Promise.resolve();
function sendEdge(key, edge) {
  queue = queue
    .then(() => postJSON("/remote/edge", { key, edge, trigger: "http.remote" }))
    .catch(() => {});
}

const held = new Set();
const pad = document.getElementById("pad");

function bindKey(el) {
  const key = el.dataset.key;
  const release = () => {
    el.classList.remove("pressed");
    if (held.delete(key)) sendEdge(key, "up");
  };
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (el.setPointerCapture) el.setPointerCapture(e.pointerId);
    el.classList.add("pressed");
    haptic();
    if (pad.contains(el) && !el.classList.contains("ok")) {
      const box = pad.getBoundingClientRect();
      const ripple = document.createElement("span");
      ripple.className = "ripple";
      ripple.style.left = `${e.clientX - box.left}px`;
      ripple.style.top = `${e.clientY - box.top}px`;
      pad.appendChild(ripple);
      setTimeout(() => ripple.remove(), 500);
    }
    if (!held.has(key)) {
      held.add(key);
      sendEdge(key, "down");
    }
    if (key === "rem_mute") setTimeout(() => statusPoll.now(), 400);
  });
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
  el.addEventListener("lostpointercapture", release);
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}
document.querySelectorAll("[data-key]").forEach(bindKey);

// Never leave a key held if the page goes to the background mid-press.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) return;
  for (const key of [...held]) {
    held.delete(key);
    sendEdge(key, "up");
  }
  document.querySelectorAll(".pressed").forEach((el) => el.classList.remove("pressed"));
});
