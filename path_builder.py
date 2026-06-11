"""Builds the output hierarchy for workflow runs."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from csv_loader import Job


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(s: str, maxlen: int = 60) -> str:
    s = _SAFE.sub("_", s).strip("_")
    return s[:maxlen] or "x"


def lora_dir_name(lora_name: str) -> str:
    # strip .safetensors/.ckpt for a cleaner folder name
    stem = Path(lora_name).stem
    return slug(stem)


def params_dir_name(job: Job) -> str:
    """Short, deterministic name encoding only the parameters that should
    split folders for variant A.

    Prompts and input image names are intentionally excluded so they do not
    create extra subfolders.
    """
    base = (
        f"seedmode_{slug(job.control_after_generate)}"
        f"s{job.steps}"
        f"_cfg{job.cfg:g}"
        f"_{slug(job.sampler_name)}"
        f"_{slug(job.scheduler)}"
    )
    # Add a short hash of only the KSampler-related controls so folders are
    # stable across prompt changes but still split when the generation
    # settings differ.
    h = hashlib.sha1(
        f"{job.control_after_generate}|{job.steps}|{job.cfg}|"
        f"{job.sampler_name}|{job.scheduler}".encode()
    ).hexdigest()[:8]
    return f"{base}_{h}"


def day_dir_name(day_tag: str | None = None) -> str:
    return day_tag or time.strftime("%d-%m")


def workflow_dir_name(job: Job, run_tag: str | None = None) -> str:
    """Second-level folder: '<HHMMSS>_<workflow>' so every run gets its own
    subtree under the day. Falls back to just the workflow slug when no
    run_tag is supplied."""
    wf = slug(job.workflow)
    return f"{run_tag}_{wf}" if run_tag else wf


def output_dir(root: Path, job: Job, day_tag: str | None = None,
               run_tag: str | None = None) -> Path:
    # Layout: <day>/<HHMMSS_workflow>/<girl>/<params>
    return Path(
        root, day_dir_name(day_tag), workflow_dir_name(job, run_tag),
        slug(job.girl), params_dir_name(job),
    )


def save_prefix(job: Job, idx: int, day_tag: str | None = None,
                run_tag: str | None = None) -> str:
    """filename_prefix for ComfyUI SaveImage. Keeps results within a
    per-job subfolder on the pod so concurrent jobs don't collide.
    Mirrors output_dir: <day>/<HHMMSS_workflow>/<girl>/<params>/<idx>."""
    parts = [
        day_dir_name(day_tag), workflow_dir_name(job, run_tag),
        slug(job.girl), params_dir_name(job),
    ]
    return f"lora_runner/{'/'.join(parts)}/{idx:05d}"
