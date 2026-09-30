"""LLM 专属参数合并: profile.params 覆盖通用默认; 留空则不多发任何字段"""
import pytest
from dubforge.backends.llm import _payload
from dubforge.errors import StepFailedError

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


def test_unconfigured_temperature_is_omitted(isolated_env):
    """步骤没有指定采样参数时, 请求体应完全交给服务商默认值。"""
    body = _payload({"model": "m"}, MSGS, None, json_mode=False)
    assert "temperature" not in body
    assert body == {"model": "m", "messages": MSGS}


def test_json_mode_is_protocol_field_not_sampling_default(isolated_env):
    body = _payload({"model": "m"}, MSGS, None, json_mode=True)
    assert "temperature" not in body
    assert body["response_format"] == {"type": "json_object"}


def test_extract_json_keeps_outer_object_with_nested_lines():
    """翻译响应的外层对象不能被内层 lines 数组截断。"""
    from dubforge.backends.llm import _extract_json

    obj = _extract_json('{"lines":[{"id":1,"dst":"译文"}],'
                        '"handoff_summary":"下一窗口继续沿用术语"}')
    assert obj["lines"][0]["dst"] == "译文"
    assert obj["handoff_summary"] == "下一窗口继续沿用术语"


def test_extract_json_repairs_deterministic_small_errors():
    """常见 JSON 小错先本地确定性修复，避免浪费一次完整上下文重试。"""
    from dubforge.backends.llm import _extract_json

    # 实际事故形态：两个数字行键之间混入孤立字符串。
    stray = '{"lines":{"1":"一","2":"二","多余","3":"三"}}'
    obj, method = _extract_json(stray, return_repair=True)
    assert obj["lines"] == {"1": "一", "2": "二", "3": "三"}
    assert "孤立字符串" in method

    # 裸数字键 + 缺失右括号。
    bare = '{"lines":{1:"一",2:"二"'
    obj, method = _extract_json(bare, return_repair=True)
    assert obj["lines"] == {"1": "一", "2": "二"}
    assert "数字键" in method and "右括号" in method

    # 字符串里的裸换行可以安全转义。
    raw_newline = '{"lines":{"1":"第一行\n第二行"}}'
    obj, method = _extract_json(raw_newline, return_repair=True)
    assert obj["lines"]["1"] == "第一行\n第二行"
    assert "裸换行" in method


def test_extract_json_does_not_guess_when_repair_is_uncertain():
    from dubforge.backends.llm import _extract_json
    import pytest

    with pytest.raises(ValueError, match="未找到可解析的 JSON 块"):
        _extract_json('{"lines":{"1":"截断途中')


def test_chat_json_traces_local_json_repair(monkeypatch):
    from dubforge.backends import llm

    raw = '{"lines":{"1":"一","孤立","2":"二"}}'
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: raw)
    events = []
    llm.chat_json("原始任务", trace=lambda event, content="", **meta:
                  events.append((event, content, meta)))
    assert any(event == "json_repair" and "孤立字符串" in content
               for event, content, _ in events)


def test_chat_validated_plain_protocol_does_not_request_json_mode(monkeypatch):
    """编号纯文本等协议不能继承 JSON response_format。"""
    from dubforge.backends import llm

    bodies = []

    def fake_post(provider, body, **kwargs):
        bodies.append(body)
        return "1. 一"

    monkeypatch.setattr(llm, "_post", fake_post)
    result = llm.chat_validated(
        "原文窗口", parser=lambda raw: raw.strip(),
        profile={"name": "plain", "base_url": "http://127.0.0.1:9",
                 "api_key": "x", "model": "m"})
    assert result == "1. 一"
    assert "response_format" not in bodies[0]


def test_chat_json_retry_preserves_previous_turns(monkeypatch):
    """JSON 校验重试必须把原始请求和模型上次输出带入同一对话上下文。"""
    from dubforge.backends import llm

    calls = []
    first_response = '{"lines":{"1":"一"}}'
    responses = [
        first_response,
        '{"lines":{"2":"二"}}',
    ]

    def fake_chat(prompt="", system=None, **kwargs):
        calls.append({"prompt": prompt, "system": system,
                      "messages": ([dict(m) for m in kwargs["messages"]]
                                   if kwargs.get("messages") else None)})
        return responses.pop(0)

    class Missing(ValueError):
        missing = [2]

    seen = {}

    def validator(obj):
        lines = obj["lines"]
        seen.update(lines)
        if "2" not in seen:
            raise Missing("缺少行号: 2")
        return seen

    monkeypatch.setattr(llm, "chat", fake_chat)
    result = llm.chat_json(
        "原始窗口", validator=validator, system="固定规则",
        retry_prompt=lambda error, attempt: "只输出缺失行",
        retry_request=lambda error, attempt: "缺失行窗口")
    assert result == {"1": "一", "2": "二"}
    assert calls[0]["messages"] is None
    history = calls[1]["messages"]
    assert [m["role"] for m in history] == ["system", "user", "assistant", "user"]
    assert history[1]["content"] == "原始窗口"
    assert history[2]["content"] == first_response
    assert history[3]["content"] == "缺失行窗口\n\n只输出缺失行"


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


