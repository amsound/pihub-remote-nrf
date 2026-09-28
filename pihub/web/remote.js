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

// ---- Keys: one tap per press (no hold-to-repeat), sent in order ----
let queue = Promise.resolve();
function sendTap(key) {
  queue = queue
    .then(() => postJSON("/remote/tap", { key, hold_ms: 60 }))
    .catch(() => {});
}

const pad = document.getElementById("pad");

function bindKey(el) {
  const key = el.dataset.key;
  const unpress = () => el.classList.remove("pressed");
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
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
    sendTap(key);
    if (key === "rem_mute") setTimeout(() => statusPoll.now(), 400);
  });
  el.addEventListener("pointerup", unpress);
  el.addEventListener("pointercancel", unpress);
  el.addEventListener("pointerleave", unpress);
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}
document.querySelectorAll("[data-key]").forEach(bindKey);
