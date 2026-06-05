"""Patch a ComfyUI video workflow (LTX-style) by directly targeting node IDs.

Instead of the generic mapping.yaml approach used for image workflows,
video workflows expose a fixed set of well-known node IDs that can be
set directly from the CSV row. This avoids the need to register/configure
the workflow separately.

Node IDs for video_LTX 2.3_1_API_3.json:
  15  - Load Image          -> inputs.image (str)
  6   - Load Lora's         -> inputs.lora_N.{on, lora, strength}
  7   - Load Distilled Lora -> inputs.lora_N.{on, lora, strength}
  18  - Video Length (sec)  -> inputs.Xi / inputs.Xf (int)
  19  - Video Width         -> inputs.Xi / inputs.Xf (int)
  181 - Video Height        -> inputs.Xi / inputs.Xf (int)
  125 - Seed (rgthree)      -> inputs.seed (int)
  225 - Sigmas First Pass   -> inputs.sigmas (str)
  226 - Sigmas Final Pass   -> inputs.sigmas (str)
  28  - Positive            -> inputs.text (str)
  29  - Negative            -> inputs.text (str)
  245 - CFGGuider First Pass-> inputs.cfg (float)
  255 - CFGGuider Final Pass-> inputs.cfg (float)
  249 - Audio Adj. Vol. 1st -> inputs.volume (float)
  251 - Audio Adj. Vol. Fin -> inputs.volume (float)
  1   - Load Checkpoint     -> inputs.ckpt_name (str)
  186 - Load Diffusion Model-> inputs.unet_name (str)
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VideoJobParams:
    """All settable parameters for a single LTX video job."""
    # Image input
    input_image: str = ""                   # node 15: image filename

    # Prompts
    prompt_positive: str = ""               # node 28: text
    prompt_negative: str = ""               # node 29: text

    # Seed
    seed: int = 0                           # node 125: seed

    # Video dimensions
    video_length_seconds: int = 5           # node 18: Xi/Xf
    video_width: int = 832                  # node 19: Xi/Xf
    video_height: int = 1216               # node 181: Xi/Xf

    # Sigmas
    sigmas_first_pass: str = ""             # node 225: sigmas
    sigmas_final_pass: str = ""             # node 226: sigmas

    # CFG
    cfg_first_pass: float = 1.5            # node 245: cfg
    cfg_final_pass: float = 1.0            # node 255: cfg

    # Audio volume
    audio_volume_first_pass: float = 5.0   # node 249: volume
    audio_volume_final_pass: float = 5.0   # node 251: volume

    # Models
    checkpoint_name: str = ""              # node 1: ckpt_name
    diffusion_model_name: str = ""         # node 186: unet_name

    # Lora loaders (nodes 6 and 7): raw JSON string or dict with lora_N keys
    # These are stored as JSON strings in CSV for flexibility.
    load_loras_json: str = ""              # node 6 full inputs patch (JSON)
    load_distilled_lora_json: str = ""     # node 7 full inputs patch (JSON)

    # Save prefix for output files
    save_prefix: str = "video"

    # Extra: arbitrary node_id -> field -> value patches (advanced)
    extra: dict[str, str] = field(default_factory=dict)


def build_video_workflow(
    template: dict[str, Any],
    params: VideoJobParams,
) -> dict[str, Any]:
    """Patch the video workflow template with the given per-job params.

    Returns a deep copy of *template* with all relevant nodes patched.
    Unknown/empty params are silently skipped so the template default stands.
    """
    wf = copy.deepcopy(template)

    def node(nid: str | int) -> dict[str, Any]:
        key = str(nid)
        if key not in wf:
            raise KeyError(f"Node {key!r} not found in video workflow template")
        return wf[key].setdefault("inputs", {})

    # ---- Load Image (15) ----
    if params.input_image:
        node("15")["image"] = params.input_image

    # ---- Prompts (28, 29) ----
    if params.prompt_positive:
        node("28")["text"] = params.prompt_positive
    if params.prompt_negative:
        node("29")["text"] = params.prompt_negative

    # ---- Seed (125) ----
    node("125")["seed"] = int(params.seed)

    # ---- Video dimensions (mxSlider nodes) ----
    if params.video_length_seconds:
        v = int(params.video_length_seconds)
        node("18")["Xi"] = v
        node("18")["Xf"] = v
    if params.video_width:
        v = int(params.video_width)
        node("19")["Xi"] = v
        node("19")["Xf"] = v
    if params.video_height:
        v = int(params.video_height)
        node("181")["Xi"] = v
        node("181")["Xf"] = v

    # ---- Sigmas ----
    if params.sigmas_first_pass:
        node("225")["sigmas"] = params.sigmas_first_pass
    if params.sigmas_final_pass:
        node("226")["sigmas"] = params.sigmas_final_pass

    # ---- CFG ----
    if params.cfg_first_pass is not None:
        node("245")["cfg"] = float(params.cfg_first_pass)
    if params.cfg_final_pass is not None:
        node("255")["cfg"] = float(params.cfg_final_pass)

    # ---- Audio volume ----
    if params.audio_volume_first_pass is not None:
        node("249")["volume"] = float(params.audio_volume_first_pass)
    if params.audio_volume_final_pass is not None:
        node("251")["volume"] = float(params.audio_volume_final_pass)

    # ---- Models ----
    if params.checkpoint_name:
        node("1")["ckpt_name"] = params.checkpoint_name
    if params.diffusion_model_name:
        node("186")["unet_name"] = params.diffusion_model_name

    # ---- Lora loaders (Power Lora Loader rgthree format) ----
    # The CSV may supply a JSON patch for lora_1..lora_N entries.
    # Format: {"lora_1": {"on": true, "lora": "name.safetensors", "strength": 0.8}, ...}
    for node_id, raw_json in (
        ("6", params.load_loras_json),
        ("7", params.load_distilled_lora_json),
    ):
        if not raw_json or not raw_json.strip():
            continue
        try:
            patch = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(patch, dict):
            node_inputs = node(node_id)
            for k, v in patch.items():
                node_inputs[k] = v

    # ---- Save prefix for VHS_VideoCombine (node 61) ----
    if "61" in wf:
        wf["61"].setdefault("inputs", {})["filename_prefix"] = params.save_prefix

    # ---- Extra arbitrary patches: "node_id.field" -> value ----
    for key, raw_value in (params.extra or {}).items():
        if "." not in key:
            continue
        nid, _, fld = key.partition(".")
        try:
            node(nid)[fld] = _coerce_extra(raw_value)
        except KeyError:
            pass  # ignore unknown nodes

    return wf


def _coerce_extra(v: str) -> Any:
    """Try to parse as JSON (catches int/float/bool/list/dict), else return str."""
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return v


def load_video_template(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())
