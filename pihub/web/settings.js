import { getJSON, postJSON, esc, renderTopBar, renderFooter, roomStrip } from "/web/common.js";

const $ = (id) => document.getElementById(id);
const keyOf = (slot) => slot % 10; // slot 10 is key 0
let backend = "";

async function load() {
  const [status, res] = await Promise.all([getJSON("/api/status"), getJSON("/api/settings")]);
  renderTopBar(document.querySelector(".top"), "/settings", status.name);
  renderFooter(document.querySelector(".foot"), status);
  roomStrip($("rooms")).update(status);
  document.title = `Settings · ${status.name}`;
  if (!res.ok) {
    $("note").className = "note bad";
    $("note").textContent = res.error || "Settings unavailable";
    return;
  }
  backend = res.backend === "samsung_soundbar" ? "samsung_soundbar" : "audiopro";
  document.querySelectorAll("[data-backend]").forEach((el) => el.classList.toggle("hidden", el.dataset.backend !== backend));
  fill(res.settings);
}

function fill(s) {
  $("watch_volume_pct").value = s.watch_volume_pct;
  $("listen_volume_pct").value = s.listen_volume_pct;

  if (backend === "samsung_soundbar") {
    $("sb_slots").innerHTML = Array.from({ length: 10 }, (_, i) => i + 1).map((n) => `
      <div class="slot">
        <span class="k">Key ${keyOf(n)}</span>
        <input type="text" id="soundbar_stream_url_${n}" placeholder="Stream URL or TuneIn ID" value="${esc(s[`soundbar_stream_url_${n}`])}">
        <label class="check"><input type="checkbox" id="soundbar_restream_${n}" ${s[`soundbar_restream_${n}`] ? "checked" : ""}>Restream</label>
      </div>`).join("");
    const renderListenOptions = () => {
      const chosen = $("sb_listen_slot").value || String(s.listen_target_stream);
      $("sb_listen_slot").innerHTML = Array.from({ length: 10 }, (_, i) => i + 1).map((n) => {
        const url = $(`soundbar_stream_url_${n}`).value.trim();
        return `<option value="${n}" ${String(n) === chosen ? "selected" : ""}>Key ${keyOf(n)}${url ? ": " + esc(url.length > 48 ? url.slice(0, 45) + "…" : url) : " (empty)"}</option>`;
      }).join("");
    };
    renderListenOptions();
    $("sb_slots").addEventListener("input", renderListenOptions);
  } else {
    $("ap_slots").innerHTML = [1, 2, 3, 4].map((n) => `
      <div class="slot" style="grid-template-columns:52px 1fr">
        <span class="k">Key ${keyOf(n + 6)}</span>
        <input type="text" id="stream_url_${n}" placeholder="http(s)://…" value="${esc(s[`stream_url_${n}`])}">
      </div>`).join("");
    $("listen_target_type").value = s.listen_target_type;
    $("listen_target_preset").value = s.listen_target_preset;
    $("listen_target_stream").value = String(s.listen_target_stream);
    const toggle = () => {
      const preset = $("listen_target_type").value === "preset";
      $("preset_field").classList.toggle("hidden", !preset);
      $("stream_field").classList.toggle("hidden", preset);
    };
    $("listen_target_type").onchange = toggle;
    toggle();
  }
}

function payload() {
  const p = {
    watch_volume_pct: Number($("watch_volume_pct").value),
    listen_volume_pct: Number($("listen_volume_pct").value),
  };
  if (backend === "samsung_soundbar") {
    p.listen_target_type = "stream";
    p.listen_target_stream = Number($("sb_listen_slot").value);
    for (let n = 1; n <= 10; n++) {
      p[`soundbar_stream_url_${n}`] = $(`soundbar_stream_url_${n}`).value.trim();
      p[`soundbar_restream_${n}`] = $(`soundbar_restream_${n}`).checked;
    }
  } else {
    p.listen_target_type = $("listen_target_type").value;
    p.listen_target_preset = Number($("listen_target_preset").value);
    p.listen_target_stream = Number($("listen_target_stream").value);
    for (let n = 1; n <= 4; n++) p[`stream_url_${n}`] = $(`stream_url_${n}`).value.trim();
  }
  return p;
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const note = $("note");
  $("save").disabled = true;
  note.className = "note";
  note.textContent = "Saving…";
  try {
    const { ok, data } = await postJSON("/api/settings", payload());
    note.className = "note " + (ok ? "ok" : "bad");
    note.textContent = ok ? "Saved. Applies to the next flow or key press." : data.error || "Save failed";
  } catch {
    note.className = "note bad";
    note.textContent = "No response from PiHub";
  } finally {
    $("save").disabled = false;
  }
});

load();
