"""FastAPI server for the workflow runner.

Endpoints (`/api/...`) cover the run lifecycle (upload csv + photos →
start → live progress → previews/downloads) plus config CRUD for pods,
S3 and workflow mappings. The frontend in `static/` is plain HTML/JS.

State is kept on disk under `storage/`:
  storage/
    config.yaml                # consolidated config (pods + workflows + s3)
    runs/<run_id>/
      jobs.csv                 # uploaded
      inputs/                  # uploaded photos
      outputs/                 # generated images
      status.json              # batch state snapshot
      thumbs/                  # 256px previews
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import secrets
import time
import uuid
import tempfile
import zipfile
from datetime import datetime
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from starlette.background import BackgroundTask

from csv_loader import load_jobs
from expand_scenario import expand as expand_scenario_dict
from path_builder import save_prefix, slug
from pod_client import PodConfig
from pod_pool import PodPool
from s3_uploader import S3Uploader
from workflow_builder import (
    JobParams,
    WorkflowRegistry,
    build_workflow,
)
from video_csv_loader import load_video_jobs, SAMPLE_CSV, VideoJob
from video_workflow_builder import (
    build_video_workflow,
    load_video_template,
    detect_video_mapping,
    default_video_mapping,
    is_video_workflow,
    sample_csv_for,
    full_columns,
    universal_sample_csv,
    VIDEO_FIELD_CATALOG,
    VIDEO_SAVE_CLASSES,
)


# ---------------------------------------------------------------------------
# bootstrap

ROOT = Path(__file__).parent
# STORAGE вынесен в env, потому что на RunPod entrypoint пода при каждом старте
# делает `rm -rf /workspace/workflow-runner && git clone` — всё, что лежит внутри
# репозитория, пропадает вместе с прогонами (и незакоммиченными jobs/*.json).
# RUNNER_STORAGE=/workspace/runner-state переносит состояние наружу, и прогон
# переживает рестарт пода. По умолчанию — прежний путь, поведение не меняется.
STORAGE = Path(os.environ.get("RUNNER_STORAGE") or (ROOT / "storage"))
RUNS = STORAGE / "runs"


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env(v: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return os.environ.get(name, "")

    return _ENV_VAR_RE.sub(repl, v)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("web")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        v = _expand_env(v)
        if v == "":
            continue
        os.environ.setdefault(k, v)


_load_dotenv(ROOT / ".env")
CONFIG_PATH = Path(os.environ.get("CONFIG", STORAGE / "config.yaml"))


# ---------------------------------------------------------------------------
# config (single consolidated yaml under storage/)


def _default_s3_config() -> dict[str, str]:
    # RunPod typically exposes either S3_BUCKET or the sync bases.
    # Use them to prefill and keep S3 mirrored by default instead of making
    # the user type bucket/prefix.
    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        for env_name in ("S3_MODELS_BASE", "S3_NODES_BASE"):
            raw = (os.environ.get(env_name) or "").strip()
            if raw.startswith("s3://"):
                rest = raw[5:]
                bucket = rest.split("/", 1)[0].strip()
                if bucket:
                    break
    prefix = (os.environ.get("S3_PREFIX") or "test/runner/").strip().strip("/")
    region = (
        os.environ.get("S3_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    ).strip()
    endpoint_url = (os.environ.get("S3_ENDPOINT_URL") or "").strip()
    return {
        "bucket": bucket,
        "prefix": f"{prefix}/" if prefix else "",
        "region": region,
        "endpoint_url": endpoint_url,
    }

DEFAULT_CONFIG: dict[str, Any] = {
    "pods": [],
    "workflows": {
        "defaults": {
            "ksampler": {"node": "348"},
            "positive_prompt": {"node": "622"},
            "negative_prompt": {"node": "343"},
            "load_images": {"main": {"node": "617"}},
            "save_images": ["620"],
            "lora_loaders": [{"node": "621"}],
        },
        "workflows": {
            "sample_image_v1": {
                "template": "jobs/sample_image_v1.json",
            },
        },
    },
    "s3": _default_s3_config(),
}


def _bootstrap_default_config() -> dict[str, Any]:
    """Build initial config. If a project-root workflows.yaml exists,
    use it instead of the empty stub — this is the file the CLI version
    of the project keeps as source of truth."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    wf_path = ROOT / "workflows.yaml"
    if wf_path.exists():
        try:
            cfg["workflows"] = yaml.safe_load(wf_path.read_text()) or cfg["workflows"]
            log.info("bootstrap: imported workflows from %s", wf_path)
        except yaml.YAMLError as e:
            log.warning("bootstrap: failed to parse %s: %s", wf_path, e)
    return cfg


def _workflow_cfg_block() -> dict[str, Any]:
    cfg = load_config()
    return cfg.get("workflows") or {}


def _resolve_workflow_template_path(template: str) -> Path:
    p = Path(template)
    return p if p.is_absolute() else (ROOT / p)


def _node_ref(raw: Any, default_field: str = "text") -> tuple[str | None, str]:
    if isinstance(raw, dict):
        return str(raw.get("node")) if raw.get("node") is not None else None, str(raw.get("field", default_field))
    if raw is None:
        return None, default_field
    return str(raw), default_field


def _workflow_meta(name: str, defaults: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    merged = {**defaults, **spec}
    template_raw = merged.get("template")
    template_path = _resolve_workflow_template_path(str(template_raw)) if template_raw else None

    out: dict[str, Any] = {
        "name": name,
        "template": str(template_raw or ""),
        "available": bool(template_path and template_path.exists()),
        "video": bool(spec.get("is_video")),
    }
    if out["video"]:
        # Mapped CSV columns for this video flow ({col: {node, field}}), so the
        # /video page can show per-workflow columns without a second request.
        out["video_fields"] = spec.get("video_fields") or {}
    if not template_path or not template_path.exists():
        return out

    try:
        wf = json.loads(template_path.read_text())
    except Exception as e:
        out["error"] = f"template parse failed: {e}"
        return out

    def inputs(nid: str | None) -> dict[str, Any]:
        if not nid:
            return {}
        node = wf.get(str(nid))
        if not isinstance(node, dict):
            return {}
        return node.get("inputs", {}) or {}

    ks_raw = merged.get("ksampler") or {}
    ks_node, _ = _node_ref(ks_raw if isinstance(ks_raw, dict) else {"node": ks_raw}, default_field="text")
    ks_fields = {
        "seed": "seed",
        "steps": "steps",
        "cfg": "cfg",
        "sampler_name": "sampler_name",
        "scheduler": "scheduler",
        "denoise": "denoise",
    }
    if isinstance(ks_raw, dict) and ks_raw.get("fields"):
        ks_fields.update(ks_raw["fields"])
    ks_inputs = inputs(ks_node)
    out["ksampler"] = {
        k: ks_inputs.get(field)
        for k, field in ks_fields.items()
        if ks_inputs.get(field) is not None
    }

    pos_node, pos_field = _node_ref(merged.get("positive_prompt"), "text")
    neg_node, neg_field = _node_ref(merged.get("negative_prompt"), "text")
    pos_inputs = inputs(pos_node)
    neg_inputs = inputs(neg_node)
    out["prompt_defaults"] = {
        "positive": pos_inputs.get(pos_field, "") if pos_inputs else "",
        "negative": neg_inputs.get(neg_field, "") if neg_inputs else "",
    }

    load_images: dict[str, Any] = {}
    for role, ref in (merged.get("load_images") or {}).items():
        li_node, li_field = _node_ref(ref, "image")
        li_inputs = inputs(li_node)
        load_images[role] = li_inputs.get(li_field, "") if li_inputs else ""
    out["load_images"] = load_images
    out["image_roles"] = list(load_images.keys())

    lora_defaults: list[dict[str, Any]] = []
    for lr in merged.get("lora_loaders") or []:
        lr_node, _ = _node_ref(lr if isinstance(lr, dict) else {"node": lr}, default_field="text")
        lr_fields = {
            "name": "lora_name",
            "strength_model": "strength_model",
            "strength_clip": "strength_clip",
        }
        if isinstance(lr, dict) and lr.get("fields"):
            lr_fields.update(lr["fields"])
        lr_inputs = inputs(lr_node)
        lora_defaults.append({
            "node": lr_node,
            "lora_name": lr_inputs.get(lr_fields["name"], ""),
            "strength_model": lr_inputs.get(lr_fields["strength_model"], 1.0),
            "strength_clip": lr_inputs.get(lr_fields["strength_clip"], 1.0),
        })
    out["lora_loaders"] = lora_defaults
    return out


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        cfg = _bootstrap_default_config()
        save_config(cfg)
        return cfg
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or _bootstrap_default_config()
    s3 = cfg.setdefault("s3", {})
    s3_defaults = _default_s3_config()
    changed = False
    for key, value in s3_defaults.items():
        if not str(s3.get(key) or "").strip():
            s3[key] = value
            changed = True
    wf_block = cfg.setdefault("workflows", {})
    registered = wf_block.setdefault("workflows", {})
    for key, spec in list(registered.items()):
        template = str((spec or {}).get("template") or "")
        if not template:
            continue
        template_path = _resolve_workflow_template_path(template)
        if not template_path.exists():
            registered.pop(key, None)
            changed = True
    if not registered:
        wf_block["workflows"] = json.loads(json.dumps(DEFAULT_CONFIG["workflows"]["workflows"]))
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    STORAGE.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


# ---------------------------------------------------------------------------
# run state, per-run pub/sub

class RunState:
    """In-memory state of a single batch. Persisted to status.json after
    every change. SSE subscribers receive events from `queue`."""

    def __init__(self, run_id: str, dir_: Path, run_type: str = "image"):
        self.run_id = run_id
        self.dir = dir_
        self.run_type = run_type
        self.jobs: list[dict[str, Any]] = []   # per-row status
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancelled: bool = False
        self.save_prompt: bool = False         # write a .txt prompt next to each output
        self.lora_folder: bool = False         # video: insert a <lora+strength> folder before girl
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        # `stalled` — джоба отправлена, но ComfyUI не отвечает (см. _video_handle).
        # Ключ объявлен явно, чтобы UI всегда получал его в counts, даже когда
        # таких джоб нет.
        counts = {"pending": 0, "running": 0, "stalled": 0, "done": 0, "failed": 0}
        for j in self.jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        return {
            "run_id": self.run_id,
            "run_type": self.run_type,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancelled": self.cancelled,
            "save_prompt": self.save_prompt,
            "lora_folder": self.lora_folder,
            "queued": self.run_id in RUN_QUEUE,
            "queue_position": (RUN_QUEUE.index(self.run_id) + 1
                               if self.run_id in RUN_QUEUE else None),
            "counts": counts,
            "total": len(self.jobs),
            "jobs": self.jobs,
        }

    async def emit(self, kind: str, **extra: Any) -> None:
        event = {"type": kind, "ts": time.time(), **extra}
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        await self.persist()

    async def persist(self) -> None:
        async with self._lock:
            (self.dir / "status.json").write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2))

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(q)


