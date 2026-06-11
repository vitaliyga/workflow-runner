// video.js — standalone JS for /video page.
// Uses dedicated /api/video-runs/* endpoints, NOT /api/runs.
// History only shows run_type==="video" runs.

"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ---- auth header (optional) ---------------------------------------------
const ADMIN_TOKEN = localStorage.getItem("admin_token") || "";
function authHeaders() {
  return ADMIN_TOKEN ? { Authorization: `Bearer ${ADMIN_TOKEN}` } : {};
}

// ---- drag&drop helpers --------------------------------------------------
function wireDrop(elDrop, elInput, onChange) {
  elInput.addEventListener("change", () => onChange(elInput.files));
  ["dragenter", "dragover"].forEach((ev) =>
    elDrop.addEventListener(ev, (e) => {
      e.preventDefault(); elDrop.classList.add("hover");
    }));
  ["dragleave", "drop"].forEach((ev) =>
    elDrop.addEventListener(ev, (e) => {
      e.preventDefault(); elDrop.classList.remove("hover");
    }));
  elDrop.addEventListener("drop", (e) => {
    e.preventDefault();
    onChange(e.dataTransfer.files);
  });
}

let csvFile = null;
let photoFiles = [];

function setCsvFile(file, label) {
  csvFile = file;
  $("#csv-name").textContent = label || (csvFile ? csvFile.name : "");
  refreshCreateBtn();
}

wireDrop($("#csv-drop"), $("#csv-input"), (files) => {
  const file = files[0] || null;
  if (file) setCsvFile(file, file.name);
});

wireDrop($("#photos-drop"), $("#photos-input"), (files) => {
  photoFiles = Array.from(files);
  $("#photos-name").textContent =
    photoFiles.length ? `${photoFiles.length} файл(ов)` : "";
});

function refreshCreateBtn() {
  $("#btn-create").disabled = !csvFile;
}

// ---- create video run ---------------------------------------------------
$("#btn-create").addEventListener("click", async () => {
  $("#btn-create").disabled = true;
  $("#create-msg").textContent = "загружаю...";

  const fd = new FormData();
  fd.append("csv_file", csvFile);
  fd.append("save_prompt", $("#save-prompt-input")?.checked ? "true" : "false");
  photoFiles.forEach((f) => fd.append("photos", f, f.name));

  let r;
  try {
    r = await fetch("/api/video-runs", {
      method: "POST", body: fd, headers: authHeaders(),
    });
  } catch (e) {
    $("#create-msg").textContent = "сеть упала: " + e;
    $("#btn-create").disabled = false;
    return;
  }

  if (!r.ok) {
    $("#create-msg").textContent = "ошибка: " + (await r.text());
    $("#btn-create").disabled = false;
    return;
  }

  const { run_id, total, missing_inputs } = await r.json();
  $("#create-msg").textContent = "";
  await openRun(run_id, total);

  if (missing_inputs && missing_inputs.length) {
    showHint("warn",
      `⚠ Не загружены фото: ${missing_inputs.join(", ")}. ` +
      `Загрузи их и пересоздай прогон.`);
  } else {
    showHint("info",
      `✓ Видео прогон создан, ${total} заданий готово. Нажми «▶ Запустить генерацию».`);
  }
});

function showHint(kind, text) {
  const el = $("#hint");
  el.className = "hint " + kind;
  el.textContent = text;
  el.classList.remove("hidden");
}
function clearHint() { $("#hint").classList.add("hidden"); }

// ---- run view + SSE -----------------------------------------------------
let currentRunId = null;
let evtSrc = null;

async function openRun(runId) {
  currentRunId = runId;
  $("#run").classList.remove("hidden");
  $("#run-id").textContent = "ID " + runId;
  $("#jobs tbody").innerHTML = "";

  const s = await fetch(`/api/runs/${runId}/status`,
                        { headers: authHeaders() }).then(r => r.json());
  s.jobs.forEach(j => upsertRow(j));
  updateCounts(s.counts, s.total);

  if (evtSrc) evtSrc.close();
  evtSrc = new EventSource(`/api/runs/${runId}/events`);
  evtSrc.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "snapshot") {
      ev.data.jobs.forEach(j => upsertRow(j));
      updateCounts(ev.data.counts, ev.data.total);
    } else if (ev.type === "job_updated") {
      upsertRow(ev.job);
      refreshCountsFromTable();
    } else if (ev.type === "run_finished" || ev.type === "run_cancelled") {
      refreshCountsFromTable();
      loadHistory();
    }
  };
  $("#run").scrollIntoView({ behavior: "smooth" });
}

function upsertRow(j) {
  const tbody = $("#jobs tbody");
  let tr = tbody.querySelector(`tr[data-idx="${j.idx}"]`);
  if (!tr) {
    tr = document.createElement("tr");
    tr.dataset.idx = j.idx;
    tbody.appendChild(tr);
  }

  const status = j.status || "pending";
  const dims = (j.video_width && j.video_height)
    ? `${j.video_width}×${j.video_height}`
    : "";
  const dur = j.video_length_seconds ? `${j.video_length_seconds}s` : "";

  // Render file links — video files get a special link style
  const files = (j.files || []).map(f => {
    const ext = f.split(".").pop().toLowerCase();
    const isVideo = ["mp4", "webm", "mov", "gif", "mkv", "avi"].includes(ext);
    const url = `/api/runs/${currentRunId}/file/output/${encodePath(f)}`;
    const name = f.split("/").pop();
    if (isVideo) {
      return `<a class="video-file-link" href="${url}" target="_blank">${esc(name)}</a>`;
    }
    return `<a href="${url}" target="_blank">${esc(name)}</a>`;
  }).join(" ");

  tr.innerHTML = `
    <td>${j.idx}</td>
    <td>${esc(j.girl || j.scenario || "")}</td>
    <td class="prompt" title="${esc(j.prompt_positive || "")}">${esc(j.prompt_positive || "")}</td>
    <td>${j.seed || ""}</td>
    <td>${dims ? `<span class="video-badge">${esc(dims)}</span>` : ""}</td>
    <td>${dur ? `<span class="video-badge">${esc(dur)}</span>` : ""}</td>
    <td class="s-${status}">${status}${j.error ? `<br><small>${esc(j.error)}</small>` : ""}</td>
    <td class="files">${files}</td>`;
}

