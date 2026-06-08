"""Loads jobs.csv into Job dataclasses."""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from pathlib import Path
import re


# Sentinels that mean "pick a fresh random seed for this row". An empty cell or
# a missing `seed` column lands here too, so by default every job gets its own
# noise instead of all sharing seed 0.
_RANDOM_SEED_TOKENS = {"", "0", "-1", "random", "rand"}


def _parse_seed(raw: str) -> int:
    token = (raw or "").strip().lower()
    if token in _RANDOM_SEED_TOKENS:
        return random.randint(0, 2**32 - 1)
    return int(float(token))


@dataclass
class Job:
    workflow: str
    scenario: str
    girl: str
    lora_name: str
    lora_strength_model: float
    lora_strength_clip: float
    prompt_positive: str
    prompt_negative: str
    input_image: str            # path under inputs/ (relative)
    input_images: tuple[str, ...]
    seed: int
    control_after_generate: str
    steps: int
    cfg: float
    sampler_name: str
    scheduler: str
    denoise: float
    extra: dict[str, str] = field(default_factory=dict)


def _f(row: dict[str, str], key: str, default: str = "") -> str:
    v = row.get(key)
    return v if v is not None else default


def _detect_delimiter(path: Path) -> str:
    # Excel / Google Sheets in many locales export with ";". Sniff the header.
    with path.open(newline="", encoding="utf-8-sig") as f:
        first = f.readline()
    if first.count(";") > first.count(","):
        return ";"
    return ","


def _split_images(raw: str) -> tuple[str, ...]:
    parts = [p.strip() for p in re.split(r"[|\n;]+", raw or "") if p.strip()]
    return tuple(dict.fromkeys(parts))


def _row_images(row: dict[str, str]) -> tuple[str, ...]:
    raw_multi = _f(row, "input_images", "").strip()
    if raw_multi:
        return _split_images(raw_multi)

    numbered: list[tuple[int, str]] = []
    for key, value in row.items():
        if not key or not key.startswith("input_image_"):
            continue
        suffix = key[len("input_image_"):]
        if not suffix.isdigit():
            continue
        value = (value or "").strip()
        if value:
            numbered.append((int(suffix), value))
    if numbered:
        numbered.sort(key=lambda item: item[0])
        return tuple(v for _, v in numbered)

    single = _f(row, "input_image", "").strip()
    return (single,) if single else ()


def load_jobs(path: Path) -> list[Job]:
    jobs: list[Job] = []
    delim = _detect_delimiter(path)
    known = {
        "workflow", "scenario", "girl",
        "lora_name", "lora_strength_model", "lora_strength_clip",
        "prompt_positive", "prompt_negative",
        "input_image", "input_images", "seed",
        "control_after_generate", "steps", "cfg",
        "sampler_name", "scheduler", "denoise",
    }
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delim)
        for row in reader:
            images = _row_images(row)
            first_image = images[0] if images else ""
            extra = {
                k: (v or "").strip()
                for k, v in row.items()
                if k and k not in known and (v or "").strip()
            }
            jobs.append(Job(
                workflow=_f(row, "workflow", "sample_image_v1") or "sample_image_v1",
                scenario=_f(row, "scenario"),
                girl=_f(row, "girl"),
                lora_name=_f(row, "lora_name"),
                lora_strength_model=float(_f(row, "lora_strength_model", "1") or 1),
                lora_strength_clip=float(_f(row, "lora_strength_clip", "1") or 1),
                prompt_positive=_f(row, "prompt_positive"),
                prompt_negative=_f(row, "prompt_negative"),
                input_image=first_image,
                input_images=images,
                seed=_parse_seed(_f(row, "seed", "")),
                control_after_generate=_f(row, "control_after_generate", "fixed"),
                steps=int(_f(row, "steps", "20") or 20),
                cfg=float(_f(row, "cfg", "7") or 7),
                sampler_name=_f(row, "sampler_name", "dpmpp_2m"),
                scheduler=_f(row, "scheduler", "karras"),
                denoise=float(_f(row, "denoise", "1.0") or 1.0),
                extra=extra,
            ))
    return jobs