RUN_STATES: dict[str, RunState] = {}


def get_run(run_id: str) -> RunState:
    state = RUN_STATES.get(run_id)
    if state:
        return state
    d = RUNS / run_id
    if not d.exists():
        raise HTTPException(404, f"run {run_id} not found")
    state = RunState(run_id, d)
    sj = d / "status.json"
    if sj.exists():
        data = json.loads(sj.read_text())
        state.run_type = data.get("run_type", state.run_type)
        state.jobs = data.get("jobs", [])
        state.started_at = data.get("started_at")
        state.finished_at = data.get("finished_at")
        state.cancelled = data.get("cancelled", False)
        state.save_prompt = data.get("save_prompt", False)
        state.lora_folder = data.get("lora_folder", False)
    RUN_STATES[run_id] = state
    return state


# ---------------------------------------------------------------------------
# bridging PodPool → RunState events

class WebPool(PodPool):
    """PodPool subclass that emits per-row events to a RunState and writes
    thumbnails alongside downloaded images."""

    def __init__(self, *, state: RunState, **kwargs: Any):
        super().__init__(**kwargs)
        self.state = state
        self.cancelled_check = lambda: self.state.cancelled

    async def _handle(self, client, item):  # type: ignore[override]
        idx = item.idx
        await self._set_status(idx, "running", pod=client.cfg.name, error=None)
        try:
            files = await super()._handle(client, item)
            await self._make_thumbnails(files)
            await self._set_status(idx, "done", files=files,
                                    pod=client.cfg.name, error=None)
        except Exception as e:
            # Re-raise so PodPool's retry logic still works. The status
            # will flip to failed only when attempts run out (handled in
            # _worker via the new hook below).
            await self._set_status(idx, "running",
                                    error=f"{type(e).__name__}: {e}",
                                    pod=client.cfg.name)
            raise

    async def _worker(self, client, slot, pod_name=None):  # type: ignore[override]
        # Wrap parent so we can mark a job as failed after retries exhaust.
        # We re-implement just enough to keep behaviour identical except
        # for the final "failed" emission.
        name = pod_name or (client.cfg.name if client else "mock")
        while True:
            item = await self.queue.get()
            if item is None:
                self.queue.task_done()
                return
            if self.state.cancelled:
                # run cancelled — don't start new jobs; mark this one cancelled
                await self._set_status(item.idx, "failed", error="cancelled")
                self.queue.task_done()
                continue
            try:
                if self.dry_run:
                    await self._handle_dry(name, item)
                else:
                    await self._handle(client, item)
            except Exception as e:
                item.attempts += 1
                if item.attempts < self.max_attempts and not self.state.cancelled:
                    await self.queue.put(item)
                else:
                    await self._set_status(item.idx, "failed",
                                            error=f"{type(e).__name__}: {e}")
            finally:
                self.queue.task_done()

    # -- helpers ----------------------------------------------------------

    async def _make_thumbnails(self, files: list[str]) -> None:
        thumbs_root = self.state.dir / "thumbs"
        thumbs_root.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        for rel in files:
            src = self.outputs_dir / rel
            dst = thumbs_root / rel.replace("/", "__")
            dst = dst.with_suffix(".jpg")
            if dst.exists():
                continue
            await loop.run_in_executor(None, _make_thumb, src, dst)

    async def _set_status(self, idx: int, status: str, **extra: Any) -> None:
        # Update inline; merge with any prior fields.
        for j in self.state.jobs:
            if j["idx"] == idx:
                now = time.time()
                j["status"] = status
                j["updated_at"] = now
                _stamp_job_timing(j, status, now)
                for k, v in extra.items():
                    j[k] = v
                await self.state.emit("job_updated", job=j)
                return


def _make_thumb(src: Path, dst: Path) -> None:
    try:
        with Image.open(src) as im:
            im.thumbnail((256, 256))
            im.convert("RGB").save(dst, "JPEG", quality=80)
    except Exception as e:
        log.warning("thumb failed for %s: %s", src, e)


# ---------------------------------------------------------------------------
# auth (optional bearer)

def auth_dep(request: Request) -> None:
    token = os.environ.get("WEB_ADMIN_TOKEN")
    if not token:
        return
    got = request.headers.get("authorization", "")
    if got != f"Bearer {token}":
        raise HTTPException(401, "unauthorized")


# ---------------------------------------------------------------------------
# FastAPI app

app = FastAPI(title="Workflow Runner")

# Static frontend
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/video")
async def video_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "video.html")


@app.get("/api/video-runs/sample-csv")
async def video_sample_csv() -> StreamingResponse:
    """Download a pre-filled example CSV for video runs."""
    return StreamingResponse(
        iter([SAMPLE_CSV.encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="video_jobs_sample.csv"'},
    )


@app.get("/settings")
async def settings_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "settings.html")


@app.get("/scenarios")
async def scenarios_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "scenarios.html")


# ---- config CRUD ----------------------------------------------------------

@app.get("/api/config", dependencies=[Depends(auth_dep)])
async def get_config() -> dict[str, Any]:
    return load_config()


@app.put("/api/config", dependencies=[Depends(auth_dep)])
async def put_config(request: Request) -> dict[str, Any]:
    """Accepts either JSON dict or YAML text. If `workflows_yaml` is
    present as a string, it's parsed and replaces the workflows block."""
    ctype = (request.headers.get("content-type") or "").lower()
    body = await request.body()
    try:
        if "yaml" in ctype:
            cfg = yaml.safe_load(body.decode("utf-8"))
        else:
            cfg = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise HTTPException(400, f"parse error: {e}")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "config must be a mapping")
    # Support a `workflows_yaml` string field that gets parsed server-side.
    wf_text = cfg.pop("workflows_yaml", None)
    if isinstance(wf_text, str) and wf_text.strip():
        try:
            cfg["workflows"] = yaml.safe_load(wf_text)
        except yaml.YAMLError as e:
            raise HTTPException(400, f"workflows YAML parse error: {e}")
    save_config(cfg)
    return {"ok": True}


@app.get("/api/config/workflows_yaml", dependencies=[Depends(auth_dep)])
async def get_workflows_yaml() -> dict[str, Any]:
    """Return the workflows block as YAML text — used by the settings UI
    so the frontend doesn't need a YAML parser."""
    cfg = load_config()
    return {"yaml": yaml.safe_dump(cfg.get("workflows") or {},
                                    sort_keys=False, allow_unicode=True)}


@app.get("/api/workflows", dependencies=[Depends(auth_dep)])
async def list_workflows() -> dict[str, Any]:
    """List both: JSON files on disk in jobs/, and named entries in the
    active config. Helps the user see what's actually wired up."""
    jobs_dir = ROOT / "jobs"
    files: list[dict[str, Any]] = []
    if jobs_dir.exists():
        for p in sorted(jobs_dir.glob("*.json")):
            try:
                wf = json.loads(p.read_text())
                node_count = len(wf) if isinstance(wf, dict) else 0
            except Exception:
                node_count = -1
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "modified": p.stat().st_mtime,
                "node_count": node_count,
            })
    cfg = load_config()
    wf_block = cfg.get("workflows") or {}
    defaults = wf_block.get("defaults") or {}
    registered = wf_block.get("workflows") or {}
    workflows = [
        _workflow_meta(name, defaults, spec)
        for name, spec in sorted(registered.items())
    ]
    return {"files": files, "registered": list(registered.keys()), "workflows": workflows}


