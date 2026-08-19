"""Loads video_jobs.csv into VideoJob dataclasses.

CSV columns (all optional except workflow):
  workflow              - workflow key (default: video_ltx)
  input_image           - filename under inputs/ (first/only reference frame)
  input_image_last      - 2nd reference frame (FLF2V flows: First+Last frame)
  input_video           - reference video filename under inputs/ (ref2v flows:
                          MiniMax H3); uploaded to the pod like a photo
  prompt_positive       - positive text (node 28)
  prompt_negative       - negative text (node 29)
  seed                  - integer seed (node 125); empty/0/-1/'random' -> random per row
  video_length_seconds  - int seconds (node 18)
  video_width           - int pixels (node 19)
  video_height          - int pixels (node 181)
  sigmas_first_pass     - comma-separated floats (node 225)
  sigmas_final_pass     - comma-separated floats (node 226)
  cfg_first_pass        - float (node 245)
  cfg_final_pass        - float (node 255)
  audio_volume_first    - float (node 249)
  audio_volume_final    - float (node 251)
  steps                 - sampler steps (BasicScheduler / KSampler)
  denoise               - float denoise
  scheduler             - scheduler name (beta, karras, ...)
  sampler_name          - sampler name (euler, euler_ancestral, ...)
  checkpoint_name       - .safetensors filename (node 1)
  diffusion_model_name  - .safetensors filename (node 186)
  lora_name             - .safetensors filename for a plain LoraLoader
  lora_strength         - float strength_model for that LoraLoader
  load_loras_json       - JSON patch for the main Power Lora Loader
  load_distilled_lora_json - JSON patch for the distilled LoRA (first pass)
  load_distilled_lora_final_json - JSON patch for the distilled LoRA (final pass; LTX v2)
  girl                  - label/display name
  scenario              - group tag

Any extra columns are passed as extra{} for direct node.field patching.
"""
from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from pathlib import Path


# An empty cell or a missing `seed` column means "pick a fresh random seed",
# so each video job gets its own noise instead of all sharing seed 0.
_RANDOM_SEED_TOKENS = {"", "0", "-1", "random", "rand"}


def _parse_seed(raw: str) -> int:
    token = (raw or "").strip().lower()
    if token in _RANDOM_SEED_TOKENS:
        return random.randint(0, 2**32 - 1)
    return int(float(token))


def _opt_int(raw: str) -> int | None:
    """int from CSV cell, or None if the cell/column is absent — so missing
    values stay blank in the UI and don't override the template."""
    raw = (raw or "").strip()
    return int(float(raw)) if raw else None


def _opt_float(raw: str) -> float | None:
    """float from CSV cell, or None for an empty/absent cell (keep template)."""
    raw = (raw or "").strip()
    return float(raw) if raw else None


@dataclass
class VideoJob:
    workflow: str
    scenario: str
    girl: str
    input_image: str
    input_image_last: str          # 2nd reference frame for FLF2V flows (optional)
    input_video: str               # reference video for ref2v flows (optional)
    prompt_positive: str
    prompt_negative: str
    seed: int
    video_length_seconds: int | None    # None = column absent → kept from template, blank in UI
    video_width: int | None
    video_height: int | None
    sigmas_first_pass: str
    sigmas_final_pass: str
    cfg_first_pass: float
    cfg_final_pass: float
    audio_volume_first: float
    audio_volume_final: float
    steps: int | None                   # None = keep the template's own value
    denoise: float | None
    scheduler: str
    sampler_name: str
    checkpoint_name: str
    diffusion_model_name: str
    lora_name: str
    lora_strength: float | None
    load_loras_json: str
    load_distilled_lora_json: str
    load_distilled_lora_final_json: str
    extra: dict[str, str] = field(default_factory=dict)


_KNOWN = {
    "workflow", "scenario", "girl",
    "input_image", "input_image_last", "input_video",
    "prompt_positive", "prompt_negative",
    "seed",
    "video_length_seconds", "video_width", "video_height",
    "sigmas_first_pass", "sigmas_final_pass",
    "cfg_first_pass", "cfg_final_pass",
    "audio_volume_first", "audio_volume_final",
    "steps", "denoise", "scheduler", "sampler_name",
    "checkpoint_name", "diffusion_model_name",
    "lora_name", "lora_strength",
    "load_loras_json", "load_distilled_lora_json", "load_distilled_lora_final_json",
}


def _f(row: dict[str, str], key: str, default: str = "") -> str:
    v = row.get(key)
    return v if v is not None else default


def _detect_delimiter(path: Path) -> str:
    with path.open(newline="", encoding="utf-8-sig") as f:
        first = f.readline()
    if first.count(";") > first.count(","):
        return ";"
    return ","


