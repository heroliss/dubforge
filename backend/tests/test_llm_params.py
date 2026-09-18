# -*- coding: utf-8 -*-
"""LLM 专属参数合并: profile.params 覆盖通用默认; 留空则不多发任何字段"""
from dubforge.backends.llm import _payload

MSGS = [{"role": "user", "content": "hi"}]


def test_profile_params_merge_and_override(isolated_env):
    provider = {"model": "m", "params": {"temperature": 1, "top_p": 0.95,
                                         "reasoning_effort": "max"}}
    body = _payload(provider, MSGS, 0.0, json_mode=False)
    # profile 专属参数覆盖通用默认 (temperature 0.0 → 1), 并原样带上扩展字段
    assert body["temperature"] == 1
    assert body["top_p"] == 0.95
    assert body["reasoning_effort"] == "max"


def test_empty_params_send_nothing_extra(isolated_env):
    body = _payload({"model": "m"}, MSGS, 0.0, json_mode=False)
    assert body["temperature"] == 0.0          # 仍用传入的通用默认
    assert "top_p" not in body and "reasoning_effort" not in body


def test_test_provider_failure_returns_error_dict(isolated_env):
    # 测试失败必须返回 {ok: False, error...} 而不是抛异常 (回归: 曾因未导入
    # DubForgeError 在 except 处 NameError → 500)
    from dubforge.backends.llm import test_provider
    r = test_provider({"name": "假配置", "base_url": "http://127.0.0.1:9",
                       "api_key": "x", "model": "m"})
    assert r["ok"] is False
    assert r.get("error")
    assert r["request"]["url"].endswith("/chat/completions")


def test_status_hint_is_status_specific():
    # 修复建议必须与状态码对应 (回归: 曾对一切非 200 都提示 429 限流)
    from dubforge.backends.llm import _status_hint
    assert "限流" in _status_hint(429)
    assert "/chat/completions" in _status_hint(404)
    assert "api_key" in _status_hint(403)
    assert _status_hint(502) is not None
    assert _status_hint(418) is None          # 无合适建议 → 不显示