@app.post("/api/workflows/upload", dependencies=[Depends(auth_dep)])
async def upload_workflow(file: UploadFile = File(...)) -> dict[str, Any]:
    if not (file.filename or "").endswith(".json"):
        raise HTTPException(400, "expecting *.json")
    name = Path(file.filename).name              # strip any path
    body = await file.read()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"not valid JSON: {e}")
    if not isinstance(parsed, dict) or not all(
        isinstance(v, dict) and "class_type" in v
        for v in parsed.values() if isinstance(v, dict)
    ):
        raise HTTPException(400,
            "doesn't look like a ComfyUI API-format workflow "
            "(every node should have class_type). "
            "Use 'Save (API Format)' in ComfyUI Dev mode.")
    (ROOT / "jobs").mkdir(exist_ok=True)
    target = ROOT / "jobs" / name
    target.write_bytes(body)
    return {"ok": True, "name": name, "node_count": len(parsed)}


def _autodetect_mapping(wf: dict[str, Any]) -> dict[str, Any]:
    """Walk a workflow JSON and pick best-guess node IDs for each role.
    Returns the mapping block in the same shape `workflows.yaml` expects.

    Heuristics target current Flux pipelines we've seen so far. Order of
    preference within each role matches what those workflows put first.
    """
    by_type: dict[str, list[str]] = {}
    for nid, n in wf.items():
        if isinstance(n, dict):
            by_type.setdefault(n.get("class_type", ""), []).append(nid)

    def first_of(*types: str) -> str | None:
        for t in types:
            ids = by_type.get(t)
            if ids:
                return ids[0]
        return None

    def all_of(*types: str) -> list[str]:
        out: list[str] = []
        for t in types:
            out.extend(by_type.get(t, []))
        return out

    out: dict[str, Any] = {}
    ks = first_of("KSampler", "KSamplerAdvanced", "KSamplerWithNAG", "SamplerCustomAdvanced")
    if ks:
        out["ksampler"] = {"node": ks}

    # Positive prompt: prefer PromptLoopNode, then CR Text,
    # then the first CLIPTextEncode. Negative: the *last* CLIPTextEncode
    # (these graphs put negative last) — fallback to a literal-text node.
    pos = first_of("PromptLoopNode", "CR Text")
    clips = by_type.get("CLIPTextEncode", [])
    if not pos and clips:
        pos = clips[0]
    if pos:
        out["positive_prompt"] = {"node": pos}
    if clips:
        # pick the CLIPTextEncode whose text input is a literal string,
        # otherwise the last one in document order.
        neg = next((nid for nid in reversed(clips)
                    if isinstance(wf[nid].get("inputs", {}).get("text"), str)),
                   clips[-1])
        out["negative_prompt"] = {"node": neg}

    # Flows without CLIPTextEncode (e.g. Krea2 edit) keep the prompt as a
    # literal `prompt` string on the encode nodes. Positive = first non-empty,
    # negative = the remaining one (these graphs feed KSampler's negative from
    # the empty encode).
    if "positive_prompt" not in out:
        prompts = [nid for nid, n in wf.items()
                   if isinstance(n, dict)
                   and isinstance((n.get("inputs") or {}).get("prompt"), str)]
        if prompts:
            pos = next((nid for nid in prompts if wf[nid]["inputs"]["prompt"].strip()),
                       prompts[0])
            out["positive_prompt"] = {"node": pos, "field": "prompt"}
            rest = [nid for nid in prompts if nid != pos]
            if rest:
                out["negative_prompt"] = {"node": rest[-1], "field": "prompt"}

    lis = all_of("LoadImage")
    if lis:
        out["load_images"] = {"main": {"node": lis[0]}}
        for idx, nid in enumerate(lis[1:], start=1):
            out["load_images"][f"ref_{idx}"] = {"node": nid}

    saves = all_of("SaveImage")
    if saves:
        out["save_images"] = saves

    loras = all_of("Load Lora", "LoraLoader", "LoraLoaderModelOnly")
    out["lora_loaders"] = [{"node": loras[0]}] if loras else []

    return out


def _video_field_candidates(wf: dict[str, Any]) -> list[dict[str, Any]]:
    """Detected video field -> node, enriched with label/title/current value
    so the UI can render an editable checklist."""
    mapping = detect_video_mapping(wf)
    out: list[dict[str, Any]] = []
    for spec in VIDEO_FIELD_CATALOG:
        ref = mapping.get(spec["key"])
        node = wf.get(str(ref["node"])) if ref else None
        title = ""
        if isinstance(node, dict):
            meta = node.get("_meta") or {}
            title = str(meta.get("title") or node.get("class_type") or "")
        out.append({
            "key": spec["key"],
            "label": spec["label"],
            "node": ref["node"] if ref else "",
            "field": (ref or {}).get("field", spec["field"]),
            "title": title,
            "detected": ref is not None,
        })
    return out


