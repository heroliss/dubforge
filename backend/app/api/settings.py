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
    """LLM 连通性测试。

    - body 为空: 并行测试全部「启用」的配置, 返回逐个结果
    - body {"index": i}: 只测第 i 组配置 (含停用的, 便于启用前验证)
    """
    from concurrent.futures import ThreadPoolExecutor

    from dubforge.backends import llm

    all_profiles = config.load_config().get("llm", {}).get("profiles") or []
    # 兼容旧版单配置
    if not all_profiles and config.get("llm.base_url"):
        all_profiles = [{"name": "default", "enabled": True,
                         **{k: config.get(f"llm.{k}") for k in
                            ("base_url", "api_key", "model")}}]

    idx = (body or {}).get("index")
    if idx is not None:
        idx = int(idx)
        if idx < 0 or idx >= len(all_profiles):
            raise DubForgeError(f"配置序号不存在: {idx}", code="PROFILE_NOT_FOUND")
        return {"results": [{**llm.test_provider(all_profiles[idx]),
                             "index": idx, "name": all_profiles[idx].get("name")}]}
    enabled = [(i, p) for i, p in enumerate(all_profiles) if p.get("enabled", True)]
    if not enabled:
        raise DubForgeError("没有启用的 LLM 配置", code="NO_ENABLED_PROFILE",
                            suggestion="在设置页勾选至少一组配置的「启用」开关。")
    with ThreadPoolExecutor(max_workers=len(enabled)) as pool:
        results = list(pool.map(
            lambda ip: {**llm.test_provider(ip[1]), "index": ip[0],
                        "name": ip[1].get("name")}, enabled))
    return {"results": results,
            "ok": any(r["ok"] for r in results)}
