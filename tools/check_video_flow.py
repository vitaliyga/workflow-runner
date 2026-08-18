#!/usr/bin/env python
"""Проверка видео-флоу БЕЗ пода: как раннер разложит граф и что запатчит.

    uv run python tools/check_video_flow.py jobs/MH3.json
    uv run python tools/check_video_flow.py jobs/MH3.json --regress

Печатает:
  1) видео это или нет (is_video_workflow);
  2) каталожный маппинг + проверку «поле реально есть на ноде» — главный
     источник тихих промахов (значение из CSV уходит в несуществующий вход);
  3) universal-колонки CSV с флагами 🖼/🎬 (файлы, которые раннер заливает);
  4) сухой прогон build_video_workflow и контроль, что связи графа не поехали
     и мусорных входов не появилось.

`--regress` дополнительно гоняет синтетический mxSlider-флоу (LTX-стиль),
чтобы убедиться, что правки детектора не сломали старые флоу.

Ненулевой exit code = что-то не так; годится для CI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video_workflow_builder import (  # noqa: E402
    build_video_workflow,
    detect_video_mapping,
    full_columns,
    is_video_workflow,
)

PROBLEMS: list[str] = []


def _conns(graph: dict) -> dict:
    """Только связи [node_id, out_idx] — они не должны меняться никогда."""
    out = {}
    for nid, node in graph.items():
        for f, v in (node.get("inputs") or {}).items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int):
                out[f"{nid}.{f}"] = list(v)
    return out


def _fields(graph: dict) -> set[str]:
    return {f"{nid}.{f}" for nid, node in graph.items() for f in (node.get("inputs") or {})}


def check(template: dict) -> None:
    print(f"is_video_workflow: {is_video_workflow(template)}")

    print("\n-- каталожный маппинг ---------------------------------------")
    mapping = detect_video_mapping(template)
    for key, ref in mapping.items():
        node = template.get(ref["node"]) or {}
        title = (node.get("_meta") or {}).get("title", "")
        field = ref["field"]
        exists = field == "" or field in (node.get("inputs") or {})
        if not exists:
            PROBLEMS.append(f"{key}: нода {ref['node']} не имеет входа '{field}'")
        print(f"  {key:24s} -> {ref['node']:>4s}.{field:<15s} {title:<34s} "
              f"{'ok' if exists else 'НЕТ ТАКОГО ВХОДА'}")

    print("\n-- universal-колонки CSV ------------------------------------")
    cols = full_columns(template)
    for c in cols:
        kind = "🖼 фото" if c["image"] else ("🎬 видео" if c.get("video") else c["cast"])
        print(f"  {c['label']:<48s} {kind:<8s} {c['node']}.{c['field']}")
    print(f"  итого колонок: {len(cols)}")

    print("\n-- сухой прогон патча --------------------------------------")
    values = {
        "input_image": "TEST_IMAGE", "input_video": "TEST_VIDEO",
        "prompt_positive": "TEST_PROMPT", "prompt_negative": "TEST_NEG",
        "seed": 424242, "video_length_seconds": 7, "video_width": 640,
        "video_height": 480, "steps": 9, "denoise": 0.8, "scheduler": "karras",
        "sampler_name": "euler", "lora_name": "TEST_LORA", "lora_strength": 0.5,
        "diffusion_model_name": "TEST_UNET", "checkpoint_name": "TEST_CKPT",
    }
    patched = build_video_workflow(template, values, mapping, save_prefix="video/dry-run")
    changed = 0
    for nid in sorted(patched, key=lambda x: int(x) if x.isdigit() else 1 << 30):
        before, after = template[nid].get("inputs", {}), patched[nid].get("inputs", {})
        for f in sorted(set(before) | set(after)):
            if before.get(f, "<нет>") != after.get(f, "<нет>"):
                changed += 1
                print(f"  {nid:>4s}.{f:<16s} {str(before.get(f, '<нет>'))[:24]!r} -> "
                      f"{str(after.get(f))[:26]!r} ({type(after.get(f)).__name__})")

    if _conns(template) != _conns(patched):
        PROBLEMS.append("СВЯЗИ ГРАФА ИЗМЕНИЛИСЬ при патче")
    junk = _fields(patched) - _fields(template)
    if junk:
        PROBLEMS.append(f"появились входы, которых нет в шаблоне: {sorted(junk)}")
    try:
        json.dumps(patched)
    except (TypeError, ValueError) as e:
        PROBLEMS.append(f"граф не сериализуется для /prompt: {e}")
    print(f"  изменено входов: {changed} | связи целы: {_conns(template) == _conns(patched)} "
          f"| мусорных входов: {len(junk)}")


def regress() -> None:
    """Синтетический LTX-подобный флоу: mxSlider (Xi/Xf), CLIPTextEncode, rgthree."""
    print("\n== РЕГРЕСС: mxSlider / CLIPTextEncode / Power Lora Loader ==")
    ltx = {
        "18": {"inputs": {"Xi": 5, "Xf": 5, "isfloatX": False}, "class_type": "mxSlider",
               "_meta": {"title": "Length (sec)"}},
        "19": {"inputs": {"Xi": 832, "Xf": 832, "isfloatX": False}, "class_type": "mxSlider",
               "_meta": {"title": "Width"}},
        "181": {"inputs": {"Xi": 1216, "Xf": 1216, "isfloatX": False}, "class_type": "mxSlider",
                "_meta": {"title": "Height"}},
        "28": {"inputs": {"text": "a"}, "class_type": "CLIPTextEncode",
               "_meta": {"title": "Positive Prompt"}},
        "29": {"inputs": {"text": "b"}, "class_type": "CLIPTextEncode",
               "_meta": {"title": "Negative Prompt"}},
        "15": {"inputs": {"image": "x.png"}, "class_type": "LoadImage",
               "_meta": {"title": "Load Image"}},
        "6": {"inputs": {"lora_1": {"on": True, "lora": "l.safetensors", "strength": 1.0}},
              "class_type": "Power Lora Loader (rgthree)", "_meta": {"title": "Power Lora Loader"}},
        "61": {"inputs": {"filename_prefix": "v", "images": ["28", 0]},
               "class_type": "VHS_VideoCombine", "_meta": {"title": "Video Combine"}},
    }
    m = detect_video_mapping(ltx)
    out = build_video_workflow(ltx, {
        "video_length_seconds": 7, "video_width": 640, "video_height": 480,
        "prompt_positive": "P", "prompt_negative": "N", "input_image": "up.png",
        "load_loras_json": '{"lora_1": {"on": true, "lora": "z.safetensors", "strength": 0.5}}',
    }, m)
    expect = [
        ("длина -> Xi/Xf", out["18"]["inputs"] == {"Xi": 7, "Xf": 7, "isfloatX": False}),
        ("ширина -> Xi/Xf", out["19"]["inputs"]["Xi"] == 640 and out["19"]["inputs"]["Xf"] == 640),
        ("высота -> Xi/Xf", out["181"]["inputs"]["Xf"] == 480),
        ("промпты -> text", out["28"]["inputs"]["text"] == "P" and out["29"]["inputs"]["text"] == "N"),
        ("фото -> image", out["15"]["inputs"]["image"] == "up.png"),
        ("rgthree lora -> JSON", out["6"]["inputs"]["lora_1"]["lora"] == "z.safetensors"),
    ]
    for name, ok in expect:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            PROBLEMS.append(f"регресс сломан: {name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow", help="путь к API-format JSON")
    ap.add_argument("--regress", action="store_true", help="плюс синтетический LTX-флоу")
    args = ap.parse_args()

    path = Path(args.workflow)
    template = json.loads(path.read_text(encoding="utf-8"))
    print(f"=== {path} ({len(template)} нод) ===")
    check(template)
    if args.regress:
        regress()

    print()
    if PROBLEMS:
        print("ПРОБЛЕМЫ:")
        for p in PROBLEMS:
            print(f"  - {p}")
        return 1
    print("ВСЁ ЧИСТО")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