def test_empty_base_url_gives_friendly_error(isolated_env):
    # 空/空白接口地址 → 中文友好错误, 而非 httpx 的英文 UnsupportedProtocol
    from dubforge.backends.llm import test_provider
    r = test_provider({"name": "空地址", "base_url": "   ",
                       "api_key": "x", "model": "m"})
    assert r["ok"] is False
    assert "接口地址为空" in r["error"]


def test_chat_without_enabled_profile(isolated_env):
    # 没有启用配置 → 明确指引去设置页, 而非含糊的"全部失败"
    from dubforge.backends.llm import chat
    with pytest.raises(StepFailedError) as ei:
        chat("hi")
    assert "没有已启用的 LLM 配置" in ei.value.message


def test_step_can_select_named_profile(isolated_env):
    from dubforge import config
    from dubforge.backends.llm import _profiles, describe_selection

    config.set_key("llm.profiles", [
        {"name": "快速", "enabled": True, "base_url": "http://fast", "model": "fast-model"},
        {"name": "高质量", "enabled": True, "base_url": "http://quality", "model": "quality-model"},
    ])
    assert [p["model"] for p in _profiles("高质量")] == ["quality-model"]
    assert describe_selection("快速") == "快速 / fast-model"


def test_http_429_fails_fast_and_cools_profile(isolated_env, monkeypatch):
    """429 不应每个片段重试三次；同一运行内短暂冷却，直接走降级链。"""
    from dubforge import config
    import dubforge.backends.llm as llm

    config.set_key("llm.profiles", [
        {"name": "限流A", "enabled": True, "base_url": "http://a", "model": "a"},
        {"name": "限流B", "enabled": True, "base_url": "http://b", "model": "b"},
    ])
    llm._rate_limit_until.clear()
    calls = []

    class Response:
        status_code = 429
        text = '{"error":"rate limit"}'
        headers = {}

    class Client:
        def __init__(self, base):
            self.base = base
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def post(self, *args, **kwargs):
            calls.append(self.base)
            return Response()

    monkeypatch.setattr(llm, "_client", lambda base: Client(base))
    logs = []
    with pytest.raises(StepFailedError) as ei:
        llm.chat("hello", log=logs.append)
    assert ei.value.code == "LLM_RATE_LIMITED"
    assert calls == ["http://a", "http://b"]
    assert any("立即跳过剩余重试" in line for line in logs)
    assert any("冷却" in line for line in logs)

    # 第二次请求不再撞两个 429 端点。
    calls.clear()
    with pytest.raises(StepFailedError):
        llm.chat("again", log=logs.append)
    assert calls == []


def test_streaming_chat_emits_trace_and_uses_stream_body(isolated_env, monkeypatch):
    """OpenAI 兼容 SSE 增量应及时回调，最终仍拼成完整文本。"""
    from dubforge import config
    import dubforge.backends.llm as llm

    config.set_key("llm.profiles", [
        {"name": "流式", "enabled": True, "base_url": "http://stream", "model": "m"},
    ])
    seen = []
    events = []

    class Response:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"内部推理"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"甲"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"乙"}}]}'
            yield "data: [DONE]"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, path, json, headers):
            seen.append(json)
            return Response()

    monkeypatch.setattr(llm, "_client", lambda base: Client())
    assert llm.chat("hello", trace=lambda *args, **kwargs: events.append((args, kwargs))) == "甲乙"
    assert seen[0]["stream"] is True
    assert any(args and args[0] == "assistant_delta" for args, _ in events)
    assert any(args and args[0] == "reasoning_delta" and "内部推理" in str(args[1])
               for args, _ in events)
    assert any(args and args[0] == "status" and "思考中" in str(args[1])
               for args, _ in events)
