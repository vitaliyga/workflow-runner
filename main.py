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
    VIDEO_FIELD_CATALOG,
)


# ---------------------------------------------------------------------------
# bootstrap

ROOT = Path(__file__).parent
STORAGE = ROOT / "storage"
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
    }
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
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for j in self.jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        return {
            "run_id": self.run_id,
            "run_type": self.run_type,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancelled": self.cancelled,
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
            try:
                if self.dry_run:
                    await self._handle_dry(name, item)
                else:
                    await self._handle(client, item)
            except Exception as e:
                item.attempts += 1
                if item.attempts < self.max_attempts:
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
                j["status"] = status
                j["updated_at"] = time.time()
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
    ks = first_of("KSampler", "KSamplerAdvanced", "SamplerCustomAdvanced")
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
    return {
        "name": safe,
        "key": Path(safe).stem,
        "is_video": video,
        "video_fields": _video_field_candidates(wf) if video else [],
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
    mapping = spec.get("video_fields") or default_video_mapping(template)
    csv_text = sample_csv_for(template, mapping, key)
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{key}_sample.csv"'},
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
        img
        for j in state.jobs
        for img in (j.get("input_images") or [j.get("input_image")])
        if img
    }
    return sorted(n for n in needed if not (inputs / n).exists())


@app.post("/api/runs/{run_id}/start", dependencies=[Depends(auth_dep)])
async def start_run(run_id: str) -> dict[str, Any]:
    state = get_run(run_id)
    if state.started_at and not state.finished_at:
        raise HTTPException(409, "already running")

    missing = _missing_inputs(state)
    if missing:
        raise HTTPException(400, f"missing input photos: {missing}")

    cfg = load_config()
    pod_cfgs = [
        PodConfig(name=p["name"], url=p["url"],
                  max_parallel=p.get("max_parallel", 1),
                  api_key=p.get("api_key"))
        for p in cfg.get("pods") or []
    ]
    if not pod_cfgs:
        raise HTTPException(400, "no pods configured (see /settings)")

    # Write workflows.yaml on the fly to a temp file inside the run dir, so
    # WorkflowRegistry can read it as a single source of truth. We resolve
    # every `template:` to an absolute path against the project root,
    # because the YAML now lives in the run dir (not next to jobs/).
    wf_block = json.loads(json.dumps(cfg["workflows"]))   # deep copy
    for spec in (wf_block.get("workflows") or {}).values():
        t = spec.get("template")
        if t and not Path(t).is_absolute():
            spec["template"] = str((ROOT / t).resolve())
    wf_path = state.dir / "workflows.yaml"
    wf_path.write_text(yaml.safe_dump(wf_block, sort_keys=False,
                                       allow_unicode=True))
    registry = WorkflowRegistry(wf_path)
    # Validate all referenced workflows — translate missing-key into a
    # readable 400 instead of a 500 in the background task.
    for j in state.jobs:
        try:
            registry.get(j["workflow"])
        except KeyError as e:
            raise HTTPException(400, str(e))
        except FileNotFoundError as e:
            raise HTTPException(400, f"workflow template missing: {e}")

    s3 = _s3_from_config(cfg.get("s3") or {})

    state.started_at = time.time()
    state.finished_at = None
    state.cancelled = False
    await state.emit("run_started")

    pool = WebPool(
        state=state,
        pods=pod_cfgs,
        workflows=registry,
        inputs_dir=state.dir / "inputs",
        outputs_dir=state.dir / "outputs",
        day_tag=_run_day_tag(run_id),
        run_tag=_run_time_tag(run_id),
        s3=s3,
    )

    csv_jobs = load_jobs(state.dir / "jobs.csv")
    asyncio.create_task(_run_pool(state, pool, csv_jobs))
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
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(auth_dep)])
async def cancel_run(run_id: str) -> dict[str, Any]:
    state = get_run(run_id)
    state.cancelled = True
    await state.emit("run_cancelled")
    # The PodPool doesn't currently look at this flag — we leave proper
    # cancellation to a follow-up commit. For now mark pending → cancelled.
    for j in state.jobs:
        if j["status"] == "pending":
            j["status"] = "failed"
            j["error"] = "cancelled"
    await state.persist()
    return {"ok": True}


# ---- video runs (LTX-specific, direct node patching) -------------------