function updateCounts(c = {}, total = 0) {
  $("#counts").innerHTML = `
    <span>всего: <b>${total}</b></span>
    <span class="pending">pending: <b>${c.pending || 0}</b></span>
    <span class="running">running: <b>${c.running || 0}</b></span>
    <span class="done">done: <b>${c.done || 0}</b></span>
    <span class="failed">failed: <b>${c.failed || 0}</b></span>`;
}

function refreshCountsFromTable() {
  const c = { pending: 0, running: 0, done: 0, failed: 0 };
  $$("#jobs tbody td[class^=s-]").forEach(td => {
    const s = td.className.replace("s-", "");
    if (s in c) c[s]++;
  });
  const total = $$("#jobs tbody tr").length;
  updateCounts(c, total);
}

// ---- start / cancel -----------------------------------------------------
$("#btn-start").addEventListener("click", async () => {
  if (!currentRunId) return;
  $("#start-msg").textContent = "запускаю...";
  // Use the video-specific start endpoint
  const r = await fetch(`/api/video-runs/${currentRunId}/start`,
                        { method: "POST", headers: authHeaders() });
  $("#start-msg").textContent = "";
  if (!r.ok) {
    const txt = await r.text();
    let msg = txt;
    try { msg = JSON.parse(txt).detail || txt; } catch {}
    showHint("error", "Ошибка запуска: " + msg);
    return;
  }
  showHint("info", "Поехали — задания уходят на pod'ы. Прогресс будет тут.");
});

$("#btn-cancel").addEventListener("click", async () => {
  if (!currentRunId) return;
  const r = await fetch(`/api/runs/${currentRunId}/cancel`,
                        { method: "POST", headers: authHeaders() });
  if (!r.ok) showHint("error", "Отмена не сработала: " + await r.text());
  else showHint("warn", "Прогон отменён (pending → failed).");
});

// ---- history (ONLY video runs) ------------------------------------------
async function loadHistory() {
  // Filter strictly by run_type=video — never mix with image runs
  const data = await fetch("/api/runs?run_type=video",
                           { headers: authHeaders() }).then(r => r.json());
  const tbody = $("#history-table tbody");
  tbody.innerHTML = "";
  for (const r of data.runs) {
    const tr = document.createElement("tr");
    const dt = r.started_at ? new Date(r.started_at * 1000).toLocaleString() : "";
    tr.innerHTML = `
      <td><code>${r.run_id}</code></td>
      <td>${r.total}</td>
      <td class="s-done">${r.counts.done || 0}</td>
      <td class="s-failed">${r.counts.failed || 0}</td>
      <td class="muted">${dt}</td>
      <td>
        <button data-open="${r.run_id}" class="ghost">Открыть</button>
        <button data-archive="${r.run_id}" class="ghost">⬇ Видео ZIP</button>
        <button data-del="${r.run_id}" class="ghost">×</button>
      </td>`;
    tbody.appendChild(tr);
  }

  tbody.querySelectorAll("button[data-open]").forEach(btn =>
    btn.addEventListener("click", () => openRun(btn.dataset.open)));

  tbody.querySelectorAll("button[data-archive]").forEach(btn =>
    btn.addEventListener("click", () => downloadVideoArchive(btn, btn.dataset.archive)));

  tbody.querySelectorAll("button[data-del]").forEach(btn =>
    btn.addEventListener("click", async () => {
      if (!confirm(`Удалить видео прогон ${btn.dataset.del}?\nЭто снесёт все файлы и фото.`)) return;
      const r = await fetch("/api/runs/" + btn.dataset.del,
                            { method: "DELETE", headers: authHeaders() });
      if (!r.ok) { alert("ошибка: " + await r.text()); return; }
      if (currentRunId === btn.dataset.del) {
        if (evtSrc) evtSrc.close();
        currentRunId = null;
        $("#run").classList.add("hidden");
      }
      loadHistory();
    }));
}

loadHistory();
setInterval(loadHistory, 10000);

async function downloadVideoArchive(btn, runId) {
  const archiveMsg = $("#archive-msg");
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ Готовим архив...";
  if (archiveMsg) archiveMsg.textContent = `Готовим архив для ${runId}...`;

  // Try video-specific endpoint first (returns .mp4/.webm); fall back to generic
  try {
    let url = `/api/video-runs/${runId}/archive`;
    let r = await fetch(url, { headers: authHeaders() });
    if (!r.ok) {
      // Fallback to generic (may return empty for video-only runs)
      url = `/api/runs/${runId}/archive`;
      r = await fetch(url, { headers: authHeaders() });
    }
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${runId}-videos.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) {
    alert("ошибка архива: " + e);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
    if (archiveMsg) archiveMsg.textContent = "";
  }
}

// ---- helpers ------------------------------------------------------------
function esc(s) {
  return String(s || "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function encodePath(p) {
  return p.split("/").map(encodeURIComponent).join("/");
}
