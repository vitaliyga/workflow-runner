// Lightweight vanilla JS. No build step.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const PAGE_RUN_TYPE = document.body.dataset.runType || "image";

// ---- auth header (optional) ---------------------------------------------
const ADMIN_TOKEN = localStorage.getItem("admin_token") || "";
function authHeaders() {
  return ADMIN_TOKEN ? { Authorization: `Bearer ${ADMIN_TOKEN}` } : {};
}

// ---- drag&drop helpers --------------------------------------------------
function wireDrop(elDrop, elInput, onChange) {
  // elDrop is a <label> wrapping elInput, so native label-for-input
  // already triggers the file dialog on click. No explicit click handler
  // needed — adding one would open the dialog twice.
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

function clearBuilderCsv() {
  sessionStorage.removeItem("builder_jobs_csv");
  sessionStorage.removeItem("builder_jobs_name");
  localStorage.removeItem("builder_jobs_csv");
  localStorage.removeItem("builder_jobs_name");
}

function setCsvFile(file, label) {
  csvFile = file;
  $("#csv-name").textContent = label || (csvFile ? csvFile.name : "");
  refreshCreateBtn();
}

const pendingBuilderCsv = sessionStorage.getItem("builder_jobs_csv") ||
                          localStorage.getItem("builder_jobs_csv") || "";
if (pendingBuilderCsv) {
  setCsvFile(new File([pendingBuilderCsv], "jobs.csv", { type: "text/csv" }),
             "jobs.csv (из Builder)");
}

wireDrop($("#csv-drop"), $("#csv-input"), (files) => {
  const file = files[0] || null;
  if (file) {
    clearBuilderCsv();
    setCsvFile(file, file.name);
  }
});

wireDrop($("#photos-drop"), $("#photos-input"), (files) => {
  photoFiles = Array.from(files);
  $("#photos-name").textContent =
    photoFiles.length ? `${photoFiles.length} файл(ов)` : "";
  refreshCreateBtn();
});

function refreshCreateBtn() {
  $("#btn-create").disabled = !csvFile;
}

if (csvFile) {
  refreshCreateBtn();
}

// ---- create run ---------------------------------------------------------
$("#btn-create").addEventListener("click", async () => {
  $("#btn-create").disabled = true;
  $("#create-msg").textContent = "загружаю...";
  const fd = new FormData();
  fd.append("csv_file", csvFile);
  fd.append("run_type", PAGE_RUN_TYPE);
  fd.append("save_prompt", $("#save-prompt-input")?.checked ? "true" : "false");
  photoFiles.forEach((f) => fd.append("photos", f, f.name));
  let r;
  try {
    r = await fetch("/api/runs", {
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
  clearBuilderCsv();
  await openRun(run_id, total);
  if (missing_inputs && missing_inputs.length) {
    showHint("warn",
      `⚠ Не загружены фото: ${missing_inputs.join(", ")}. ` +
      `Без них запуск упадёт. Положи их и пересоздай прогон.`);
  } else {
    showHint("info",
      `✓ Прогон создан, ${total} заданий готово. Нажми «▶ Запустить генерацию».`);
  }
});

function showHint(kind, text) {
  const el = $("#hint");
  el.className = "hint " + kind;
  el.textContent = text;
  el.classList.remove("hidden");
}
function clearHint() { $("#hint").classList.add("hidden"); }

// Per-job generation time → "42с" / "1м 05с".
function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return "";
  const s = Math.round(sec);
  if (s < 60) return `${s}с`;
  const m = Math.floor(s / 60);
  return `${m}м ${String(s % 60).padStart(2, "0")}с`;
}

// ---- run view + SSE ------------------------------------------------------
let currentRunId = null;
let evtSrc = null;
let currentGallery = [];
let currentPreviewIndex = -1;
let currentJobCache = new Map();

async function openRun(runId) {
  currentRunId = runId;
  $("#run").classList.remove("hidden");
  $("#run-id").textContent = "ID " + runId;
  $("#jobs tbody").innerHTML = "";
  currentGallery = [];
  currentPreviewIndex = -1;
  currentJobCache = new Map();
  // Initial snapshot
  const s = await fetch(`/api/runs/${runId}/status`,
                        { headers: authHeaders() }).then(r => r.json());
  cacheJobs(s.jobs);
  s.jobs.forEach(j => upsertRow(j));
  updateCounts(s.counts, s.total);
  // SSE
  if (evtSrc) evtSrc.close();
  evtSrc = new EventSource(`/api/runs/${runId}/events`);
  evtSrc.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "snapshot") {
      cacheJobs(ev.data.jobs);
      ev.data.jobs.forEach(j => upsertRow(j));
      updateCounts(ev.data.counts, ev.data.total);
    } else if (ev.type === "job_updated") {
      currentJobCache.set(ev.job.idx, {
        idx: ev.job.idx,
        files: Array.isArray(ev.job.files) ? [...ev.job.files] : []
      });
      rebuildGalleryFromCache();
      upsertRow(ev.job);
      refreshCountsFromTable();
    } else if (ev.type === "run_finished" || ev.type === "run_cancelled") {
      refreshCountsFromTable();
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
  const inputs = j.input_images && j.input_images.length
    ? j.input_images
    : (j.input_image ? [j.input_image] : []);
  const inputLabel = inputs[0] || "";
  const inputExtra = inputs.length > 1 ? ` +${inputs.length - 1}` : "";
  const inputTitle = inputs.join(", ");
  const thumbs = (j.files || [])
    .filter(isPreviewableImage)
    .map((f) => {
      const galleryIndex = findGalleryIndex(f);
      const thumbUrl = `/api/runs/${currentRunId}/file/thumb/${encodePath(f).replace(/\//g, "__").replace(/\.(png|jpe?g|webp)$/i, ".jpg")}`;
      const title = `${f.split("/").pop()}${galleryIndex >= 0 ? ` #${galleryIndex + 1}` : ""}`;
      return `<button type="button" class="thumb" data-preview="${escape(f)}" data-preview-index="${galleryIndex}" title="${escape(title)}">
        <img loading="lazy" src="${thumbUrl}" alt="${escape(title)}" />
      </button>`;
    })
    .join("");
  const files = (j.files || [])
    .map(f => `<a href="/api/runs/${currentRunId}/file/output/${encodePath(f)}" target="_blank">${f.split("/").pop()}</a>`)
    .join("");
  const status = j.status || "pending";
  tr.innerHTML = `
    <td>${j.idx}</td>
    <td>${escape(j.workflow)}</td>
    <td title="${escape(inputTitle)}">${escape(j.girl)}${inputExtra ? ` <small class="muted">(${escape(inputLabel)}${escape(inputExtra)})</small>` : ""}</td>
    <td>${escape(j.lora_name)}</td>
    <td class="prompt" title="${escape(j.prompt_positive || "")}">${escape(j.prompt_positive || "")}</td>
    <td>${j.seed || ""}</td>
    <td class="s-${status}">${status}${j.error ? `<br><small>${escape(j.error)}</small>` : ""}</td>
    <td class="dur">${fmtDuration(j.duration)}</td>
    <td class="thumbs">${thumbs}</td>
    <td class="files">${files}</td>`;
}

function cacheJobs(jobs = []) {
  currentJobCache = new Map(jobs.map(j => [j.idx, {
    idx: j.idx,
    files: Array.isArray(j.files) ? [...j.files] : []
  }]));
  rebuildGalleryFromCache();
}

function rebuildGalleryFromCache() {
  currentGallery = [];
  const jobs = [...currentJobCache.values()].sort((a, b) => a.idx - b.idx);
  for (const j of jobs) {
    for (const f of (j.files || [])) {
      if (isPreviewableImage(f)) currentGallery.push(f);
    }
  }
}

function isPreviewableImage(f) {
  return /\.(png|jpe?g|webp)$/i.test(String(f || ""));
}

function findGalleryIndex(file) {
  return currentGallery.findIndex(f => f === file);
}

function openPreviewAt(index) {
  if (index < 0 || index >= currentGallery.length) return;
  currentPreviewIndex = index;
  renderPreview();
}

function renderPreview() {
  const box = $("#preview-lightbox");
  const img = $("#lightbox-image");
  const caption = $("#lightbox-caption");
  const counter = $("#lightbox-counter");
  if (!box || !img) return;
  const file = currentGallery[currentPreviewIndex];
  if (!file) return;
  img.src = `/api/runs/${currentRunId}/file/output/${encodePath(file)}`;
  img.alt = file.split("/").pop();
  caption.textContent = file.split("/").pop();
  counter.textContent = `${currentPreviewIndex + 1} / ${currentGallery.length}`;
  box.classList.remove("hidden");
  box.setAttribute("aria-hidden", "false");
}

function closePreview() {
  const box = $("#preview-lightbox");
  if (!box) return;
  box.classList.add("hidden");
  box.setAttribute("aria-hidden", "true");
  $("#lightbox-image").src = "";
  currentPreviewIndex = -1;
}

function movePreview(delta) {
  if (!currentGallery.length) return;
  const next = (currentPreviewIndex + delta + currentGallery.length) % currentGallery.length;
  currentPreviewIndex = next;
  renderPreview();
}

document.addEventListener("click", (e) => {
  const previewBtn = e.target.closest("button.thumb[data-preview]");
  if (previewBtn) {
    const idx = Number(previewBtn.dataset.previewIndex);
    if (!Number.isNaN(idx) && idx >= 0) {
      openPreviewAt(idx);
    }
    return;
  }
  if (e.target.closest("[data-close]")) {
    closePreview();
    return;
  }
  if (e.target.closest("[data-prev]")) {
    movePreview(-1);
    return;
  }
  if (e.target.closest("[data-next]")) {
    movePreview(1);
  }
});

document.addEventListener("keydown", (e) => {
  if ($("#preview-lightbox")?.classList.contains("hidden")) return;
  if (e.key === "Escape") closePreview();
  if (e.key === "ArrowLeft") movePreview(-1);
  if (e.key === "ArrowRight") movePreview(1);
});

function updateCounts(c = {}, total = 0) {
  $("#counts").innerHTML = `
    <span>всего: <b>${total}</b></span>
    <span class="pending">pending: <b>${c.pending||0}</b></span>
    <span class="running">running: <b>${c.running||0}</b></span>
    <span class="done">done: <b>${c.done||0}</b></span>
    <span class="failed">failed: <b>${c.failed||0}</b></span>`;
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
  const r = await fetch(`/api/runs/${currentRunId}/start`,
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

// ---- history ------------------------------------------------------------
async function loadHistory() {
  const url = `/api/runs?run_type=${encodeURIComponent(PAGE_RUN_TYPE)}`;
  const data = await fetch(url,
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
        <button data-archive="${r.run_id}" class="ghost">Архив</button>
        <button data-del="${r.run_id}" class="ghost">×</button>
      </td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("button[data-open]").forEach(btn =>
    btn.addEventListener("click", () => openRun(btn.dataset.open)));
  tbody.querySelectorAll("button[data-archive]").forEach(btn =>
    btn.addEventListener("click", () => downloadArchive(btn, btn.dataset.archive)));
  tbody.querySelectorAll("button[data-del]").forEach(btn =>
    btn.addEventListener("click", async () => {
      if (!confirm(`Удалить прогон ${btn.dataset.del}?\nЭто снесёт все файлы и фото.`)) return;
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

async function downloadArchive(btn, runId) {
  const archiveMsg = $("#archive-msg");
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ Готовим архив...";
  if (archiveMsg) archiveMsg.textContent = `Готовим архив для ${runId}...`;

  try {
    const r = await fetch(`/api/runs/${runId}/archive`, { headers: authHeaders() });
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${runId}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    alert("ошибка архива: " + e);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
    if (archiveMsg) archiveMsg.textContent = "";
  }
}

// ---- helpers ------------------------------------------------------------
function escape(s) {
  return String(s || "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function encodePath(p) {
  return p.split("/").map(encodeURIComponent).join("/");
}
