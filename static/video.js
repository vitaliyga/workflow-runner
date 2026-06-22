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
// Upload photos to an existing run in small batches (one giant multipart gets
// dropped by the RunPod proxy). Returns latest missing_inputs.
async function uploadPhotosInBatches(runId, files, onProgress) {
  const MAX_FILES = 20, MAX_BYTES = 25 * 1024 * 1024;
  let i = 0, done = 0, missing = [];
  while (i < files.length) {
    const fd = new FormData();
    let n = 0, bytes = 0;
    while (i < files.length && n < MAX_FILES && bytes < MAX_BYTES) {
      const f = files[i++];
      fd.append("photos", f, f.name);
      n++; bytes += f.size || 0;
    }
    const r = await fetch(`/api/runs/${runId}/inputs`,
                          { method: "POST", body: fd, headers: authHeaders() });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json().catch(() => ({}));
    if (Array.isArray(data.missing_inputs)) missing = data.missing_inputs;
    done += n;
    if (onProgress) onProgress(done, files.length);
  }
  return missing;
}

$("#btn-create").addEventListener("click", async () => {
  $("#btn-create").disabled = true;
  $("#create-msg").textContent = "создаю прогон...";

  // 1) create the run with the CSV only (tiny request)
  const fd = new FormData();
  fd.append("csv_file", csvFile);
  fd.append("save_prompt", $("#save-prompt-input")?.checked ? "true" : "false");
  fd.append("lora_folder", $("#lora-folder-input")?.checked ? "true" : "false");

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

  let { run_id, total, missing_inputs } = await r.json();
  // 2) upload photos in batches (robust to hundreds of files)
  if (photoFiles.length) {
    try {
      missing_inputs = await uploadPhotosInBatches(run_id, photoFiles,
        (d, t) => { $("#create-msg").textContent = `загрузка фото ${d}/${t}…`; });
    } catch (e) {
      $("#create-msg").textContent = "";
      $("#btn-create").disabled = false;
      await openRun(run_id, total);
      showHint("error",
        `Загрузка фото оборвалась: ${e}. Часть могла не дойти — повтори (можно частями).`);
      return;
    }
  }
  $("#create-msg").textContent = "";
  await openRun(run_id, total);

  if (missing_inputs && missing_inputs.length) {
    showHint("warn",
      `⚠ Не загружены фото: ${missing_inputs.length} шт. ` +
      `Без них запуск упадёт. Дозагрузи и пересоздай прогон.`);
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
    <td class="dur">${fmtDuration(j.duration)}</td>
    <td class="files">${files}</td>`;
}

// Per-job generation time → "42с" / "1м 05с".
function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return "";
  const s = Math.round(sec);
  if (s < 60) return `${s}с`;
  const m = Math.floor(s / 60);
  return `${m}м ${String(s % 60).padStart(2, "0")}с`;
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

$("#btn-queue").addEventListener("click", async () => {
  if (!currentRunId) return;
  $("#start-msg").textContent = "ставлю в очередь...";
  // Generic queue endpoint works for video runs too (same run_id namespace).
  const r = await fetch(`/api/runs/${currentRunId}/queue`,
                        { method: "POST", headers: authHeaders() });
  $("#start-msg").textContent = "";
  if (!r.ok) {
    const txt = await r.text();
    let msg = txt;
    try { msg = JSON.parse(txt).detail || txt; } catch {}
    showHint("error", "В очередь не удалось: " + msg);
    return;
  }
  const data = await r.json().catch(() => ({}));
  showHint("info", data.position > 1
    ? `В очереди — позиция ${data.position}. Стартует, когда освободится pod.`
    : "В очереди — стартует автоматически (pod свободен).");
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

// ---- workflow / version picker -----------------------------------------
// Lists registered video flows (incl. LTX v1/v2). Selecting one shows that
// flow's CSV columns and points the sample-CSV link at its per-flow sample.
const FIELD_LABELS = {
  input_image: "входное фото", prompt_positive: "позитивный промт",
  prompt_negative: "негативный промт", seed: "сид",
  video_length_seconds: "длина (сек)", video_width: "ширина", video_height: "высота",
  sigmas_first_pass: "sigmas 1-й проход", sigmas_final_pass: "sigmas финальный",
  cfg_first_pass: "CFG 1-й проход", cfg_final_pass: "CFG финальный",
  audio_volume_first: "громкость 1-й", audio_volume_final: "громкость финал",
  checkpoint_name: "checkpoint", diffusion_model_name: "diffusion model",
  load_loras_json: "лора (основная)",
  load_distilled_lora_json: "лора distilled — 1-й проход",
  load_distilled_lora_final_json: "лора distilled — финальный проход",
};

let videoFlows = [];

async function loadFlows() {
  const sel = $("#wf-select");
  let data;
  try {
    const r = await fetch("/api/workflows", { headers: authHeaders() });
    if (!r.ok) throw new Error(await r.text());
    data = await r.json();
  } catch (e) {
    sel.innerHTML = '<option value="">(не удалось загрузить)</option>';
    return;
  }
  videoFlows = (data.workflows || []).filter(w => w.video && w.available);
  if (!videoFlows.length) {
    sel.innerHTML = '<option value="">(нет видео-флоу)</option>';
    $("#cols-grid").innerHTML = "";
    $("#wf-hint").textContent =
      "Нет зарегистрированных видео-флоу. Загрузите JSON в Настройки → Upload → Register.";
    $("#btn-sample-csv").setAttribute("href", "/api/video-runs/sample-csv");
    return;
  }
  sel.innerHTML = "";
  videoFlows.forEach(w => {
    const o = document.createElement("option");
    o.value = w.name; o.textContent = w.name;
    sel.appendChild(o);
  });
  selectFlow(videoFlows[0].name);
}

function selectFlow(name) {
  const flow = videoFlows.find(w => w.name === name);
  if (!flow) return;
  $("#wf-select").value = name;
  $("#btn-sample-csv").setAttribute(
    "href", `/api/workflows/${encodeURIComponent(name)}/sample_csv`);
  $("#cols-title").textContent = `Колонки CSV — ${name}`;
  const grid = $("#cols-grid");
  grid.innerHTML = "";
  // workflow/scenario/girl are always present in samples
  const base = [["workflow", "ключ флоу"], ["girl", "метка / имя"], ["scenario", "группа / сцена"]];
  const fields = flow.video_fields || {};
  base.forEach(([k, d]) => grid.appendChild(colItem(k, d)));
  Object.keys(fields).forEach(k => {
    const node = fields[k] && fields[k].node ? `нода ${fields[k].node}` : "";
    grid.appendChild(colItem(k, [FIELD_LABELS[k] || "", node].filter(Boolean).join(" — ")));
  });
  $("#wf-hint").textContent =
    "«Скачать пример CSV» даст файл ровно с этими колонками, предзаполненный значениями шаблона.";
}

function colItem(code, sub) {
  const div = document.createElement("div");
  div.className = "col-item";
  div.innerHTML = `<code>${esc(code)}</code><small>${esc(sub)}</small>`;
  return div;
}

$("#wf-select").addEventListener("change", (e) => selectFlow(e.target.value));
loadFlows();

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
