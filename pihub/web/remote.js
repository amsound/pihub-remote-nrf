import { getJSON, postJSON, poll } from "/web/common.js";

const root = document.documentElement;
const MODE_ATTR = { power_off: "off", listen: "listen", watch: "watch" };

// ---- Mode: follow /api/status while the page is visible ----
let flowPending = false; // while a flow runs, the optimistic mode stands
const statusPoll = poll(async () => {
  const d = await getJSON("/api/status", { timeoutMs: 2500 });
  if (!flowPending) root.dataset.mode = MODE_ATTR[d.mode] || "off";
  document.getElementById("room").textContent = d.name || "";
  document.title = d.name ? `${d.name} Remote` : "Remote";
  document.getElementById("mute").classList.toggle("on", !!d.speaker?.muted);
}, 2000);

function haptic() {
  if (navigator.vibrate) navigator.vibrate(8);
}

// Off / Listen / Watch: switch straight away (optimistic) and run the flow; the
// pressed segment pulses while it runs. If the flow fails, slide back and flash.
const seg = document.querySelector(".seg");
document.querySelectorAll(".seg button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (flowPending) return;
    haptic();
    const previous = root.dataset.mode;
    flowPending = true;
    root.dataset.mode = MODE_ATTR[btn.dataset.flow];
    btn.classList.add("pending");
    const started = performance.now();
    let ok = false;
    try {
      ({ ok } = await postJSON(`/api/flow/${btn.dataset.flow}`, { trigger: "http.remote.flow" }));
    } catch { /* treated as failed */ }
    if (!ok) {
      // Let the switch register before bouncing back, even if the failure is instant.
      await new Promise((r) => setTimeout(r, Math.max(0, 400 - (performance.now() - started))));
    }
    btn.classList.remove("pending");
    flowPending = false;
    if (!ok) {
      root.dataset.mode = previous;
      seg.classList.add("failed");
      setTimeout(() => seg.classList.remove("failed"), 700);
    }
    statusPoll.now();
  });
});

// ---- Keys: one tap per press (no hold-to-repeat), sent in order ----
let queue = Promise.resolve();
function sendTap(key) {
  queue = queue
    .then(() => postJSON("/api/key/tap", { key, hold_ms: 60 }))
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
