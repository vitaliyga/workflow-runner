"""Patch a ComfyUI API-format workflow with per-job parameters.

Each workflow lives under workflows/<name>/ with two files:
  - template.json   (API format)
  - mapping.yaml    (role -> {node, field} indirection)

The mapping shields the runner from per-workflow node-id and field-name
differences, so a new flow == new folder, no code changes.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Default field names for standard ComfyUI node types. Overridable per role
# via mapping.yaml's `fields:` block.
_DEFAULT_KSAMPLER_FIELDS = {
    "seed": "seed",
    "steps": "steps",
    "cfg": "cfg",
    "sampler_name": "sampler_name",
    "scheduler": "scheduler",
    "denoise": "denoise",
}
_DEFAULT_LORA_FIELDS = {
    "name": "lora_name",
    "strength_model": "strength_model",
    "strength_clip": "strength_clip",
}


@dataclass
class NodeRef:
    node: str
    field: str = "text"
    cast: str = "str"

    @classmethod
    def parse(cls, raw: Any, default_field: str = "text",
              default_cast: str = "str") -> "NodeRef":
        if isinstance(raw, str):
            return cls(node=str(raw), field=default_field, cast=default_cast)
        return cls(
            node=str(raw["node"]),
            field=str(raw.get("field", default_field)),
            cast=str(raw.get("cast", default_cast)),
        )


@dataclass
class KSamplerRef:
    node: str
    fields: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_KSAMPLER_FIELDS))


@dataclass
class LoraRef:
    node: str
    fields: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_LORA_FIELDS))


def _parse_set_fields(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize a `set_fields` block: {node_id: {field: value}}. Node ids are
    coerced to str; values are kept verbatim. Non-dict entries are skipped."""
    return {
        str(nid): dict(fields)
        for nid, fields in (data.get("set_fields") or {}).items()
        if isinstance(fields, dict)
    }


@dataclass
class WorkflowMapping:
    name: str
    ksampler: KSamplerRef
    positive_prompt: NodeRef
    negative_prompt: NodeRef
    load_images: dict[str, NodeRef]       # role -> ref (e.g. {"main": ...})
    save_images: list[str]                # node IDs
    lora_loaders: list[LoraRef]
    extra_inputs: dict[str, NodeRef] = field(default_factory=dict)
    # Static widget values injected verbatim at build time: {node_id: {field: value}}.
    # Use to fill a required widget that a template's API export is missing, or to
    # pin a value from config without editing the JSON. Opt-in per flow.
    set_fields: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, name: str, path: Path) -> "WorkflowMapping":
        data = yaml.safe_load(path.read_text())

        ks_raw = data["ksampler"]
        ks_node = ks_raw["node"] if isinstance(ks_raw, dict) else ks_raw
        ks_fields = dict(_DEFAULT_KSAMPLER_FIELDS)
        if isinstance(ks_raw, dict) and ks_raw.get("fields"):
            ks_fields.update(ks_raw["fields"])

        load_images: dict[str, NodeRef] = {}
        li_raw = data.get("load_images") or {}
        for role, ref in li_raw.items():
            load_images[role] = NodeRef.parse(ref, default_field="image")

        save_raw = data.get("save_images") or []
        save_ids = [str(x) for x in save_raw] if isinstance(save_raw, list) else [str(save_raw)]

        loras: list[LoraRef] = []
        for lr in data.get("lora_loaders") or []:
            n = lr["node"] if isinstance(lr, dict) else lr
            fl = dict(_DEFAULT_LORA_FIELDS)
            if isinstance(lr, dict) and lr.get("fields"):
                fl.update(lr["fields"])
            loras.append(LoraRef(node=str(n), fields=fl))

        extras: dict[str, NodeRef] = {}
        for key, ref in (data.get("extra_inputs") or {}).items():
            extras[key] = NodeRef.parse(ref, default_field="text")

        return cls(
            name=name,
            ksampler=KSamplerRef(node=str(ks_node), fields=ks_fields),
            positive_prompt=NodeRef.parse(data["positive_prompt"], default_field="text"),
            negative_prompt=NodeRef.parse(data["negative_prompt"], default_field="text"),
            load_images=load_images,
            save_images=save_ids,
            lora_loaders=loras,
            extra_inputs=extras,
            set_fields=_parse_set_fields(data),
        )


