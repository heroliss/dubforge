# -*- coding: utf-8 -*-
"""工作台预设扩展: WebUI 预设一键应用 + 基线差异对比
预设来自 index-tts 的 outputs/presets/<name>/preset.json (双维度参考语义
沿用 VideoLingo-Workspace 验证过的映射: preset 内 "same" → 两维同填)"""
import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from dubforge import config
from dubforge.errors import DubForgeError

router = APIRouter(prefix="/api/workbench-presets", tags=["workbench-presets"])

# preset.json 键 → config 键 (index_tts 段)
_BASE_MAP = {
    "do_sample": "do_sample", "top_p": "top_p", "top_k": "top_k",
    "temperature": "temperature", "length_penalty": "length_penalty",
    "num_beams": "num_beams", "repetition_penalty": "repetition_penalty",
    "max_mel_tokens": "max_mel_tokens",
    "max_text_tokens_per_segment": "max_text_tokens_per_segment",
}
_EMO_MAP = {1: "preset", 2: "preset", 3: "vector"}


def _presets_dir() -> Path:
    project_dir = config.get("tts.indextts25.project_dir") or ""
    if project_dir:
        p = Path(project_dir)
        if p.exists():
            return p / "outputs" / "presets"
    # 自动查找: 工作区同级 (VideoLingo-Workspace 布局惯例)
    for base in (config.REPO_ROOT.parent, config.REPO_ROOT):
        cand = base / "index-tts" / "outputs" / "presets"
        if cand.is_dir():
            return cand
    raise DubForgeError("未找到 index-tts 预设目录", code="PRESETS_DIR_MISSING",
                        suggestion="请在设置页 tts.indextts25.project_dir 填写 index-tts 项目根。")


def _apply_preset(name: str) -> dict:
    """应用预设: 双维度全量填参 + 记录基线 (供差异对比)"""
    pdir = _presets_dir() / name
    pf = pdir / "preset.json"
    if not pf.exists():
        raise DubForgeError(f"预设不存在: {name}", code="PRESET_MISSING")
    data = json.loads(pf.read_text(encoding="utf-8"))
    adv = data.get("advanced_params", {}) or {}

    for pk, ck in _BASE_MAP.items():
        if pk in adv:
            config.set_key(f"tts.indextts25.{ck}", adv[pk])

    pa = (data.get("prompt_audio") or "").strip()
    if pa:
        src = pdir / pa
        if src.exists():
            fwd = str(src).replace("\\", "/")
            config.set_key("tts.indextts25.timbre_mode", "preset")
            config.set_key("tts.indextts25.timbre_audio", fwd)
            emo_m = int(data.get("emo_control_method", 1))
            config.set_key("tts.indextts25.emo_mode", _EMO_MAP.get(emo_m, "preset"))
            config.set_key("tts.indextts25.emo_audio", fwd)
    config.set_key("tts.indextts25.emo_weight", float(data.get("emo_weight", 0.65)))
    config.set_key("tts.indextts25.emo_vector",
                   [float(x) for x in (data.get("emo_vector") or [0] * 8)])
    config.set_key("tts.indextts25.source_preset", name)
    config.set_key("tts.indextts25.preset_baseline", dict(adv))
    return {"applied": name, "audio": pa or None}


@router.get("")
def list_presets():
    try:
        names = sorted(d.name for d in _presets_dir().iterdir()
                       if (d / "preset.json").is_file())
    except DubForgeError:
        return {"presets": [], "error": "预设目录未找到"}
    return {"presets": names, "source": config.get("tts.indextts25.source_preset", "")}


class ApplyBody(BaseModel):
    name: str


@router.post("/apply")
def apply(body: ApplyBody):
    r = _apply_preset(body.name.strip())
    return {"ok": True, **r}


@router.get("/diff")
def diff():
    """当前值 vs 基线 (应用预设时的快照); 无基线返回空 diff"""
    baseline = config.get("tts.indextts25.preset_baseline") or {}
    if not baseline:
        return {"diff": [], "source": config.get("tts.indextts25.source_preset", "")}
    diff = []
    for pk, val in baseline.items():
        ck = _BASE_MAP.get(pk)
        if ck:
            cur = config.get(f"tts.indextts25.{ck}")
            if cur != val:
                diff.append({"key": ck, "preset": val, "current": cur})
    for mk in ("timbre_mode", "emo_mode", "timbre_audio", "emo_audio"):
        bkey = mk + "_path" if mk.endswith("audio") else mk
        if bkey in baseline:
            cur = config.get(f"tts.indextts25.{mk}")
            if cur != baseline[bkey]:
                diff.append({"key": mk, "preset": baseline[bkey], "current": cur})
    return {"diff": diff, "source": config.get("tts.indextts25.source_preset", "")}