# Legacy fallback template, used only when a CSV row's `workflow` key is not
# a registered video flow (keeps older single-template setups working).
VIDEO_WORKFLOW_TEMPLATE_PATH = ROOT / "video_workflow.json"


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


@app.post("/api/video-runs", dependencies=[Depends(auth_dep)])
async def create_video_run(
    csv_file: UploadFile = File(...),
    photos: list[UploadFile] = File(default=[]),
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
    jobs = load_video_jobs(rd / "jobs.csv")
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
            "input_images": [j.input_image] if j.input_image else [],
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
    }


@app.post("/api/video-runs/{run_id}/start", dependencies=[Depends(auth_dep)])
async def start_video_run(run_id: str) -> dict[str, Any]:
    """Start processing a video run with direct node patching."""
    state = get_run(run_id)
    if state.run_type != "video":
        raise HTTPException(400, "not a video run")
    if state.started_at and not state.finished_at:
        raise HTTPException(409, "already running")

    missing = _missing_inputs(state)
    if missing:
        raise HTTPException(400, f"missing input photos: {missing}")

    cfg = load_config()
    pod_cfgs = [
        PodConfig(name=p["name"], url=p["url"],
                  max_parallel=p.get("max_parallel", 1),
                  api_key=p.get("api_key"))
        for p in cfg.get("pods") or []
    ]
    if not pod_cfgs:
        raise HTTPException(400, "no pods configured (see /settings)")

    video_jobs = load_video_jobs(state.dir / "jobs.csv")
    # Resolve template + mapping per workflow key (raises if any unregistered).
    flows = _resolve_video_flows(cfg, video_jobs)

    s3 = _s3_from_config(cfg.get("s3") or {})

    state.started_at = time.time()
    state.finished_at = None
    state.cancelled = False
    await state.emit("run_started")

    asyncio.create_task(_run_video_pool(state, pod_cfgs, video_jobs, flows, s3))
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
        attempts = 0
        while attempts < max_attempts:
            try:
                await _video_handle(state, client, idx, job, flows, s3)
                break
            except Exception as e:
                attempts += 1
                log.warning("[%s] video job %d failed (attempt %d): %s",
                            client.cfg.name, idx, attempts, e)
                if attempts >= max_attempts:
                    await _video_set_status(state, idx, "failed",
                                            error=f"{type(e).__name__}: {e}")
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

    # Upload input image
    remote_image = ""
    if job.input_image:
        local_img = state.dir / "inputs" / job.input_image
        if not local_img.exists():
            raise FileNotFoundError(f"input image missing: {local_img}")
        remote_image = await client.upload_image(local_img)

    day_tag = _run_day_tag(state.run_id)
    # Layout: <day>/<HHMMSS_workflow>/<girl>. HHMMSS groups each run separately.
    wf_tag = f"{_run_time_tag(state.run_id)}_{_safe_tag(job.workflow, 'video')}"
    # folder/filename carry the CSV 'girl' value so outputs are grouped per subject
    girl_tag = _safe_tag(job.girl, f"{idx:05d}")
    prefix = f"video/{day_tag}/{wf_tag}/{girl_tag}/{girl_tag}_{idx:05d}_seed{job.seed}"

    # CSV row -> field-key values. Only fields present in the flow's mapping
    # are patched; everything else keeps the template default.
    values = {
        "input_image": remote_image,
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
        "checkpoint_name": job.checkpoint_name,
        "diffusion_model_name": job.diffusion_model_name,
        "load_loras_json": job.load_loras_json,
        "load_distilled_lora_json": job.load_distilled_lora_json,
    }
    wf = build_video_workflow(template, values, mapping,
                              save_prefix=prefix, extra=job.extra)

    log.info("[%s] submit video job %d girl=%s seed=%d",
             client.cfg.name, idx, job.girl, job.seed)
    prompt_id = await client.submit(wf)
    entry = await client.wait(prompt_id)

    # Download all outputs (video files). Top-level folder = run date (DD-MM),
    # then the previous per-job layout. This propagates everywhere rel paths
    # are used: local outputs, S3 key, status files list, thumbnails, archive.
    out_dir = state.dir / "outputs" / day_tag / wf_tag / girl_tag
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save node = VHS_VideoCombine (detect its id from the template).
    save_nodes = {
        nid for nid, n in template.items()
        if isinstance(n, dict) and n.get("class_type") == "VHS_VideoCombine"
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

    if s3:
        for p in local_files:
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
            j["status"] = status
            j["updated_at"] = time.time()
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