def load_video_jobs(path: Path) -> list[VideoJob]:
    jobs: list[VideoJob] = []
    delim = _detect_delimiter(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delim)
        # Дефолтный ключ — только когда колонки workflow нет вовсе (легаси
        # single-template CSV). Пустая ячейка при существующей колонке —
        # забытый флоу: оставляем "" и роняем на пре-флайте, а не молча
        # подменяем графом video_ltx.
        has_wf_col = "workflow" in (reader.fieldnames or [])
        for row in reader:
            extra = {
                k: (v or "").strip()
                for k, v in row.items()
                if k and k not in _KNOWN and (v or "").strip()
            }
            jobs.append(VideoJob(
                workflow=(_f(row, "workflow").strip() if has_wf_col
                          else "video_ltx"),
                scenario=_f(row, "scenario"),
                girl=_f(row, "girl"),
                input_image=_f(row, "input_image", "").strip(),
                input_image_last=_f(row, "input_image_last", "").strip(),
                input_video=_f(row, "input_video", "").strip(),
                prompt_positive=_f(row, "prompt_positive"),
                prompt_negative=_f(row, "prompt_negative"),
                seed=_parse_seed(_f(row, "seed", "")),
                video_length_seconds=_opt_int(_f(row, "video_length_seconds", "")),
                video_width=_opt_int(_f(row, "video_width", "")),
                video_height=_opt_int(_f(row, "video_height", "")),
                sigmas_first_pass=_f(row, "sigmas_first_pass"),
                sigmas_final_pass=_f(row, "sigmas_final_pass"),
                cfg_first_pass=float(_f(row, "cfg_first_pass", "1.5") or 1.5),
                cfg_final_pass=float(_f(row, "cfg_final_pass", "1.0") or 1.0),
                audio_volume_first=float(_f(row, "audio_volume_first", "5") or 5),
                audio_volume_final=float(_f(row, "audio_volume_final", "5") or 5),
                steps=_opt_int(_f(row, "steps", "")),
                denoise=_opt_float(_f(row, "denoise", "")),
                scheduler=_f(row, "scheduler").strip(),
                sampler_name=_f(row, "sampler_name").strip(),
                checkpoint_name=_f(row, "checkpoint_name"),
                diffusion_model_name=_f(row, "diffusion_model_name"),
                lora_name=_f(row, "lora_name").strip(),
                lora_strength=_opt_float(_f(row, "lora_strength", "")),
                load_loras_json=_f(row, "load_loras_json"),
                load_distilled_lora_json=_f(row, "load_distilled_lora_json"),
                load_distilled_lora_final_json=_f(row, "load_distilled_lora_final_json"),
                extra=extra,
            ))
    return jobs


SAMPLE_CSV_HEADER = (
    "workflow,scenario,girl,input_image,input_video,"
    "prompt_positive,prompt_negative,seed,"
    "video_length_seconds,video_width,video_height,"
    "sigmas_first_pass,sigmas_final_pass,"
    "cfg_first_pass,cfg_final_pass,"
    "audio_volume_first,audio_volume_final,"
    "steps,denoise,scheduler,sampler_name,"
    "checkpoint_name,diffusion_model_name,"
    "lora_name,lora_strength,"
    "load_loras_json,load_distilled_lora_json,load_distilled_lora_final_json"
)

SAMPLE_CSV_ROW = (
    'video_ltx,scene_01,SubjectName,sample.jpg,,'
    '"subject in frame, first-person POV, smooth continuous repetitive loop motion, '
    'subject face sharp and clearly visible, highly coherent motion, stable details",'
    '"blurry, low quality, artifacts, watermark, text, logo, deformed anatomy, bad anatomy, '
    'static, cartoon, 3d render, noise, jpeg artifacts, music, background music",3820205485533,'
    '5,832,1216,'
    '"1., 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0",'
    '"0.85, 0.7250, 0.4219, 0.0",'
    '1.5,1.0,'
    '5.0,5.0,'
    ',,,,'
    'my_checkpoint.safetensors,'
    'my_diffusion_model.safetensors,'
    ',,'
    '"{""lora_16"": {""on"": true, ""lora"": ""my_style_lora.safetensors"", ""strength"": 0.85}}",'
    '"{""lora_2"": {""on"": true, ""lora"": ""my_distilled_lora.safetensors"", ""strength"": 0.6}}",'
    '"{""lora_1"": {""on"": true, ""lora"": ""my_distilled_lora.safetensors"", ""strength"": 0.6}}"'
)

SAMPLE_CSV = SAMPLE_CSV_HEADER + "\n" + SAMPLE_CSV_ROW + "\n"
