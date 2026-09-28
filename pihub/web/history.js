import { getJSON, postJSON, esc, title, MODE, clock, renderTopBar, poll, triggerText } from "/web/common.js";

const $ = (id) => document.getElementById(id);
// "apple_tv_power_on" -> "Apple TV power on"
const stepName = (id) => title(id)
  .replace(/\btv\b/gi, "TV").replace(/\barc\b/gi, "ARC").replace(/\bhdmi\b/gi, "HDMI").replace(/\bapple TV\b/gi, "Apple TV");


function stepLine(s) {
  // Await steps settle in `status`; background ("dispatch") steps in `outcome_status`.
  const status = s.status === "dispatched" ? s.outcome_status || "running" : s.status;
  const error = s.status === "dispatched" ? s.outcome_error : s.error;
  const dot = status === "ok" ? "ok" : status === "failed" ? "bad" : "";
  const why = status === "skipped" && s.reason?.startsWith("when_false:")
    ? `skipped: ${s.reason.slice(11).replace(/_/g, " ")} was false`
    : error || "";
  const ms = s.duration_ms != null && status !== "skipped" ? `${s.outcome_duration_ms ?? s.duration_ms} ms` : "";
  return `<div class="step"><span class="dot ${dot}"></span><span>${esc(stepName(s.step_id))}${s.mode === "dispatch" ? ' <span class="st">(background)</span>' : ""}</span>
    <span class="st">${esc(ms)}</span>${why ? `<span class="why ${status === "failed" ? "bad" : ""}">${esc(why)}</span>` : ""}</div>`;
}

let open = new Set();

async function refresh() {
  const [status, { flows = [] }, { events = [] }] = await Promise.all([
    getJSON("/api/status"), getJSON("/history/flows?limit=20"), getJSON("/history/events?limit=100"),
  ]);
  renderTopBar(document.querySelector(".top"), "/history", status.name);
  document.title = `History · ${status.name}`;

  open = new Set([...document.querySelectorAll("details[open]")].map((d) => d.dataset.id));
  $("flows").className = flows.length ? "" : "muted";
  $("flows").innerHTML = flows.length ? flows.map((f) => {
    const ok = f.result === "ok";
    const secs = f.duration_ms != null ? (f.duration_ms / 1000).toFixed(1) + "s" : "…";
    return `<details data-id="${esc(f.id)}" ${open.has(f.id) ? "open" : ""}>
      <summary><span class="when">${clock(f.ts_started)}</span><span class="name">${esc(MODE[f.flow_name] || title(f.flow_name))}</span>
        <span class="by">${esc(triggerText(f.trigger))}</span>
        <span class="ms">${secs} <span class="dot ${f.result === "running" ? "" : ok ? "ok" : "bad"}" style="vertical-align:middle;margin-left:4px"></span></span></summary>
      ${f.error ? `<div class="fail">${esc(f.error)}</div>` : ""}
      <div class="steps">${(f.steps || []).map(stepLine).join("") || '<span class="muted">No steps</span>'}</div>
    </details>`;
  }).join("") : "No flows yet";

  const problems = events.filter((e) => ["warning", "error"].includes(String(e.level).toLowerCase()));
  $("events").className = problems.length ? "" : "muted";
  $("events").innerHTML = problems.length ? problems.map((e) => {
    const err = e.metadata?.error;
    return `<div class="event"><span class="when">${clock(e.ts)} · ${esc(e.level)}</span><span>${esc(e.message)}</span>${err ? `<span class="err">${esc(err)}</span>` : ""}</div>`;
  }).join("") : "None";
}

const history = poll(refresh, 10000);

$("clear").addEventListener("click", async () => {
  if (!confirm("Clear all flow history and events?")) return;
  await postJSON("/history/clear");
  history.now();
});
