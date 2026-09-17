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
def test_llm():
    """LLM 连通性测试: 按配置池顺序发一个最小翻译请求, 返回时延与回复预览"""
    import time

    from dubforge.backends import llm
    if not llm.check_online():
        return {"ok": False, "message": f"服务不可达: {llm._profiles()}"}
    t0 = time.time()
    try:
        reply = llm.chat_json(
            '请把这一行字幕翻译成简体中文, 输出 JSON 数组 [{"id": 1, "dst": "..."}]:\n\n'
            '1. Hello world, this is a connection test.',
            validator=lambda o: o if isinstance(o, list) and o else (_ for _ in ()).throw(
                ValueError("输出不是非空数组")),
            temperature=0.0)
    except DubForgeError as e:
        return {"ok": False, "message": f"{e.message}",
                "suggestion": e.suggestion, "detail": str(e.detail or "")[:300]}
    except Exception as e:  # noqa: BLE001 (测试端点兜底, 不让 500 裸奔)
        return {"ok": False, "message": f"未预期错误: {type(e).__name__}: {e}"}
    dt = time.time() - t0
    return {"ok": True, "latency": round(dt, 1),
            "reply": json.dumps(reply, ensure_ascii=False)[:200]}
