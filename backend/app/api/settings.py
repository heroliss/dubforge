# -*- coding: utf-8 -*-
"""设置接口: 读写 config.yaml (前端设置页与后端共用单一配置源) + LLM 连通性测试"""
import json

from fastapi import APIRouter
from pydantic import BaseModel

from dubforge import config
from dubforge.errors import DubForgeError

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 前端"留空 = 保持不变"的敏感键
_KEEP_IF_EMPTY = {"llm.api_key", "separate.hf_token"}


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
    """每步骤可编辑参数的 schema + 当前值 (值实时读 config, 步骤页表单用)"""
    from dubforge import config as cfg
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, schema_view
    values = {f.path: cfg.get(f.path)
              for fields in STEP_PARAM_SCHEMA.values() for f in fields}
    return {"ok": True, "steps": schema_view(values)}


@router.get("")
def get_settings():
    # 本机单用户应用: api_key 明文回传 (前端密码框直接显示, 可切换明文)
    return config.load_config()


@router.post("")
def update_settings(body: SettingsUpdate):
    applied = {}
    for key, value in body.updates.items():
        config.set_key(key, value)
        applied[key] = value
    return {"ok": True, "applied": applied}


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
