"""Expand a high-level scenario spec into a flat jobs.csv.

Usage:
    python expand_scenario.py scenarios/alice_1_3.yaml > jobs.csv

Scenario YAML schema:

    scenario: "1.3"
    girl: alice
    input_images: [alice_ref.png]
    loras:
      - name: alice_v3.safetensors
        strength_model: 0.9
        strength_clip: 0.9
    ksampler:                       # one fixed set, or a list of sets
      seed: 12345
      steps: 20
      cfg: 7.0
      sampler_name: dpmpp_2m
      scheduler: karras
      denoise: 1.0
    prompts:
      - positive: "portrait, studio light"
        negative: "blurry"
      - positive: "on the beach, sunset"
        negative: "blurry"

If you pass a list under `ksampler:` (instead of a dict), every variant is
crossed with every prompt / lora / image — covers scenarios 1.1, 1.4, 2.x.
"""
from __future__ import annotations

import argparse
import csv
import sys
from itertools import product
from pathlib import Path

import yaml


CSV_COLS = [
    "workflow",
    "scenario", "girl",
    "lora_name", "lora_strength_model", "lora_strength_clip",
    "prompt_positive", "prompt_negative",
    "input_image",
    "seed", "control_after_generate", "steps", "cfg",
    "sampler_name", "scheduler", "denoise",
]


def _as_list(x):
    return x if isinstance(x, list) else [x]


def expand(spec: dict) -> list[dict]:
    workflow = str(spec.get("workflow", "B_PROD"))
    scenario = str(spec.get("scenario", ""))

    # Two shapes supported:
    #   - single subject:  girl: <s>   input_images: [...]
    #   - multi-subject:   subjects: [{girl, input_image}, ...]
    if "subjects" in spec:
        subjects = [{"girl": s["girl"], "input_image": s["input_image"]}
                    for s in spec["subjects"]]
    else:
        subjects = [{"girl": spec["girl"], "input_image": img}
                    for img in _as_list(spec["input_images"])]

    loras = _as_list(spec["loras"])
    prompts = _as_list(spec["prompts"])
    ksampler_variants = _as_list(spec["ksampler"])

    rows: list[dict] = []
    for subj, lora, prompt, ks in product(subjects, loras, prompts, ksampler_variants):
        rows.append({
            "workflow": workflow,
            "scenario": scenario,
            "girl": subj["girl"],
            "lora_name": lora["name"],
            "lora_strength_model": lora.get("strength_model", 1.0),
            "lora_strength_clip": lora.get("strength_clip", 1.0),
            "prompt_positive": prompt["positive"],
            "prompt_negative": prompt.get("negative", ""),
            "input_image": subj["input_image"],
            "seed": ks.get("seed", 0),
            "control_after_generate": ks.get("control_after_generate", "fixed"),
            "steps": ks.get("steps", 20),
            "cfg": ks.get("cfg", 7.0),
            "sampler_name": ks.get("sampler_name", "dpmpp_2m"),
            "scheduler": ks.get("scheduler", "karras"),
            "denoise": ks.get("denoise", 1.0),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="output CSV (default: stdout)")
    args = ap.parse_args()

    spec = yaml.safe_load(args.spec.read_text())
    rows = expand(spec)

    fh = args.out.open("w", newline="") if args.out else sys.stdout
    writer = csv.DictWriter(fh, fieldnames=CSV_COLS)
    writer.writeheader()
    writer.writerows(rows)
    if args.out:
        fh.close()
        print(f"wrote {len(rows)} rows -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