@app.get("/api/workflows/{name}/detect", dependencies=[Depends(auth_dep)])
async def detect_workflow(name: str) -> dict[str, Any]:
    """Inspect an uploaded JSON: is it a video flow, and which nodes drive
    each known field. Drives the 'select CSV columns' UI."""
    safe = Path(name).name
    target = ROOT / "jobs" / safe
    if not target.exists():
        raise HTTPException(404, f"{safe} not found")
    try:
        wf = json.loads(target.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON: {e}")
    video = is_video_workflow(wf)
    uni = []
    if video:
        for c in full_columns(wf):
            lab = c["label"].lower()
            # Pre-tick what a per-job CSV almost always drives: uploaded files
            # (photos + reference video), prompts and the seed.
            default = (c["image"] or c.get("video")
                       or any(w in lab for w in ("positive", "negative", "seed", "prompt")))
            uni.append({**c, "default": default})
    return {
        "name": safe,
        "key": Path(safe).stem,
        "is_video": video,
        "video_fields": _video_field_candidates(wf) if video else [],
        "universal_fields": uni,
    }


@app.post("/api/workflows/{name}/register", dependencies=[Depends(auth_dep)])
async def register_workflow(name: str, body: dict[str, Any] = None) -> dict[str, Any]:
    """Register a workflow under workflows.workflows. Key defaults to the
    filename stem.

    Image flows: auto-detect KSampler/prompt/image/lora mapping.
    Video flows: store a `video_fields` mapping {field_key: {node, field}}.
      The frontend may pass the selected subset as body["video_fields"];
      otherwise all auto-detected fields are used.
    """
    safe = Path(name).name
    target = ROOT / "jobs" / safe
    if not target.exists():
        raise HTTPException(404, f"{safe} not found")
    try:
        wf = json.loads(target.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON: {e}")

    body = body or {}
    key = body.get("key") or Path(safe).stem

    if body.get("video_fields") is not None or is_video_workflow(wf):
        # ---- video flow ----
        selected = body.get("video_fields")
        if selected:
            # normalize: {key: {node, field}} or {key: node_id}
            video_fields: dict[str, Any] = {}
            for fk, ref in selected.items():
                if isinstance(ref, dict):
                    video_fields[fk] = {"node": str(ref.get("node")),
                                        "field": ref.get("field")}
                else:
                    video_fields[fk] = {"node": str(ref)}
        else:
            video_fields = detect_video_mapping(wf)
        entry = {
            "template": f"jobs/{safe}",
            "is_video": True,
            "video_fields": video_fields,
        }
        # Universal mode: the UI may instead send title-based columns the user
        # ticked. Stored only to drive the sample CSV — runtime patches any
        # universal column present in the CSV via full_columns translation.
        uni = body.get("universal_columns")
        if isinstance(uni, list) and uni:
            entry["universal_columns"] = [
                {"label": str(c.get("label")), "node": str(c.get("node")),
                 "field": str(c.get("field")), "image": bool(c.get("image")),
                 "video": bool(c.get("video"))}
                for c in uni if c.get("label") and c.get("node") and c.get("field")
            ]
        for k, v in (body.get("overrides") or {}).items():
            entry[k] = v
        mapping = entry
    else:
        # ---- image flow (unchanged) ----
        mapping = _autodetect_mapping(wf)
        mapping["template"] = f"jobs/{safe}"
        for k, v in (body.get("overrides") or {}).items():
            mapping[k] = v

    cfg = load_config()
    wfs = cfg.setdefault("workflows", {}).setdefault("workflows", {}) or {}
    wfs[key] = mapping
    cfg["workflows"]["workflows"] = wfs
    save_config(cfg)
    return {"ok": True, "key": key, "mapping": mapping}


@app.post("/api/workflows/register_all", dependencies=[Depends(auth_dep)])
async def register_all_workflows() -> dict[str, Any]:
    """Register every jobs/*.json whose key (filename stem) is not yet in the
    config. Existing entries are NEVER overwritten — hand-written mappings
    (multi-stage graphs, curated video fields) always win; re-register those
    individually if really needed."""
    jobs_dir = ROOT / "jobs"
    cfg = load_config()
    wfs = cfg.setdefault("workflows", {}).setdefault("workflows", {}) or {}
    registered_now: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for p in sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []:
        key = p.stem
        if key in wfs:
            skipped.append(key)
            continue
        try:
            wf = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            errors.append({"name": p.name, "error": f"invalid JSON: {e}"})
            continue
        if not isinstance(wf, dict) or not all(
            isinstance(v, dict) and "class_type" in v
            for v in wf.values() if isinstance(v, dict)
        ):
            errors.append({"name": p.name,
                           "error": "не ComfyUI API-format (нет class_type)"})
            continue
        if is_video_workflow(wf):
            mapping: dict[str, Any] = {
                "template": f"jobs/{p.name}",
                "is_video": True,
                "video_fields": detect_video_mapping(wf),
            }
        else:
            mapping = _autodetect_mapping(wf)
            mapping["template"] = f"jobs/{p.name}"
        wfs[key] = mapping
        registered_now.append(key)
    if registered_now:
        cfg["workflows"]["workflows"] = wfs
        save_config(cfg)
    return {"ok": True, "registered": registered_now,
            "skipped": skipped, "errors": errors}


@app.get("/api/workflows/{name}/sample_csv", dependencies=[Depends(auth_dep)])
async def workflow_sample_csv(name: str) -> StreamingResponse:
    """Return a one-row sample CSV for a registered video flow, with exactly
    the mapped columns pre-filled from the template's current values."""
    key = Path(name).name
    if key.endswith(".json"):
        key = key[:-5]
    cfg = load_config()
    registered = (cfg.get("workflows") or {}).get("workflows") or {}
    spec = registered.get(key)
    if not spec or not spec.get("is_video"):
        raise HTTPException(404, f"video flow '{key}' not registered")
    tmpl_path = _resolve_workflow_template_path(str(spec.get("template") or ""))
    if not tmpl_path.exists():
        raise HTTPException(404, f"template missing: {tmpl_path.name}")
    template = json.loads(tmpl_path.read_text())
    uni = spec.get("universal_columns")
    if uni:
        labels = [c["label"] for c in uni]
        csv_text = universal_sample_csv(template, key, only_labels=labels)
    else:
        mapping = spec.get("video_fields") or default_video_mapping(template)
        csv_text = sample_csv_for(template, mapping, key)
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers=_attachment_headers(f"{key}_sample.csv"),
    )


def _attachment_headers(filename: str) -> dict[str, str]:
    """Content-Disposition safe for non-ASCII names. HTTP headers are latin-1,
    so a Cyrillic/emoji flow name in `filename` would crash with
    UnicodeEncodeError (500). ASCII fallback + RFC 5987 filename* for the real
    unicode name."""
    import urllib.parse
    ascii_name = filename.encode("ascii", "ignore").decode().strip() or "download.csv"
    quoted = urllib.parse.quote(filename)
    return {"Content-Disposition":
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"}


@app.get("/api/workflows/{name}/universal_csv", dependencies=[Depends(auth_dep)])
async def workflow_universal_csv(name: str) -> StreamingResponse:
    """Universal sample CSV: EVERY editable node input as a title-based column,
    pre-filled with current values. Works for any registered video flow; the
    user trims to the columns they actually vary."""
    key = Path(name).name
    if key.endswith(".json"):
        key = key[:-5]
    cfg = load_config()
    registered = (cfg.get("workflows") or {}).get("workflows") or {}
    spec = registered.get(key)
    if not spec or not spec.get("is_video"):
        raise HTTPException(404, f"video flow '{key}' not registered")
    tmpl_path = _resolve_workflow_template_path(str(spec.get("template") or ""))
    if not tmpl_path.exists():
        raise HTTPException(404, f"template missing: {tmpl_path.name}")
    template = json.loads(tmpl_path.read_text())
    csv_text = universal_sample_csv(template, key)
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers=_attachment_headers(f"{key}_universal.csv"),
    )


@app.delete("/api/workflows/{name}", dependencies=[Depends(auth_dep)])
async def delete_workflow(name: str) -> dict[str, Any]:
    safe = Path(name).name
    target = ROOT / "jobs" / safe
    if not target.exists():
        raise HTTPException(404)
    target.unlink()

    cfg = load_config()
    wf_block = cfg.setdefault("workflows", {})
    registered = wf_block.setdefault("workflows", {})
    removed: list[str] = []
    for key, spec in list(registered.items()):
        template = str((spec or {}).get("template") or "")
        template_name = Path(template).name
        if template_name == safe:
            removed.append(key)
            registered.pop(key, None)
    if removed:
        save_config(cfg)
    return {"ok": True, "removed": removed}


@app.post("/api/pods/test", dependencies=[Depends(auth_dep)])
async def test_pod(body: dict[str, Any]) -> dict[str, Any]:
    """Pings a pod URL and reports basic info + missing custom nodes
    for the currently configured workflows."""
    import aiohttp
    url = body["url"].rstrip("/")
    api_key = body.get("api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    out: dict[str, Any] = {"url": url}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        try:
            async with s.get(f"{url}/system_stats", headers=headers) as r:
                out["status"] = r.status
                if r.status == 200:
                    sysinfo = await r.json()
                    out["comfyui_version"] = sysinfo["system"].get("comfyui_version")
                    out["device"] = (sysinfo.get("devices") or [{}])[0].get("name")
            async with s.get(f"{url}/object_info", headers=headers) as r:
                installed = set((await r.json()).keys()) if r.status == 200 else set()
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            return out
    # Check missing nodes against configured workflows
    cfg = load_config()
    missing_per_wf: dict[str, list[str]] = {}
    for name, spec in (cfg["workflows"].get("workflows") or {}).items():
        tpl = ROOT / spec.get("template", "")
        if not tpl.exists():
            continue
        wf = json.loads(tpl.read_text())
        types = {n.get("class_type") for n in wf.values() if isinstance(n, dict)}
        miss = sorted(t for t in types if t and t not in installed)
        if miss:
            missing_per_wf[name] = miss
    out["missing_nodes"] = missing_per_wf
    return out


# ---- runs ----------------------------------------------------------------

def _make_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _safe_tag(s: str | None, fallback: str) -> str:
    """Filesystem/S3-safe slug for naming (e.g. the CSV 'girl' value)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", (s or "").strip()).strip("_")
    return slug or fallback


def _run_day_tag(run_id: str) -> str:
    prefix = run_id.split("-", 1)[0]
    try:
        return datetime.strptime(prefix, "%Y%m%d").strftime("%d-%m")
    except ValueError:
        return time.strftime("%d-%m")


def _lora_folder_tag(load_loras_json: str) -> str:
    """Folder segment built from the ACTIVE loras in a load_loras_json value:
    '<name>_s<strength>' per enabled lora, joined by '__'. Empty if none/parse
    error. Lets video outputs be grouped by exact lora setup."""
    raw = (load_loras_json or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    parts: list[str] = []
    if isinstance(data, dict):
        for v in data.values():
            if not (isinstance(v, dict) and v.get("on")):
                continue
            name = Path(str(v.get("lora", ""))).stem
            if not name:
                continue
            try:
                strength = f"{float(v.get('strength', 1)):g}"
            except (ValueError, TypeError):
                strength = _safe_tag(str(v.get("strength", "")), "x")
            parts.append(f"{_safe_tag(name, 'lora')}_s{strength}")
    return "__".join(parts)


def _stamp_job_timing(job: dict[str, Any], status: str, now: float,
                      prev_status: str | None = None) -> None:
    """Per-job generation timing. Stamps the (latest) attempt start on
    'running' and computes `duration` (seconds) on 'done'/'failed'.

    Тонкость: в 'running' джоба входит и как новая попытка (retry — время старта
    надо перезаписать), и как возврат из 'stalled', когда ComfyUI снова начал
    отвечать. Во втором случае перезапись `started_ts` украла бы уже отработанное
    время из `duration`, поэтому переход именно `stalled -> running` времени не
    трогает; все остальные входы в 'running' штампуются как раньше."""
    if status == "running":
        if prev_status != "stalled":
            job["started_ts"] = now
    elif status in ("done", "failed"):
        start = job.get("started_ts")
        if start:
            job["duration"] = round(now - start, 1)


def _run_time_tag(run_id: str) -> str:
    """HHMMSS from the run_id (format YYYYMMDD-HHMMSS-hex). Used as the
    second-level output folder so each run gets its own subtree under the day."""
    parts = run_id.split("-")
    if len(parts) >= 2 and len(parts[1]) == 6 and parts[1].isdigit():
        return parts[1]
    return time.strftime("%H%M%S")


async def _archive_run_meta(state: "RunState", s3: "S3Uploader | None") -> None:
    """Snapshot the run's inputs/state into the run folder root
    (outputs/<day>/<HHMMSS_workflow>/) so they get mirrored to S3 next to the
    results.

    Copies the original jobs.csv and the final status.json (the latter carries
    the resolved seeds — incl. randomly generated ones — so a run is fully
    reproducible from S3 alone). A run mixing several workflows gets a copy in
    each workflow folder. Best-effort: never breaks run completion.
    """
    try:
        await state.persist()                      # status.json reflects final state
        outputs_dir = state.dir / "outputs"
        day = _run_day_tag(state.run_id)
        hhmmss = _run_time_tag(state.run_id)
        # The run-folder names must match the actual output layout: image runs
        # use path_builder.slug, video runs use _safe_tag.
        names = {j.get("workflow") for j in state.jobs if j.get("workflow")}
        if state.run_type == "video":
            folders = {f"{hhmmss}_{_safe_tag(w, 'video')}" for w in names}
        else:
            folders = {f"{hhmmss}_{slug(w)}" for w in names}
        if not folders:
            folders = {hhmmss}

        sources = [state.dir / "jobs.csv", state.dir / "status.json"]
        for folder in folders:
            meta_dir = outputs_dir / day / folder
            meta_dir.mkdir(parents=True, exist_ok=True)
            for src in sources:
                if not src.exists():
                    continue
                dest = meta_dir / src.name
                dest.write_bytes(src.read_bytes())
                if s3:
                    rel = dest.relative_to(outputs_dir).as_posix()
                    try:
                        key = await s3.upload(dest, rel)
                        log.info("s3 ↑ run meta %s", key)
                    except Exception as e:
                        log.warning("s3 upload failed for %s: %s", dest, e)
    except Exception as e:
        log.warning("archive run meta failed: %s", e)


@app.post("/api/runs", dependencies=[Depends(auth_dep)])
async def create_run(
    csv_file: UploadFile = File(...),
    photos: list[UploadFile] = File(default=[]),
    run_type: str = Form(default="image"),
    save_prompt: bool = Form(default=False),
) -> dict[str, Any]:
    run_id = _make_run_id()
    rd = RUNS / run_id
    (rd / "inputs").mkdir(parents=True, exist_ok=True)
    (rd / "outputs").mkdir(parents=True, exist_ok=True)
    (rd / "thumbs").mkdir(parents=True, exist_ok=True)

    csv_bytes = await csv_file.read()
    (rd / "jobs.csv").write_bytes(csv_bytes)

    saved_photos: list[str] = []
    for p in photos:
        # strip any path components from the uploaded name
        name = Path(p.filename or "unnamed").name
        target = rd / "inputs" / name
        target.write_bytes(await p.read())
        saved_photos.append(name)

    # Parse CSV into initial job rows
    state = RunState(run_id, rd, run_type=run_type or "image")
    state.save_prompt = save_prompt
    jobs = load_jobs(rd / "jobs.csv")
    for i, j in enumerate(jobs):
        input_images = list(j.input_images or ((j.input_image,) if j.input_image else ()))
        state.jobs.append({
            "idx": i,
            "status": "pending",
            "workflow": j.workflow,
            "girl": j.girl,
            "lora_name": j.lora_name,
            "prompt_positive": j.prompt_positive,
            "input_image": j.input_image,
            "input_images": input_images,
            "seed": j.seed,
            "extra": j.extra,
            "task_type": state.run_type,
            "files": [],
        })
    await state.persist()
    RUN_STATES[run_id] = state

    return {
        "run_id": run_id,
        "total": len(jobs),
        "run_type": state.run_type,
        "photos": saved_photos,
        "missing_inputs": _missing_inputs(state),
    }


def _missing_inputs(state: RunState) -> list[str]:
    inputs = state.dir / "inputs"
    needed = {
        f
        for j in state.jobs
        for f in (j.get("input_files") or j.get("input_images") or [j.get("input_image")])
        if f
    }
    return sorted(n for n in needed if not (inputs / n).exists())


@app.post("/api/runs/{run_id}/inputs", dependencies=[Depends(auth_dep)])
async def add_run_inputs(
    run_id: str,
    photos: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    """Add input photos to an existing run, in small batches. Lets the frontend
    upload many references without one giant multipart request (the RunPod proxy
    drops huge/slow uploads). Works for image and video runs alike."""
    state = get_run(run_id)
    saved: list[str] = []
    for p in photos:
        name = Path(p.filename or "unnamed").name
        (state.dir / "inputs" / name).write_bytes(await p.read())
        saved.append(name)
    return {"ok": True, "saved": saved, "missing_inputs": _missing_inputs(state)}


# ---------------------------------------------------------------------------
# Run launch + global sequential queue
#
# "Запустить" launches immediately (fire-and-forget). "Поставить в очередь"
# appends the run to RUN_QUEUE; a single worker runs queued runs one at a time
# and only when nothing else is active — so queued runs never overlap each
# other or an immediately-started run. (A GPU processes one job at a time
# anyway, so serial is the natural mode — see README/onboarding note.)

RUN_QUEUE: list[str] = []                 # run_ids waiting, FIFO by enqueue order
_queue_task: "asyncio.Task | None" = None


def _validate_run(state: RunState) -> None:
    """Cheap pre-flight shared by start + queue. Raises HTTPException so the
    user gets immediate feedback; the heavy build happens at execution time."""
    if state.started_at and not state.finished_at:
        raise HTTPException(409, "already running")
    if state.run_id in RUN_QUEUE:
        raise HTTPException(409, "already queued")
    missing = _missing_inputs(state)
    if missing:
        raise HTTPException(400, f"missing input photos: {missing}")
    cfg = load_config()
    pods = cfg.get("pods") or []
    if not pods:
        raise HTTPException(400, "no pods configured (see /settings)")
    registered = (cfg.get("workflows") or {}).get("workflows") or {}
    unknown = sorted({j.get("workflow") for j in state.jobs
                      if j.get("workflow") and j["workflow"] not in registered})
    if unknown:
        raise HTTPException(400, f"workflow(s) not registered: {', '.join(unknown)}")
    # Проверки адреса пода здесь нет намеренно: раннер не решает за пользователя,
    # какой URL правильный (proxy-адрес законен, например для ComfyUI на другой
    # машине). Недостижимый ComfyUI ловится уже во время ожидания результата —
    # PodClient.wait() падает с внятной ошибкой вместо молчаливого висения.


def _any_run_active(exclude: str | None = None) -> bool:
    for rid, st in RUN_STATES.items():
        if rid == exclude:
            continue
        if st.started_at and not st.finished_at:
            return True
    return False


async def _execute_image_run(state: RunState) -> None:
    cfg = load_config()
    pod_cfgs = [
        PodConfig(name=p["name"], url=p["url"],
                  max_parallel=p.get("max_parallel", 1), api_key=p.get("api_key"))
        for p in cfg.get("pods") or []
    ]
    if not pod_cfgs:
        raise RuntimeError("no pods configured")
    # workflows.yaml on the fly (templates resolved to absolute paths)
    wf_block = json.loads(json.dumps(cfg["workflows"]))
    for spec in (wf_block.get("workflows") or {}).values():
        t = spec.get("template")
        if t and not Path(t).is_absolute():
            spec["template"] = str((ROOT / t).resolve())
    wf_path = state.dir / "workflows.yaml"
    wf_path.write_text(yaml.safe_dump(wf_block, sort_keys=False, allow_unicode=True))
    registry = WorkflowRegistry(wf_path)
    for j in state.jobs:
        registry.get(j["workflow"])
    s3 = _s3_from_config(cfg.get("s3") or {})
    state.started_at = time.time()
    state.finished_at = None
    state.cancelled = False
    await state.emit("run_started")
    pool = WebPool(
        state=state, pods=pod_cfgs, workflows=registry,
        inputs_dir=state.dir / "inputs", outputs_dir=state.dir / "outputs",
        day_tag=_run_day_tag(state.run_id), run_tag=_run_time_tag(state.run_id),
        save_prompt=state.save_prompt, s3=s3,
    )
    csv_jobs = load_jobs(state.dir / "jobs.csv")
    await _run_pool(state, pool, csv_jobs)


async def _execute_video_run(state: RunState) -> None:
    cfg = load_config()
    pod_cfgs = [
        PodConfig(name=p["name"], url=p["url"],
                  max_parallel=p.get("max_parallel", 1), api_key=p.get("api_key"))
        for p in cfg.get("pods") or []
    ]
    if not pod_cfgs:
        raise RuntimeError("no pods configured")
    video_jobs = load_video_jobs(state.dir / "jobs.csv")
    flows = _resolve_video_flows(cfg, video_jobs)
    s3 = _s3_from_config(cfg.get("s3") or {})
    state.started_at = time.time()
    state.finished_at = None
    state.cancelled = False
    await state.emit("run_started")
    await _run_video_pool(state, pod_cfgs, video_jobs, flows, s3)


async def _execute_run(state: RunState) -> None:
    """Build + run this run to completion (awaits). Dispatches by run_type and
    always leaves it finished, even if the launch itself fails."""
    try:
        if state.run_type == "video":
            await _execute_video_run(state)
        else:
            await _execute_image_run(state)
    except Exception as e:
        log.exception("run %s failed to launch: %s", state.run_id, e)
        for j in state.jobs:
            if j["status"] in ("pending", "running", "stalled"):
                j["status"] = "failed"
                j["error"] = f"launch failed: {e}"
        state.finished_at = state.finished_at or time.time()
        await state.emit("run_finished")


def _ensure_queue_worker() -> None:
    global _queue_task
    if _queue_task is None or _queue_task.done():
        _queue_task = asyncio.create_task(_queue_loop())


async def _queue_loop() -> None:
    """Process RUN_QUEUE one run at a time, never overlapping an active run."""
    global _queue_task
    try:
        while RUN_QUEUE:
            while _any_run_active():       # wait out anything still running
                await asyncio.sleep(2)
            if not RUN_QUEUE:
                break
            run_id = RUN_QUEUE[0]
            state = RUN_STATES.get(run_id)
            if state is None:
                RUN_QUEUE.pop(0)
                continue
            await _execute_run(state)      # blocks until this run finishes
            if RUN_QUEUE and RUN_QUEUE[0] == run_id:
                RUN_QUEUE.pop(0)
            # refresh positions for whoever is left waiting
            for rid in list(RUN_QUEUE):
                st = RUN_STATES.get(rid)
                if st:
                    await st.emit("queue_update")
    finally:
        _queue_task = None


@app.post("/api/runs/{run_id}/queue", dependencies=[Depends(auth_dep)])
async def queue_run(run_id: str) -> dict[str, Any]:
    """Add a run to the global sequential queue. Starts automatically when no
    other run is active; otherwise waits its turn (FIFO by enqueue order).
    Works for both image and video runs."""
    state = get_run(run_id)
    _validate_run(state)
    RUN_QUEUE.append(run_id)
    position = len(RUN_QUEUE)
    await state.emit("queued", position=position)
    _ensure_queue_worker()
    return {"ok": True, "queued": True, "position": position}


@app.post("/api/runs/{run_id}/start", dependencies=[Depends(auth_dep)])
async def start_run(run_id: str) -> dict[str, Any]:
    """Launch a run immediately (fire-and-forget), independent of the queue."""
    state = get_run(run_id)
    _validate_run(state)
    asyncio.create_task(_execute_run(state))
    return {"ok": True}


async def _run_pool(state: RunState, pool: WebPool, jobs) -> None:
    try:
        await pool.run(jobs)
    except Exception as e:
        log.exception("pool crashed: %s", e)
    finally:
        state.finished_at = time.time()
        await _archive_run_meta(state, pool.s3)
        await state.emit("run_finished")


def _s3_from_config(s3cfg: dict[str, Any]) -> S3Uploader | None:
    bucket = (s3cfg.get("bucket") or os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        return None
    try:
        return S3Uploader(
            bucket=bucket,
            prefix=s3cfg.get("prefix") or os.environ.get("S3_PREFIX", ""),
            region=s3cfg.get("region") or os.environ.get("S3_REGION") or None,
            endpoint_url=(s3cfg.get("endpoint_url")
                          or os.environ.get("S3_ENDPOINT_URL") or None),
        )
    except Exception as e:
        log.warning("S3 init failed: %s", e)
        return None


def _cleanup_path(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


@app.get("/api/runs", dependencies=[Depends(auth_dep)])
async def list_runs(run_type: str | None = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if RUNS.exists():
        for d in sorted(RUNS.iterdir(), reverse=True):
            sj = d / "status.json"
            if not sj.exists():
                continue
            try:
                data = json.loads(sj.read_text())
            except Exception:
                continue
            if run_type and data.get("run_type", "image") != run_type:
                continue
            items.append({
                "run_id": d.name,
                "run_type": data.get("run_type", "image"),
                "total": data.get("total", 0),
                "counts": data.get("counts", {}),
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
            })
    return {"runs": items}


@app.get("/api/runs/{run_id}/status", dependencies=[Depends(auth_dep)])
async def run_status(run_id: str) -> dict[str, Any]:
    return get_run(run_id).snapshot()


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    state = get_run(run_id)

    async def gen() -> AsyncIterator[bytes]:
        # initial snapshot
        yield _sse_event({"type": "snapshot", "data": state.snapshot()})
        q = state.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield _sse_event(ev)
                except asyncio.TimeoutError:
                    # heartbeat to keep proxies from closing the stream
                    yield b": ping\n\n"
        finally:
            state.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse_event(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


@app.get("/api/runs/{run_id}/file/{kind}/{path:path}")
async def serve_file(run_id: str, kind: str, path: str) -> FileResponse:
    """kind ∈ {output, thumb}"""
    state = get_run(run_id)
    sub = {"output": "outputs", "thumb": "thumbs"}.get(kind)
    if sub is None:
        raise HTTPException(400, "bad kind")
    target = (state.dir / sub / path).resolve()
    # make sure we don't escape the run dir
    if not str(target).startswith(str(state.dir.resolve())):
        raise HTTPException(400, "bad path")
    if not target.exists():
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/api/runs/{run_id}/archive", dependencies=[Depends(auth_dep)])
async def download_run_archive(run_id: str) -> FileResponse:
    state = get_run(run_id)
    outputs = state.dir / "outputs"
    if not outputs.exists():
        raise HTTPException(404)

    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{run_id}-", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    written = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(outputs.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in image_exts:
                    continue
                zf.write(p, arcname=p.relative_to(outputs).as_posix())
                written += 1
        if written == 0:
            raise HTTPException(404, "no image files found for this run")
    except Exception:
        _cleanup_path(str(tmp_path))
        raise

    return FileResponse(
        tmp_path,
        filename=f"{run_id}.zip",
        media_type="application/zip",
        background=BackgroundTask(_cleanup_path, str(tmp_path)),
    )


@app.delete("/api/runs/{run_id}", dependencies=[Depends(auth_dep)])
async def delete_run(run_id: str) -> dict[str, Any]:
    """Deletes the whole run directory (jobs.csv + inputs + outputs +
    thumbs + status.json). Refuses if the run is still actively running."""
    state = RUN_STATES.get(run_id)
    if state and state.started_at and not state.finished_at and not state.cancelled:
        raise HTTPException(409, "run is still active — cancel it first")
    target = (RUNS / run_id).resolve()
    if not str(target).startswith(str(RUNS.resolve())) or not target.exists():
        raise HTTPException(404)
    import shutil
    shutil.rmtree(target)
    RUN_STATES.pop(run_id, None)
    if run_id in RUN_QUEUE:
        RUN_QUEUE.remove(run_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(auth_dep)])
async def cancel_run(run_id: str) -> dict[str, Any]:
    state = get_run(run_id)
    state.cancelled = True
    # Drop from the queue if it was waiting.
    if run_id in RUN_QUEUE:
        RUN_QUEUE.remove(run_id)
    await state.emit("run_cancelled")
    # Mark not-yet-finished jobs as cancelled (running ones get aborted below).
    # `stalled` — тоже незавершённая джоба: без неё отмена оставила бы её висеть.
    for j in state.jobs:
        if j["status"] in ("pending", "running", "stalled"):
            j["status"] = "failed"
            j["error"] = "cancelled"
    # Abort the in-flight generation on the pod(s) — /interrupt stops ComfyUI's
    # current prompt now; the worker's wait() also bails on the cancelled flag.
    import aiohttp
    cfg = load_config()
    for p in (cfg.get("pods") or []):
        url = (p.get("url") or "").rstrip("/")
        if not url:
            continue
        headers = {"Authorization": f"Bearer {p['api_key']}"} if p.get("api_key") else {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(f"{url}/interrupt", headers=headers) as r:
                    await r.read()
        except Exception as e:
            log.warning("interrupt %s failed: %s", url, e)
    await state.persist()
    return {"ok": True}


# ---- video runs (LTX-specific, direct node patching) -------------------

# Legacy fallback template, used only when a CSV row's `workflow` key is not
# a registered video flow (keeps older single-template setups working).
VIDEO_WORKFLOW_TEMPLATE_PATH = ROOT / "video_workflow.json"


def _video_job_input_files(job: VideoJob, cfg: dict[str, Any]) -> list[str]:
    """Every file this job expects under the run's inputs/ — photos AND reference
    videos, catalog columns as well as universal (title-based) ones.

    Feeds the pre-flight "missing inputs" check: a forgotten 3rd reference photo
    or reference clip is reported before the run starts, instead of blowing up
    mid-flight inside the pod worker.
    """
    names = [job.input_image, job.input_image_last, job.input_video]
    spec = ((cfg.get("workflows") or {}).get("workflows") or {}).get(
        (job.workflow or "").strip()) or {}
    tmpl = str(spec.get("template") or "")
    if tmpl and job.extra:
        try:
            template = json.loads(_resolve_workflow_template_path(tmpl).read_text())
            cols = {c["label"]: c for c in full_columns(template)}
        except (OSError, ValueError):
            cols = {}
        for label, raw in job.extra.items():
            col = cols.get(label)
            if col and (col.get("image") or col.get("video")):
                names.append(str(raw or "").strip())
    return [n for n in names if n]


def _resolve_video_flows(cfg: dict[str, Any], jobs: list[VideoJob]) -> dict[str, tuple[dict, dict]]:
    """For each distinct `workflow` key used by the jobs, resolve
    (template, mapping). Registered video flows win; otherwise fall back to
    the legacy single template. Raises HTTPException listing unresolved keys.
    """
    registered = (cfg.get("workflows") or {}).get("workflows") or {}
    flows: dict[str, tuple[dict, dict]] = {}
    unresolved: list[str] = []
    for key in {(j.workflow or "").strip() for j in jobs}:
        spec = registered.get(key)
        if spec and spec.get("template"):
            tmpl_path = _resolve_workflow_template_path(str(spec["template"]))
            if tmpl_path.exists():
                template = json.loads(tmpl_path.read_text())
                mapping = spec.get("video_fields") or default_video_mapping(template)
                flows[key] = (template, mapping)
                continue
        # fallback to legacy template
        if VIDEO_WORKFLOW_TEMPLATE_PATH.exists():
            template = load_video_template(VIDEO_WORKFLOW_TEMPLATE_PATH)
            flows[key] = (template, default_video_mapping(template))
            continue
        unresolved.append(key)
    if unresolved:
        raise HTTPException(
            400,
            "не зарегистрированы видео-флоу для CSV-колонки workflow: "
            + ", ".join(sorted(unresolved))
            + ". Загрузите и зарегистрируйте их в Settings.",
        )
    return flows


def _missing_video_flows(cfg: dict[str, Any], jobs: list[VideoJob]) -> list[str]:
    """Workflow keys from the CSV that can't run: not registered, or the
    registered template JSON is gone from disk. Empty key is the legacy
    single-template path and isn't flagged (matches _validate_run)."""
    registered = (cfg.get("workflows") or {}).get("workflows") or {}
    missing: set[str] = set()
    for key in {(j.workflow or "").strip() for j in jobs}:
        if not key:
            continue
        spec = registered.get(key)
        if not spec:
            missing.add(f"{key} — не зарегистрирован")
        elif not spec.get("template") or not _resolve_workflow_template_path(
                str(spec["template"])).exists():
            missing.add(f"{key} — нет файла шаблона "
                        f"({spec.get('template') or '?'})")
    return sorted(missing)


@app.post("/api/video-runs", dependencies=[Depends(auth_dep)])
async def create_video_run(
    csv_file: UploadFile = File(...),
    photos: list[UploadFile] = File(default=[]),
    save_prompt: bool = Form(default=False),
    lora_folder: bool = Form(default=False),
) -> dict[str, Any]:
    """Create a video batch run from a CSV with LTX-specific columns."""
    run_id = _make_run_id()
    rd = RUNS / run_id
    (rd / "inputs").mkdir(parents=True, exist_ok=True)
    (rd / "outputs").mkdir(parents=True, exist_ok=True)
    (rd / "thumbs").mkdir(parents=True, exist_ok=True)

    csv_bytes = await csv_file.read()
    (rd / "jobs.csv").write_bytes(csv_bytes)

    saved_photos: list[str] = []
    for p in photos:
        name = Path(p.filename or "unnamed").name
        target = rd / "inputs" / name
        target.write_bytes(await p.read())
        saved_photos.append(name)

    state = RunState(run_id, rd, run_type="video")
    state.save_prompt = save_prompt
    state.lora_folder = lora_folder
    jobs = load_video_jobs(rd / "jobs.csv")
    cfg = load_config()
    for i, j in enumerate(jobs):
        state.jobs.append({
            "idx": i,
            "status": "pending",
            "workflow": j.workflow,
            "girl": j.girl,
            "scenario": j.scenario,
            "lora_name": j.load_loras_json[:60] + "..." if len(j.load_loras_json) > 60 else j.load_loras_json,
            "prompt_positive": j.prompt_positive,
            "input_image": j.input_image,
            "input_images": [x for x in (j.input_image, j.input_image_last) if x],
            "input_video": j.input_video,
            # every referenced file (incl. universal photo/video columns)
            "input_files": _video_job_input_files(j, cfg),
            "seed": j.seed,
            "video_length_seconds": j.video_length_seconds,
            "video_width": j.video_width,
            "video_height": j.video_height,
            "extra": j.extra,
            "task_type": "video",
            "files": [],
        })
    await state.persist()
    RUN_STATES[run_id] = state

    missing = _missing_inputs(state)
    return {
        "run_id": run_id,
        "total": len(jobs),
        "run_type": "video",
        "photos": saved_photos,
        "missing_inputs": missing,
        "missing_flows": _missing_video_flows(load_config(), jobs),
    }


@app.post("/api/video-runs/{run_id}/start", dependencies=[Depends(auth_dep)])
async def start_video_run(run_id: str) -> dict[str, Any]:
    """Launch a video run immediately (fire-and-forget)."""
    state = get_run(run_id)
    if state.run_type != "video":
        raise HTTPException(400, "not a video run")
    _validate_run(state)
    asyncio.create_task(_execute_run(state))
    return {"ok": True}


async def _run_video_pool(
    state: RunState,
    pod_cfgs: list[PodConfig],
    jobs: list[VideoJob],
    flows: dict[str, tuple[dict, dict]],
    s3: Any,
) -> None:
    """Execute video jobs, each patched via its registered flow mapping."""
    import aiohttp
    from pod_client import PodClient, PodError

    queue: asyncio.Queue = asyncio.Queue()
    for i, j in enumerate(jobs):
        await queue.put((i, j))

    timeout = aiohttp.ClientTimeout(total=None, sock_read=600)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            workers = []
            for pod_cfg in pod_cfgs:
                client = PodClient(pod_cfg, session)
                for slot in range(pod_cfg.max_parallel):
                    workers.append(asyncio.create_task(
                        _video_worker(state, client, queue, flows, s3),
                        name=f"{pod_cfg.name}#{slot}",
                    ))
            # poison pills
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)
    except Exception as e:
        log.exception("video pool crashed: %s", e)
    finally:
        state.finished_at = time.time()
        await _archive_run_meta(state, s3)
        await state.emit("run_finished")


async def _video_worker(
    state: RunState,
    client: Any,
    queue: asyncio.Queue,
    flows: dict[str, tuple[dict, dict]],
    s3: Any,
    max_attempts: int = 3,
) -> None:
    from pod_client import PodError

    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        idx, job = item
        if state.cancelled:
            await _video_set_status(state, idx, "failed", error="cancelled")
            queue.task_done()
            continue
        attempts = 0
        while attempts < max_attempts:
            try:
                await _video_handle(state, client, idx, job, flows, s3)
                break
            except Exception as e:
                attempts += 1
                log.warning("[%s] video job %d failed (attempt %d): %s",
                            client.cfg.name, idx, attempts, e)
                if attempts >= max_attempts or state.cancelled:
                    await _video_set_status(state, idx, "failed",
                                            error=f"{type(e).__name__}: {e}")
                    break
        queue.task_done()


async def _video_handle(
    state: RunState,
    client: Any,
    idx: int,
    job: VideoJob,
    flows: dict[str, tuple[dict, dict]],
    s3: Any,
) -> None:
    """Submit one video job and download outputs."""
    await _video_set_status(state, idx, "running", pod=client.cfg.name, error=None)

    template, mapping = flows[(job.workflow or "").strip()]

    # Upload input file(s) from the run's inputs/ dir into ComfyUI's input dir.
    # Photos (FLF2V flows use a second, "last" frame) and reference videos
    # (ref2v flows like MiniMax H3) go through the same endpoint.
    async def _upload_ref(name: str, what: str = "file") -> str:
        if not name:
            return ""
        local = state.dir / "inputs" / name
        if not local.exists():
            raise FileNotFoundError(f"input {what} missing: {local}")
        return await client.upload_file(local)

    remote_image = await _upload_ref(job.input_image, "image")
    remote_image_last = await _upload_ref(job.input_image_last, "image")
    remote_video = await _upload_ref(job.input_video, "video")

    day_tag = _run_day_tag(state.run_id)
    # Layout: <day>/<HHMMSS_workflow>/[<lora+strength>/]<girl>. HHMMSS groups
    # each run; the optional lora folder (toggle) groups by exact lora setup.
    wf_tag = f"{_run_time_tag(state.run_id)}_{_safe_tag(job.workflow, 'video')}"
    # folder/filename carry the CSV 'girl' value so outputs are grouped per subject
    girl_tag = _safe_tag(job.girl, f"{idx:05d}")
    lora_tag = _lora_folder_tag(job.load_loras_json) if state.lora_folder else ""
    rel_parts = [day_tag, wf_tag] + ([lora_tag] if lora_tag else []) + [girl_tag]
    rel_dir = "/".join(rel_parts)
    prefix = f"video/{rel_dir}/{girl_tag}_{idx:05d}_seed{job.seed}"

    # CSV row -> field-key values. Only fields present in the flow's mapping
    # are patched; everything else keeps the template default.
    values = {
        "input_image": remote_image,
        "input_image_last": remote_image_last,
        "input_video": remote_video,
        "prompt_positive": job.prompt_positive,
        "prompt_negative": job.prompt_negative,
        "seed": job.seed,
        "video_length_seconds": job.video_length_seconds,
        "video_width": job.video_width,
        "video_height": job.video_height,
        "sigmas_first_pass": job.sigmas_first_pass,
        "sigmas_final_pass": job.sigmas_final_pass,
        "cfg_first_pass": job.cfg_first_pass,
        "cfg_final_pass": job.cfg_final_pass,
        "audio_volume_first": job.audio_volume_first,
        "audio_volume_final": job.audio_volume_final,
        "steps": job.steps,
        "denoise": job.denoise,
        "scheduler": job.scheduler,
        "sampler_name": job.sampler_name,
        "checkpoint_name": job.checkpoint_name,
        "diffusion_model_name": job.diffusion_model_name,
        "lora_name": job.lora_name,
        "lora_strength": job.lora_strength,
        "load_loras_json": job.load_loras_json,
        "load_distilled_lora_json": job.load_distilled_lora_json,
        "load_distilled_lora_final_json": job.load_distilled_lora_final_json,
    }
    # Universal columns: any CSV column matching a title-based label (from
    # full_columns) is translated to a raw <node>.<field> patch; image and video
    # columns are uploaded first. Numeric <node>.<field> columns pass through
    # untouched.
    extra_patches = dict(job.extra or {})
    cols = {c["label"]: c for c in full_columns(template)}
    for label in list(extra_patches.keys()):
        spec = cols.get(label)
        if not spec:
            continue
        raw = str(extra_patches.pop(label) or "").strip()
        if not raw:
            continue
        if spec["image"] or spec.get("video"):
            what = "video" if spec.get("video") else "image"
            extra_patches[f'{spec["node"]}.{spec["field"]}'] = await _upload_ref(raw, what)
        elif spec.get("dual"):                       # mxSlider — write both Xi & Xf
            extra_patches[f'{spec["node"]}.Xi'] = raw
            extra_patches[f'{spec["node"]}.Xf'] = raw
        else:
            extra_patches[f'{spec["node"]}.{spec["field"]}'] = raw

    wf = build_video_workflow(template, values, mapping,
                              save_prefix=prefix, extra=extra_patches)

    log.info("[%s] submit video job %d girl=%s seed=%d",
             client.cfg.name, idx, job.girl, job.seed)
    prompt_id = await client.submit(wf)
    # Video generation (esp. long clips / 22B models) easily exceeds the 600s
    # image default → раннер ложно помечал failed. 30 min, env-tunable.
    wait_timeout = float(os.environ.get("VIDEO_WAIT_TIMEOUT_S", "1800"))
    # Наблюдаемость ожидания: пока ComfyUI считает, джоба стоит в `running` и
    # снаружи это неотличимо от «повисло». Раз в UNREACHABLE_LOG_EVERY опросов
    # пишем, сколько ждём, и помечаем джобу `stalled`, если ComfyUI отвалился —
    # статус виден в UI и в /status, а не только в логе.
    waited = {"polls": 0, "stalled": False}

    def _on_poll(poll_state: str, detail: str | None) -> None:
        waited["polls"] += 1
        secs = waited["polls"] * 2
        if poll_state == "unreachable":
            if not waited["stalled"]:
                waited["stalled"] = True
                log.warning("[%s] video job %d: ComfyUI не отвечает (%s) — жду",
                            client.cfg.name, idx, detail)
                asyncio.create_task(_video_set_status(
                    state, idx, "stalled", pod=client.cfg.name,
                    error=f"ComfyUI не отвечает: {detail}"))
        else:
            if waited["stalled"]:
                waited["stalled"] = False
                log.info("[%s] video job %d: ComfyUI снова отвечает",
                         client.cfg.name, idx)
                asyncio.create_task(_video_set_status(
                    state, idx, "running", pod=client.cfg.name, error=None))
            if secs and secs % 120 == 0:
                log.info("[%s] video job %d: жду результат, %d мин",
                         client.cfg.name, idx, secs // 60)

    entry = await client.wait(prompt_id, timeout=wait_timeout,
                              should_cancel=lambda: state.cancelled,
                              on_poll=_on_poll)

    # Download all outputs (video files). Top-level folder = run date (DD-MM),
    # then the previous per-job layout. This propagates everywhere rel paths
    # are used: local outputs, S3 key, status files list, thumbnails, archive.
    out_dir = state.dir / "outputs" / Path(*rel_parts)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save node(s): VHS_VideoCombine or SaveVideo (detect ids from template).
    save_nodes = {
        nid for nid, n in template.items()
        if isinstance(n, dict) and n.get("class_type") in VIDEO_SAVE_CLASSES
    } or {"61"}
    outputs = client.outputs_from_history(entry, save_nodes)
    if not outputs:
        # Fallback: grab all outputs
        outputs = client.outputs_from_history(entry, set())
    if not outputs:
        raise Exception(f"no outputs returned for video job {idx}")

    local_files: list[Path] = []
    for img in outputs:
        dest = out_dir / Path(img["filename"]).name
        await client.download(img["filename"], img["subfolder"], img["type"], dest)
        local_files.append(dest)

    # Optional: write the positive prompt next to each video (same stem),
    # uploaded to S3 too but kept out of the returned media list.
    prompt_files: list[Path] = []
    if state.save_prompt and (job.prompt_positive or "").strip():
        for p in local_files:
            txt = p.with_suffix(".txt")
            txt.write_text(job.prompt_positive, encoding="utf-8")
            prompt_files.append(txt)

    # Always: write the exact patched workflow next to each video (same stem),
    # so the generating graph can be reproduced 1:1 in ComfyUI.
    graph_files: list[Path] = []
    wf_json = json.dumps(wf, ensure_ascii=False, indent=2)
    for p in local_files:
        gp = p.with_suffix(".json")
        gp.write_text(wf_json, encoding="utf-8")
        graph_files.append(gp)

    if s3:
        for p in local_files + prompt_files + graph_files:
            rel = p.relative_to(state.dir / "outputs").as_posix()
            try:
                key = await s3.upload(p, rel)
                log.info("[%s] s3 ↑ %s", client.cfg.name, key)
            except Exception as e:
                log.warning("[%s] s3 upload failed for %s: %s",
                            client.cfg.name, p.name, e)

    rel_files = [
        p.relative_to(state.dir / "outputs").as_posix()
        for p in local_files
    ]
    log.info("[%s] done video job %d -> %s", client.cfg.name, idx, out_dir)
    await _video_set_status(state, idx, "done", files=rel_files,
                            pod=client.cfg.name, error=None)


async def _video_set_status(state: RunState, idx: int, status: str, **extra: Any) -> None:
    for j in state.jobs:
        if j["idx"] == idx:
            now = time.time()
            prev_status = j.get("status")
            j["status"] = status
            j["updated_at"] = now
            _stamp_job_timing(j, status, now, prev_status)
            for k, v in extra.items():
                j[k] = v
            await state.emit("job_updated", job=j)
            return


@app.get("/api/video-runs/{run_id}/archive", dependencies=[Depends(auth_dep)])
async def download_video_run_archive(run_id: str) -> FileResponse:
    """ZIP all video files from a video run."""
    state = get_run(run_id)
    if state.run_type != "video":
        raise HTTPException(400, "not a video run")
    outputs = state.dir / "outputs"
    if not outputs.exists():
        raise HTTPException(404)

    video_exts = {".mp4", ".webm", ".gif", ".mov", ".avi", ".mkv"}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{run_id}-video-", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    written = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for p in sorted(outputs.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in video_exts:
                    continue
                zf.write(p, arcname=p.relative_to(outputs).as_posix())
                written += 1
        if written == 0:
            raise HTTPException(404, "no video files found for this run")
    except Exception:
        _cleanup_path(str(tmp_path))
        raise

    return FileResponse(
        tmp_path,
        filename=f"{run_id}-videos.zip",
        media_type="application/zip",
        background=BackgroundTask(_cleanup_path, str(tmp_path)),
    )


# ---- scenarios builder ---------------------------------------------------

@app.post("/api/scenarios/expand", dependencies=[Depends(auth_dep)])
async def expand_scenario_endpoint(request: Request) -> dict[str, Any]:
    """Accepts either:
      - application/json: a dict spec
      - text/yaml | text/plain: raw YAML text
    This keeps the frontend free of a YAML parser dependency."""
    ctype = (request.headers.get("content-type") or "").lower()
    body = await request.body()
    if "yaml" in ctype or "text/plain" in ctype:
        try:
            spec = yaml.safe_load(body.decode("utf-8"))
        except yaml.YAMLError as e:
            raise HTTPException(400, f"YAML parse error: {e}")
    else:
        try:
            spec = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON parse error: {e}")
    if not isinstance(spec, dict):
        raise HTTPException(400, "spec must be a mapping")
    rows = expand_scenario_dict(spec)
    # Build CSV inline
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return {"rows": rows, "csv": buf.getvalue()}


# ---- entrypoint -----------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("WEB_HOST", "0.0.0.0"),
        port=int(os.environ.get("WEB_PORT", "8766")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
