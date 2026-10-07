"""设置接口: 读写 config.yaml (前端设置页与后端共用单一配置源) + LLM 连通性测试"""
import json

from dubforge import config
from dubforge.errors import DubForgeError
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/environment")
def environment():
    from dubforge.environment import report
    return report()

# 前端"留空 = 保持不变"的敏感键
_KEEP_IF_EMPTY = {"llm.api_key"}


class SettingsUpdate(BaseModel):
    updates: dict  # 点分路径 → 值, 例 {"llm.model": "glm-5.3-flash"}


@router.post("/rebuild_db")
def rebuild_db():
    """从项目目录的 manifest.json 重建索引 (只补缺不覆盖, 幂等)。
    DB 丢失/损坏后的手动恢复入口; 拷进来的项目文件夹也会被收编。"""
    from dubforge.domain import crud
    return {"ok": True, **crud.rebuild_from_dirs()}


@router.get("/step_params")
def step_params():
    """每步骤可编辑参数的 schema + 当前值 (值实时读 config, 步骤页表单用);
    附带语言能力 caps (各后端声明的 LANGUAGES, 用于下拉注入与能力展示)"""
    from dubforge import config as cfg
    from dubforge.pipeline.step_params import (
        STEP_PARAM_SCHEMA,
        COMMENT_DEFAULT_FIELDS,
        language_capabilities,
        schema_view,
    )
    settings = cfg.load_config()
    values = {f.path: cfg.get(f.path, settings=settings)
              for fields in (*STEP_PARAM_SCHEMA.values(), COMMENT_DEFAULT_FIELDS) for f in fields}
    caps = language_capabilities(settings=settings)
    from dubforge.backends.tts_indextts25 import synthesis_profiles
    return {"ok": True, "steps": schema_view(values, caps, settings=settings, global_defaults=True),
            "caps": caps, "indextts_profiles": synthesis_profiles()}


@router.get("/llm_display")
def llm_display():
    """返回 LLM 流式文字显示参数，不暴露任何模型连接信息。"""
    raw = config.get("ui.llm_stream.reference_distance_chars", 500)
    try:
        distance = max(50, min(5000, int(raw)))
    except (TypeError, ValueError):
        distance = 500
    return {"ok": True, "reference_distance_chars": distance,
            "reference_speed_chars_per_second": 360}


@router.get("/progress_timing")
def progress_timing():
    """与运行估算共用历史数据，不再维护另一套阶段比例。"""
    from dubforge.progress import HISTORY_LIMIT, LLM_DEFAULT_SECONDS, middle_third_mean, timing_history

    profiles = []
    for key, group in timing_history().items():
        rows = group["samples"]
        profiles.append({"key": key, "label": group["label"], "samples": rows,
                         "trimmed_mean_seconds": middle_third_mean(r["seconds"] for r in rows) or 0})
    return {"ok": True, "profiles": profiles,
            "sample_count": sum(len(g["samples"]) for g in profiles),
            "history_limit": HISTORY_LIMIT, "minimum_samples": 1,
            "llm_default_seconds": LLM_DEFAULT_SECONDS,
            "download_speed": "cumulative_average"}


@router.get("")
def get_settings():
    # 本机单用户应用: api_key 明文回传 (前端密码框直接显示, 可切换明文)
    return config.load_config()


@router.post("")
def update_settings(body: SettingsUpdate):
    from dubforge.pipeline.step_params import validate_numeric_updates
    from dubforge.comment_studio import validate_default_updates
    from dubforge import runtime_edit
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate:
        if not runtime_edit.display_only(body.updates):
            runtime_edit.assert_editable('global')
        validate_numeric_updates(body.updates)
        validate_default_updates(body.updates)
        config.update_config(body.updates)
    return {"ok": True, "applied": body.updates}


@router.post("/test_llm")
def test_llm(body: dict | None = None):
    """LLM 连通性测试 —— 只探测传入的表单值, 不读写 config.yaml (应用是唯一写入口)。

    body: {"profiles": [配置...]}
      - profiles: 前端当前表单里的配置 (一组=单测, 多组=并行测)
      - 不传 profiles 时兼容旧行为: 测 config 中所有启用组
    返回 {"results": [{index(传入数组中的位置), name, ok, ...}], "ok": 任一组可用}
    """
    from concurrent.futures import ThreadPoolExecutor

    from dubforge.backends import llm

    profiles = (body or {}).get("profiles")
    if not profiles:  # 兼容: 未传则测 config 启用组
        profiles = [p for p in (config.load_config().get("llm", {})
                                .get("profiles") or []) if p.get("enabled", True)]
        if not profiles:
            raise DubForgeError("没有启用的 LLM 配置", code="NO_ENABLED_PROFILE",
                                suggestion="在设置页勾选至少一组配置的「启用」开关。")

    def probe(item):
        i, p = item
        return {**llm.test_provider(p), "index": i, "name": p.get("name")}

    with ThreadPoolExecutor(max_workers=min(len(profiles), 8)) as pool:
        results = list(pool.map(probe, enumerate(profiles)))
    return {"results": results, "ok": any(r["ok"] for r in results)}
