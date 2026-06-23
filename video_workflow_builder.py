"""Patch a ComfyUI video workflow by a *mapping*, the same way image
workflows are registered — no hardcoded node IDs.

A video flow is uploaded and registered like any other workflow. On register
we auto-detect which node drives each well-known field (by the node's
``_meta.title`` first, then ``class_type``). The user can keep, trim or fix
that mapping. At run time the CSV row supplies values only for the fields
present in the mapping; everything else keeps the template's own defaults.

The set of fields the runner understands lives in ``VIDEO_FIELD_CATALOG``.
Each field knows how to write itself into a node (scalar / dual-slider / lora
JSON patch), so the builder stays generic.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# Field catalog: the vocabulary of CSV columns the video runner understands.
#   key        : CSV column name == VideoJob attribute
#   field      : node input field to write
#   kind       : 'scalar' | 'dual' (writes Xi & Xf) | 'lora' (JSON patch)
#   cast       : how to coerce the CSV string
#   title_kw   : keyword groups for _meta.title detection. A node matches if
#                ALL words in ANY group are present (case-insensitive).
#   classes    : class_type fallbacks for detection
#   default    : last-resort node id (legacy LTX 2.3 template), used only if
#                that node actually exists in the template
# --------------------------------------------------------------------------
VIDEO_FIELD_CATALOG: list[dict[str, Any]] = [
    {"key": "input_image", "label": "Входное фото (1-й кадр)", "field": "image",
     "kind": "scalar", "cast": "str",
     "title_kw": [["first", "frame"], ["load", "image"], ["input", "image"]],
     "classes": ["LoadImage"], "default": "15",
     "exclude_kw": ["last", "distilled"]},

    # 2nd reference frame for FLF2V (First-Last-Frame-to-Video) flows. Matched
    # only by a "last frame/image" title — single-frame flows never pick it up.
    {"key": "input_image_last", "label": "Входное фото (последний кадр)", "field": "image",
     "kind": "scalar", "cast": "str",
     "title_kw": [["last", "frame"], ["last", "image"]], "classes": []},

    {"key": "prompt_positive", "label": "Позитивный промт", "field": "text",
     "kind": "scalar", "cast": "str",
     "title_kw": [["positive"]], "classes": [], "default": "28"},

    {"key": "prompt_negative", "label": "Негативный промт", "field": "text",
     "kind": "scalar", "cast": "str",
     "title_kw": [["negative"]], "classes": [], "default": "29"},

    # seed: classic "Seed" nodes use field `seed`; RandomNoise uses `noise_seed`.
    {"key": "seed", "label": "Seed", "field": "seed",
     "kind": "scalar", "cast": "int",
     "title_kw": [["seed"]], "classes": ["Seed (rgthree)", "Seed", "RandomNoise"],
     "class_fields": {"RandomNoise": "noise_seed"}, "default": "125"},

    {"key": "video_length_seconds", "label": "Длина (сек)", "field": "Xi",
     "kind": "dual", "cast": "int",
     "title_kw": [["length"], ["duration"], ["sec"]],
     "classes": [], "default": "18"},

    {"key": "video_width", "label": "Ширина", "field": "Xi",
     "kind": "dual", "cast": "int",
     "title_kw": [["width"]], "classes": [], "default": "19"},

    {"key": "video_height", "label": "Высота", "field": "Xi",
     "kind": "dual", "cast": "int",
     "title_kw": [["height"]], "classes": [], "default": "181"},

    {"key": "sigmas_first_pass", "label": "Sigmas 1-й проход", "field": "sigmas",
     "kind": "scalar", "cast": "str",
     "title_kw": [["sigmas", "first"], ["sigma", "first"]],
     "classes": [], "default": "225"},

    {"key": "sigmas_final_pass", "label": "Sigmas финальный", "field": "sigmas",
     "kind": "scalar", "cast": "str",
     "title_kw": [["sigmas", "final"], ["sigma", "final"], ["sigmas", "last"]],
     "classes": [], "default": "226"},

    {"key": "cfg_first_pass", "label": "CFG 1-й проход", "field": "cfg",
     "kind": "scalar", "cast": "float",
     "title_kw": [["cfg", "first"], ["guider", "first"]],
     "classes": [], "default": "245"},

    {"key": "cfg_final_pass", "label": "CFG финальный", "field": "cfg",
     "kind": "scalar", "cast": "float",
     "title_kw": [["cfg", "final"], ["guider", "final"], ["cfg", "last"]],
     "classes": [], "default": "255"},

    {"key": "audio_volume_first", "label": "Громкость 1-й", "field": "volume",
     "kind": "scalar", "cast": "float",
     "title_kw": [["vol", "first"], ["volume", "first"], ["vol", "1"]],
     "classes": [], "default": "249"},

    {"key": "audio_volume_final", "label": "Громкость финал", "field": "volume",
     "kind": "scalar", "cast": "float",
     "title_kw": [["vol", "final"], ["volume", "final"], ["vol", "fin"]],
     "classes": [], "default": "251"},

    {"key": "checkpoint_name", "label": "Checkpoint", "field": "ckpt_name",
     "kind": "scalar", "cast": "str",
     "title_kw": [["checkpoint"], ["load", "ckpt"]],
     "classes": ["CheckpointLoaderSimple", "CheckpointLoader"], "default": "1"},

    {"key": "diffusion_model_name", "label": "Diffusion model", "field": "unet_name",
     "kind": "scalar", "cast": "str",
     "title_kw": [["diffusion", "model"], ["unet"]],
     "classes": ["UNETLoader", "UnetLoaderGGUF"], "default": "186"},

    # JSON-patch only fits Power Lora Loader (rgthree). Match by class or a
    # "power"+"lora" title — NOT a bare "lora" (that grabs a plain
    # LoraLoaderModelOnly titled "Load LoRA", which the JSON format breaks).
    {"key": "load_loras_json", "label": "Lora (основная)", "field": "",
     "kind": "lora", "cast": "json",
     "title_kw": [["power", "lora"]],
     "classes": ["Power Lora Loader (rgthree)"], "default": "6",
     "exclude_kw": ["distilled"]},

    {"key": "load_distilled_lora_json", "label": "Lora (distilled, 1-й проход)", "field": "",
     "kind": "lora", "cast": "json",
     "title_kw": [["distilled"]],
     "classes": [], "default": "7",
     "exclude_kw": ["final", "last"]},

    # Some flows (LTX 2.3 v2) carry a SECOND distilled loader for the final
    # pass. Patching only the first pass leaves the final pass on the template
    # default and produces blurry output, so it gets its own column.
    # Detected ONLY by title (distilled + final/last) — no legacy default node,
    # because node id 260 means different things across flows (e.g. an audio
    # VAE node in LTX v1). So v1, which has a single distilled loader, never
    # picks up a bogus final-pass mapping.
    {"key": "load_distilled_lora_final_json", "label": "Lora (distilled, финальный проход)",
     "field": "", "kind": "lora", "cast": "json",
     "title_kw": [["distilled", "final"], ["distilled", "last"]],
     "classes": []},
]

_CATALOG_BY_KEY = {f["key"]: f for f in VIDEO_FIELD_CATALOG}

# Class types that mark a workflow as "video" (used to decide which detector
# / which CSV template to offer in the UI).
VIDEO_MARKER_CLASSES = {
    "VHS_VideoCombine", "SaveVideo", "CreateVideo",
    "LTXVPreprocess", "LTXVScheduler", "LTXVAddGuide",
    "LTXVAudioVAELoader", "LTXAVTextEncoderLoader",
    "EmptyLTXVLatentVideo", "LTXVConditioning",
    "mxSlider", "mxSlider2D",
}

# Node classes that write the final video file (filename_prefix lives on them).
VIDEO_SAVE_CLASSES = {"VHS_VideoCombine", "SaveVideo"}


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def _title(node: dict[str, Any]) -> str:
    meta = node.get("_meta") if isinstance(node, dict) else None
    if isinstance(meta, dict):
        return str(meta.get("title") or "").lower()
    return ""


def _title_matches(title: str, kw_groups: list[list[str]]) -> bool:
    for group in kw_groups:
        if all(w in title for w in group):
            return True
    return False


def is_video_workflow(template: dict[str, Any]) -> bool:
    for node in template.values():
        if isinstance(node, dict) and node.get("class_type") in VIDEO_MARKER_CLASSES:
            return True
    return False


def detect_video_mapping(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Best-guess {field_key: {node, field}} for a video template.

    Strategy per field: match by node title keywords first (most reliable),
    then by class_type, then fall back to the legacy default node id only if
    that node actually exists. Each node is claimed by at most one field.
    """
    mapping: dict[str, dict[str, Any]] = {}
    claimed: set[str] = set()

    def field_for(spec: dict[str, Any], nid: str) -> str:
        """Resolve the input field for a node — some specs map the same concept
        to a different field per node class (e.g. seed -> RandomNoise.noise_seed)."""
        cf = spec.get("class_fields")
        if cf:
            node = template.get(str(nid))
            ct = node.get("class_type") if isinstance(node, dict) else None
            if ct in cf:
                return cf[ct]
        return spec["field"]

    def claim(key: str, nid: str, spec: dict[str, Any]) -> None:
        mapping[key] = {"node": str(nid), "field": field_for(spec, nid)}
        claimed.add(str(nid))

    # Pass 1 — titles (skip nodes carrying an exclude keyword)
    for spec in VIDEO_FIELD_CATALOG:
        if spec["key"] in mapping:
            continue
        excl = spec.get("exclude_kw") or []
        for nid, node in template.items():
            if not isinstance(node, dict) or str(nid) in claimed:
                continue
            title = _title(node)
            if not title:
                continue
            if any(w in title for w in excl):
                continue
            if _title_matches(title, spec.get("title_kw") or []):
                claim(spec["key"], nid, spec)
                break

    # Pass 2 — class_type
    for spec in VIDEO_FIELD_CATALOG:
        if spec["key"] in mapping or not spec.get("classes"):
            continue
        for nid, node in template.items():
            if not isinstance(node, dict) or str(nid) in claimed:
                continue
            if node.get("class_type") in spec["classes"]:
                claim(spec["key"], nid, spec)
                break

    # Pass 3 — legacy default node id, only if present and free
    for spec in VIDEO_FIELD_CATALOG:
        if spec["key"] in mapping:
            continue
        dflt = spec.get("default")
        if dflt and str(dflt) in template and str(dflt) not in claimed:
            claim(spec["key"], dflt, spec)

    return mapping


