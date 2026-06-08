"""Loads video_jobs.csv into VideoJob dataclasses.

CSV columns (all optional except workflow):
  workflow              - workflow key (default: video_ltx)
  input_image           - filename under inputs/ (for node 15)
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
  checkpoint_name       - .safetensors filename (node 1)
  diffusion_model_name  - .safetensors filename (node 186)
  load_loras_json       - JSON patch for node 6 (Power Lora Loader)
  load_distilled_lora_json - JSON patch for node 7 (Power Lora Loader)
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


@dataclass
class VideoJob:
    workflow: str
    scenario: str
    girl: str
    input_image: str
    prompt_positive: str
    prompt_negative: str
    seed: int
    video_length_seconds: int
    video_width: int
    video_height: int
    sigmas_first_pass: str
    sigmas_final_pass: str
    cfg_first_pass: float
    cfg_final_pass: float
    audio_volume_first: float
    audio_volume_final: float
    checkpoint_name: str
    diffusion_model_name: str
    load_loras_json: str
    load_distilled_lora_json: str
    extra: dict[str, str] = field(default_factory=dict)


_KNOWN = {
    "workflow", "scenario", "girl",
    "input_image",
    "prompt_positive", "prompt_negative",
    "seed",
    "video_length_seconds", "video_width", "video_height",
    "sigmas_first_pass", "sigmas_final_pass",
    "cfg_first_pass", "cfg_final_pass",
    "audio_volume_first", "audio_volume_final",
    "checkpoint_name", "diffusion_model_name",
    "load_loras_json", "load_distilled_lora_json",
}


def _f(row: dict[str, str], key: str, default: str = "") -> str:
    v = row.get(key)
    return v if v is not None else default


def _detect_delimiter(path: Path) -> str:
    with path.open(newline="") as f:
        first = f.readline()
    if first.count(";") > first.count(","):
        return ";"
    return ","


def load_video_jobs(path: Path) -> list[VideoJob]:
    jobs: list[VideoJob] = []
    delim = _detect_delimiter(path)
    with path.open(newline="") as f:
        reader = csv.DictReader(f, delimiter=delim)
        for row in reader:
            extra = {
                k: (v or "").strip()
                for k, v in row.items()
                if k and k not in _KNOWN and (v or "").strip()
            }
            jobs.append(VideoJob(
                workflow=_f(row, "workflow", "video_ltx") or "video_ltx",
                scenario=_f(row, "scenario"),
                girl=_f(row, "girl"),
                input_image=_f(row, "input_image", "").strip(),
                prompt_positive=_f(row, "prompt_positive"),
                prompt_negative=_f(row, "prompt_negative"),
                seed=_parse_seed(_f(row, "seed", "")),
                video_length_seconds=int(_f(row, "video_length_seconds", "5") or 5),
                video_width=int(_f(row, "video_width", "832") or 832),
                video_height=int(_f(row, "video_height", "1216") or 1216),
                sigmas_first_pass=_f(row, "sigmas_first_pass"),
                sigmas_final_pass=_f(row, "sigmas_final_pass"),
                cfg_first_pass=float(_f(row, "cfg_first_pass", "1.5") or 1.5),
                cfg_final_pass=float(_f(row, "cfg_final_pass", "1.0") or 1.0),
                audio_volume_first=float(_f(row, "audio_volume_first", "5") or 5),
                audio_volume_final=float(_f(row, "audio_volume_final", "5") or 5),
                checkpoint_name=_f(row, "checkpoint_name"),
                diffusion_model_name=_f(row, "diffusion_model_name"),
                load_loras_json=_f(row, "load_loras_json"),
                load_distilled_lora_json=_f(row, "load_distilled_lora_json"),
                extra=extra,
            ))
    return jobs


SAMPLE_CSV_HEADER = (
    "workflow,scenario,girl,input_image,"
    "prompt_positive,prompt_negative,seed,"
    "video_length_seconds,video_width,video_height,"
    "sigmas_first_pass,sigmas_final_pass,"
    "cfg_first_pass,cfg_final_pass,"
    "audio_volume_first,audio_volume_final,"
    "checkpoint_name,diffusion_model_name,"
    "load_loras_json,load_distilled_lora_json"
)

SAMPLE_CSV_ROW = (
    'video_ltx,scene_01,SubjectName,sample.jpg,'
    '"subject in frame, first-person POV, smooth continuous repetitive loop motion, '
    'subject face sharp and clearly visible, highly coherent motion, stable details",'
    '"blurry, low quality, artifacts, watermark, text, logo, deformed anatomy, bad anatomy, '
    'static, cartoon, 3d render, noise, jpeg artifacts, music, background music",3820205485533,'
    '5,832,1216,'
    '"1., 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0",'
    '"0.85, 0.7250, 0.4219, 0.0",'
    '1.5,1.0,'
    '5.0,5.0,'
    'my_checkpoint.safetensors,'
    'my_diffusion_model.safetensors,'
    '"{""lora_16"": {""on"": true, ""lora"": ""my_style_lora.safetensors"", ""strength"": 0.85}}",'
    '"{""lora_2"": {""on"": true, ""lora"": ""my_distilled_lora.safetensors"", ""strength"": 0.6}}"'
)

SAMPLE_CSV = SAMPLE_CSV_HEADER + "\n" + SAMPLE_CSV_ROW + "\n"