@dataclass
class JobParams:
    lora_name: str
    lora_strength_model: float
    lora_strength_clip: float
    prompt_positive: str
    prompt_negative: str
    input_image: str          # filename usable by LoadImage on the pod
    input_images: tuple[str, ...]
    seed: int
    steps: int
    cfg: float
    sampler_name: str
    scheduler: str
    denoise: float
    save_prefix: str
    extra: dict[str, str] = field(default_factory=dict)


def load_workflow_template(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def build_workflow(template: dict[str, Any], mapping: WorkflowMapping,
                   params: JobParams) -> dict[str, Any]:
    wf = copy.deepcopy(template)

    def inputs(nid: str) -> dict[str, Any]:
        if nid not in wf:
            raise KeyError(f"workflow {mapping.name}: node {nid!r} not in template")
        return wf[nid].setdefault("inputs", {})

    def set_field(nid: str, field_path: str, value: Any) -> None:
        node_in = inputs(nid)
        cur: Any = node_in
        parts = field_path.split(".")
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value

    def coerce(value: Any, cast: str) -> Any:
        if cast in ("str", "string", ""):
            return "" if value is None else str(value)
        if cast in ("int", "integer"):
            return int(float(value))
        if cast in ("float", "number"):
            return float(value)
        if cast in ("bool", "boolean"):
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            return s in {"1", "true", "yes", "y", "on"}
        if cast == "json":
            return json.loads(value) if isinstance(value, str) else value
        return value

    # KSampler
    ks = inputs(mapping.ksampler.node)
    ks_f = mapping.ksampler.fields
    ks[ks_f["seed"]] = int(params.seed)
    ks[ks_f["steps"]] = int(params.steps)
    ks[ks_f["cfg"]] = float(params.cfg)
    ks[ks_f["sampler_name"]] = params.sampler_name
    ks[ks_f["scheduler"]] = params.scheduler
    ks[ks_f["denoise"]] = float(params.denoise)

    # Prompts
    set_field(mapping.positive_prompt.node, mapping.positive_prompt.field, params.prompt_positive)
    set_field(mapping.negative_prompt.node, mapping.negative_prompt.field, params.prompt_negative)

    # Input images are assigned in declared role order. "main" stays first
    # when present so single-image workflows keep the existing behavior.
    image_roles = list(mapping.load_images.items())
    image_roles.sort(key=lambda item: item[0] != "main")
    images = tuple(img for img in (params.input_images or (params.input_image,)) if img)
    if not image_roles:
        # txt2img / no-input flow (e.g. an EmptySD3LatentImage graph with no
        # LoadImage node): nothing to assign. Only complain if the row still
        # carried images that have nowhere to go.
        if images:
            raise ValueError(
                f"workflow {mapping.name}: {len(images)} input image(s) supplied "
                f"but load_images has no roles configured"
            )
    else:
        if len(images) != len(image_roles):
            raise ValueError(
                f"workflow {mapping.name}: input image count ({len(images)}) "
                f"does not match load_images roles ({len(image_roles)})"
            )
        for (role, ref), img in zip(image_roles, images):
            set_field(ref.node, ref.field, img)

    # LoRA — patch the first loader. Multi-LoRA stack: extend JobParams to a
    # list and iterate here.
    # Skip entirely when the CSV has no lora_name (empty): leave the template's
    # own value intact. "Load Lora" rejects "" (only 'None' or a real file are
    # valid), so overwriting a no-lora template ('None', strength 0) with an
    # empty string causes a submit-time validation error.
    if mapping.lora_loaders and params.lora_name:
        lora = mapping.lora_loaders[0]
        node_in = inputs(lora.node)
        node_in[lora.fields["name"]] = params.lora_name
        # Tolerate Flux-style LoraLoaderModelOnly (no strength_clip).
        if lora.fields["strength_model"] in node_in or "strength" in node_in:
            key = lora.fields["strength_model"] if lora.fields["strength_model"] in node_in else "strength"
            node_in[key] = float(params.lora_strength_model)
        if lora.fields["strength_clip"] in node_in:
            node_in[lora.fields["strength_clip"]] = float(params.lora_strength_clip)

    for key, ref in mapping.extra_inputs.items():
        if key not in params.extra:
            continue
        raw = params.extra.get(key)
        if raw is None or raw == "":
            continue
        set_field(ref.node, ref.field, coerce(raw, ref.cast))

    # SaveImage prefixes — each gets the same per-job prefix with a node-id
    # suffix so concurrent jobs don't collide and multi-stage outputs stay
    # distinguishable.
    for sid in mapping.save_images:
        suffix = "" if len(mapping.save_images) == 1 else f"_n{sid}"
        set_field(sid, "filename_prefix", params.save_prefix + suffix)

    # Static widget values from the mapping's `set_fields`. Applied last so they
    # win over anything above. inputs() raises a clear error if the node id is
    # wrong, so a stale mapping fails loud instead of silently no-op'ing.
    for nid, fields in mapping.set_fields.items():
        for field_path, value in fields.items():
            set_field(str(nid), field_path, value)

    return wf


# ---- workflow registry ---------------------------------------------------

@dataclass
class WorkflowBundle:
    name: str
    template: dict[str, Any]
    mapping: WorkflowMapping


def _mapping_from_dict(name: str, data: dict[str, Any]) -> WorkflowMapping:
    """Same parsing logic as WorkflowMapping.load but from a dict, so the
    single workflows.yaml can pass merged-with-defaults blocks straight in."""
    ks_raw = data["ksampler"]
    ks_node = ks_raw["node"] if isinstance(ks_raw, dict) else ks_raw
    ks_fields = dict(_DEFAULT_KSAMPLER_FIELDS)
    if isinstance(ks_raw, dict) and ks_raw.get("fields"):
        ks_fields.update(ks_raw["fields"])

    load_images: dict[str, NodeRef] = {}
    for role, ref in (data.get("load_images") or {}).items():
        load_images[role] = NodeRef.parse(ref, default_field="image")

    save_raw = data.get("save_images") or []
    save_ids = [str(x) for x in save_raw] if isinstance(save_raw, list) else [str(save_raw)]

    loras: list[LoraRef] = []
    for lr in data.get("lora_loaders") or []:
        n = lr["node"] if isinstance(lr, dict) else lr
        fl = dict(_DEFAULT_LORA_FIELDS)
        if isinstance(lr, dict) and lr.get("fields"):
            fl.update(lr["fields"])
        loras.append(LoraRef(node=str(n), fields=fl))

    return WorkflowMapping(
        name=name,
        ksampler=KSamplerRef(node=str(ks_node), fields=ks_fields),
        positive_prompt=NodeRef.parse(data["positive_prompt"], default_field="text"),
        negative_prompt=NodeRef.parse(data["negative_prompt"], default_field="text"),
        load_images=load_images,
        save_images=save_ids,
        lora_loaders=loras,
        extra_inputs={
            key: NodeRef.parse(ref, default_field="text")
            for key, ref in (data.get("extra_inputs") or {}).items()
        },
        set_fields=_parse_set_fields(data),
    )


class WorkflowRegistry:
    """Loads all workflows from a single YAML with `defaults:` + per-flow
    overrides + a `template:` pointing at the API-format JSON."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.root = config_path.parent
        raw = yaml.safe_load(config_path.read_text()) or {}
        self.defaults: dict[str, Any] = raw.get("defaults") or {}
        self.flows: dict[str, dict[str, Any]] = raw.get("workflows") or {}
        self._cache: dict[str, WorkflowBundle] = {}

    def get(self, name: str) -> WorkflowBundle:
        if name in self._cache:
            return self._cache[name]
        if name not in self.flows:
            available = ", ".join(sorted(self.flows)) or "(none)"
            raise KeyError(
                f"workflow {name!r} not in {self.config_path}. "
                f"Available: {available}")
        spec = self.flows[name]
        merged = {**self.defaults, **spec}        # shallow merge
        template_path = self.root / merged.pop("template")
        bundle = WorkflowBundle(
            name=name,
            template=load_workflow_template(template_path),
            mapping=_mapping_from_dict(name, merged),
        )
        self._cache[name] = bundle
        return bundle
