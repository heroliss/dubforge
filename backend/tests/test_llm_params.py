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


def test_status_title_carries_brief_meaning():
    # 标题 = 状态码 + 码义 + 该场景最可能的原因 (只指来源, 不写修复步骤)
    from dubforge.backends.llm import _status_title
    # 404 特殊: OpenAI 惯例"模型不存在"也报 404, 按响应内容区分
    assert _status_title(404, '{"error":{"message":"模型不存在"}}') == \
        "LLM 请求失败 HTTP 404 —— 资源不存在（可能是模型名不对）"
    assert _status_title(404, "The model `x` does not exist") == \
        "LLM 请求失败 HTTP 404 —— 资源不存在（可能是模型名不对）"
    assert _status_title(404, '{"detail":"Not Found"}') == \
        "LLM 请求失败 HTTP 404 —— 资源不存在（可能是接口地址填错）"
    assert "限流" in _status_title(429, "") and "配额" in _status_title(429, "")
    assert "服务器错误" in _status_title(502) and "与本地配置无关" in _status_title(502)
    assert _status_title(418).endswith("HTTP 418")   # 无含义的码只显示数字


def test_explicit_empty_suggestion_clears_default():
    # 服务器已响应的错误不再自造建议: 传 "" 显式清除类默认
    # (回归: 曾把 StepFailedError 的管线页默认建议泄漏到设置页)
    from dubforge.errors import StepFailedError
    e = StepFailedError("LLM 请求失败 HTTP 404", detail=r'{"detail":"Not Found"}',
                        suggestion="")
    assert e.suggestion == ""
    assert StepFailedError("x").suggestion          # 不传 → 类默认仍在
    assert StepFailedError("x", suggestion="自拟建议").suggestion == "自拟建议"
