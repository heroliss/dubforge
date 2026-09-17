# -*- coding: utf-8 -*-
"""设置接口: 读写 config.yaml (前端设置页与后端共用单一配置源)"""
from fastapi import APIRouter
from pydantic import BaseModel

from dubforge import config
from dubforge.errors import DubForgeError

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    updates: dict  # 点分路径 → 值, 例 {"tts.indextts25.url": "http://..."}


@router.get("")
def get_settings():
    # 返回完整配置; api_key 脱敏为布尔 (前端只显示"已配置/未配置", 明文不回传)
    cfg = config.load_config()
    key = cfg.get("llm", {}).get("api_key", "")
    cfg["llm"]["api_key"] = {"set": bool(key), "tail": key[-4:] if key else ""}
    hf = cfg.get("separate", {}).get("hf_token", "")
    cfg["separate"]["hf_token"] = {"set": bool(hf), "tail": hf[-4:] if hf else ""}
    return cfg


@router.post("")
def update_settings(body: SettingsUpdate):
    applied = {}
    for key, value in body.updates.items():
        # api_key/hf_token 特殊处理: 空串表示"保持不变", "-" 表示删除
        config.set_key(key, value)
        applied[key] = value
    return {"ok": True, "applied": applied}