def default_video_mapping(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Backward-compatible mapping (used when a flow has no stored mapping)."""
    return detect_video_mapping(template)


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------
def _coerce(value: Any, cast: str) -> Any:
    if cast == "int":
        return int(float(value))
    if cast == "float":
        return float(value)
    if cast == "json":
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)
    return value


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def build_video_workflow(
    template: dict[str, Any],
    values: dict[str, Any],
    mapping: dict[str, dict[str, Any]],
    save_prefix: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Patch *template* using *mapping* and per-job *values*.

    Only fields present in *mapping* AND with a non-empty value are written;
    everything else keeps the template default. Unknown nodes are skipped.
    """
    wf = copy.deepcopy(template)

    def inputs(nid: str) -> dict[str, Any] | None:
        node = wf.get(str(nid))
        if not isinstance(node, dict):
            return None
        return node.setdefault("inputs", {})

    for key, ref in (mapping or {}).items():
        spec = _CATALOG_BY_KEY.get(key)
        if not spec or key not in values:
            continue
        raw = values.get(key)
        if _is_empty(raw):
            continue
        nid = str(ref.get("node") or "")
        field = ref.get("field") or spec["field"]
        node_inputs = inputs(nid)
        if node_inputs is None:
            continue
        try:
            val = _coerce(raw, spec["cast"])
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

        kind = spec["kind"]
        if kind == "dual":
            node_inputs["Xi"] = val
            node_inputs["Xf"] = val
        elif kind == "lora":
            if isinstance(val, dict):
                for k, v in val.items():
                    node_inputs[k] = v
        else:
            node_inputs[field] = val

    # Save prefix: auto-target the save node(s) — VHS_VideoCombine or SaveVideo.
    if save_prefix:
        for node in wf.values():
            if isinstance(node, dict) and node.get("class_type") in VIDEO_SAVE_CLASSES:
                node.setdefault("inputs", {})["filename_prefix"] = save_prefix

    # Advanced arbitrary patches "node_id.field" -> value
    for ekey, raw_value in (extra or {}).items():
        if "." not in ekey:
            continue
        nid, _, fld = ekey.partition(".")
        node_inputs = inputs(nid)
        if node_inputs is not None:
            try:
                node_inputs[fld] = json.loads(raw_value)
            except (ValueError, TypeError):
                node_inputs[fld] = raw_value

    return wf


# --------------------------------------------------------------------------
# Sample CSV generation (per registered flow)
# --------------------------------------------------------------------------
def _current_value(template: dict[str, Any], ref: dict[str, Any], spec: dict[str, Any]) -> str:
    node = template.get(str(ref.get("node")))
    ins = node.get("inputs", {}) if isinstance(node, dict) else {}
    if spec["kind"] == "lora":
        patch = {k: v for k, v in ins.items() if str(k).startswith("lora_")}
        return json.dumps(patch) if patch else ""
    field = ref.get("field") or spec["field"]
    val = ins.get(field, "")
    return "" if val is None else str(val)


def sample_csv_for(
    template: dict[str, Any],
    mapping: dict[str, dict[str, Any]],
    workflow_key: str,
) -> str:
    """Build a one-row sample CSV containing exactly the mapped columns,
    pre-filled with the template's current values. Always begins with the
    ``workflow`` column so the runner can resolve the template."""
    # Keep catalog order for stable, readable columns.
    cols = [f["key"] for f in VIDEO_FIELD_CATALOG if f["key"] in mapping]
    header = ["workflow", "scenario", "girl"] + cols
    row = {"workflow": workflow_key, "scenario": "scene_01", "girl": "ModelName"}
    for key in cols:
        row[key] = _current_value(template, mapping[key], _CATALOG_BY_KEY[key])

    import csv as _csv
    import io
    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(header)
    writer.writerow([row.get(c, "") for c in header])
    return buf.getvalue()


# --------------------------------------------------------------------------
# Universal mode: every editable node input becomes a CSV column, named by the
# node's title (+ field if the node has several). No catalog needed — works
# for any workflow. Connections ([node, idx]) and widget objects are skipped.
# --------------------------------------------------------------------------
_LOADIMAGE_CLASSES = {"LoadImage"}
# Utility/housekeeping nodes — no per-job-meaningful inputs.
_SKIP_CLASSES = VIDEO_SAVE_CLASSES | {
    "RAMCleanup", "VRAMCleanup", "VRAM_Debug", "ImpactDummyInput",
}


def _is_connection(v: Any) -> bool:
    # ComfyUI wires look like ["12", 0] — a 2-item [node_id, output_index].
    return (isinstance(v, list) and len(v) == 2
            and isinstance(v[0], str) and isinstance(v[1], int))


def _editable_value(v: Any) -> bool:
    if _is_connection(v):
        return False
    if isinstance(v, dict):      # widget header objects, etc.
        return False
    return isinstance(v, (str, int, float, bool))


def _skip_field(field: str) -> bool:
    """Structural junk fields, independent of node type:
    - 'isfloatX' — mxSlider's int/float toggle (collapsed into one column);
    - widget buttons whose key starts with a non-word char (e.g. rgthree's
      '➕ Add Lora')."""
    if field == "isfloatX":
        return True
    first = field[:1]
    return bool(first) and not (first.isalnum() or first == "_")


def _cast_of(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return "json"
    if isinstance(v, bool):
        return "str"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return "str"


def _is_lora_slot(field: str, v: Any) -> bool:
    """A Power Lora Loader (rgthree) slot: key like 'lora_3' holding a dict
    {on, lora, strength}. These are editable but stored as dicts (so the plain
    scalar filter skips them) — surface them as JSON columns instead."""
    return (field.startswith("lora_") and field[len("lora_"):].isdigit()
            and isinstance(v, dict))


def full_columns(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Editable inputs of every node as universal columns (title-based).

    Returns ordered list of {label, node, field, cast, image, dual, value}.
    Structural cleanup (no node-name hardcoding):
    - mxSlider-style nodes (have both ``Xi`` and ``Xf``) collapse into ONE
      column (``dual=True``) that writes both; ``isfloatX`` dropped;
    - widget-button keys ('➕ …') and connections/dicts skipped;
    - save/cleanup/debug nodes skipped entirely.
    Label = node title (+ '.<field>' if the node yields >1 column); duplicate
    labels get a '#<node_id>' suffix. ``image`` marks LoadImage.image (uploaded
    as a file); ``dual`` marks sliders (runtime writes Xi & Xf)."""
    items: list[dict[str, Any]] = []
    for nid, node in template.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        if ct in _SKIP_CLASSES:
            continue
        title = str((node.get("_meta") or {}).get("title") or ct or nid)
        ins = node.get("inputs") or {}
        # mxSlider family: one logical control split across Xi/Xf/isfloatX.
        if _editable_value(ins.get("Xi")) and "Xf" in ins:
            items.append({"node": str(nid), "field": "Xi", "value": ins.get("Xi"),
                          "cls": ct, "title": title, "dual": True})
            continue
        for field, v in ins.items():
            field = str(field)
            if _skip_field(field):
                continue
            # Power Lora Loader slots are dicts → expose as JSON columns.
            if _is_lora_slot(field, v) or _editable_value(v):
                items.append({"node": str(nid), "field": field, "value": v,
                              "cls": ct, "title": title, "dual": False})

    per_node: dict[str, int] = {}
    for it in items:
        per_node[it["node"]] = per_node.get(it["node"], 0) + 1
    for it in items:
        it["base"] = it["title"] if per_node[it["node"]] == 1 else f'{it["title"]}.{it["field"]}'
    base_counts: dict[str, int] = {}
    for it in items:
        base_counts[it["base"]] = base_counts.get(it["base"], 0) + 1

    cols: list[dict[str, Any]] = []
    for it in items:
        label = it["base"] if base_counts[it["base"]] == 1 else f'{it["base"]}#{it["node"]}'
        cols.append({
            "label": label, "node": it["node"], "field": it["field"],
            "cast": _cast_of(it["value"]), "value": it["value"],
            "image": it["cls"] in _LOADIMAGE_CLASSES and it["field"] == "image",
            "dual": it["dual"],
        })
    cols.sort(key=lambda c: (int(c["node"]) if c["node"].isdigit() else 1 << 30, c["field"]))
    return cols


def universal_sample_csv(template: dict[str, Any], workflow_key: str,
                         only_labels: list[str] | None = None) -> str:
    """One-row sample CSV with editable inputs as columns (title-based),
    pre-filled with the template's current values. If `only_labels` is given,
    keep just those columns (the set the user ticked at registration);
    otherwise dump every editable input."""
    cols = full_columns(template)
    if only_labels:
        want = set(only_labels)
        cols = [c for c in cols if c["label"] in want]
    header = ["workflow", "scenario", "girl"] + [c["label"] for c in cols]
    row = ["", "scene_01", "ModelName"]
    row[0] = workflow_key
    for c in cols:
        v = c["value"]
        if isinstance(v, (dict, list)):
            row.append(json.dumps(v, ensure_ascii=False))
        else:
            row.append("" if v is None else str(v))

    import csv as _csv
    import io
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(header)
    w.writerow(row)
    return buf.getvalue()


def load_video_template(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())
