"""
LLM 后端 (OpenAI 兼容 chat.completions)
========================================
- 用 httpx 直连, 不引 openai SDK (保持依赖轻)
- chat(): 文本补全; chat_validated(): 通用协议解析/校验/重试;
  chat_json(): JSON 模式的专用入口
- 配置: config llm 段 (profiles/timeout)，步骤可选择 profile
"""
import json
import re
import threading
import uuid

import httpx

from dubforge import config
from dubforge.errors import DubForgeError, ServiceOfflineError, StepFailedError

_offline_cache: dict = {}
_offline_lock = threading.Lock()
# 同一配置连续收到 429 时，短时间内不再把每一条字幕都送去撞限流。
# 这是进程内的短暂熔断，不写入项目配置，也不会影响服务恢复后的下一轮运行。
_rate_limit_until: dict[str, float] = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_COOLDOWN_SEC = 30.0


def _profile_key(provider: dict) -> str:
    """生成不含密钥的配置身份，用于限流冷却。"""
    return "|".join(str(provider.get(k, "")) for k in ("name", "base_url", "model"))


def _rate_limit_remaining(provider: dict) -> float:
    key = _profile_key(provider)
    now = __import__("time").monotonic()
    with _rate_limit_lock:
        until = _rate_limit_until.get(key, 0.0)
        if until <= now:
            _rate_limit_until.pop(key, None)
            return 0.0
        return until - now


def _mark_rate_limited(provider: dict) -> None:
    with _rate_limit_lock:
        _rate_limit_until[_profile_key(provider)] = (
            __import__("time").monotonic() + _RATE_LIMIT_COOLDOWN_SEC)


def _client(base: str) -> httpx.Client:
    read_to = float(config.get("llm.timeout", 300))
    return httpx.Client(base_url=base.rstrip("/"),
                        timeout=httpx.Timeout(connect=10, read=read_to,
                                              write=60, pool=10),
                        trust_env=False)  # 本地代理由 Clash 规则管, 不读环境代理


def _profiles(selected: str | list[dict] | dict | None = None) -> list[dict]:
    """启用的 API 配置池 (按列表顺序; 旧版单配置自动兼容)。

    ``selected`` 也可直接传入运行开始时的配置快照（一个 dict 或列表），
    让长任务不会因为设置页被修改而在后续请求中换模型或参数。
    """
    if isinstance(selected, dict):
        return [dict(selected)]
    if isinstance(selected, (list, tuple)):
        return [dict(p) for p in selected if isinstance(p, dict)]
    cfg = config.load_config().get("llm", {})
    ps = [p for p in (cfg.get("profiles") or []) if p.get("enabled", True)]
    if not ps and cfg.get("base_url") and cfg.get("api_key"):
        ps = [{"name": "default", "base_url": cfg["base_url"],
               "api_key": cfg["api_key"], "model": cfg.get("model", "")}]
    if selected:
        wanted = str(selected).strip()
        chosen = [p for p in ps if str(p.get("name", "")).strip() == wanted]
        if chosen:
            return chosen
        raise StepFailedError(
            f"未找到指定的 LLM 配置组「{wanted}」",
            suggestion="请在设置页检查配置组名称，或把步骤里的模型配置改为“跟随全局降级链”。")
    return ps


def describe_selection(selected: str | list[dict] | dict | None = None) -> str:
    """返回可写入运行日志的模型摘要，绝不包含 API Key。"""
    items = _profiles(selected)
    if not items:
        return "无已启用配置"
    return " → ".join(
        f"{p.get('name', 'default')} / {p.get('model') or '(未填模型)'}"
        for p in items
    )


def _payload(provider: dict, messages: list[dict], temperature: float | None,
             json_mode: bool) -> dict:
    """构造请求体。temperature=None 时不发送该字段 (用服务商默认);
    每组配置想固定随机性, 在其专属参数里写 {"temperature": ..} (最后合并, 优先级最高)"""
    body = {
        "model": provider.get("model", ""),
        "messages": messages,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    # 专属参数最后合并: 同名键覆盖上面的通用默认, 也可补充厂家私有参数
    # (如 GLM 的 {"thinking": {"type": "enabled"}, "reasoning_effort": "max"})
    body.update(provider.get("params") or {})
    return body


# 常见状态码: (码义, 该场景下最可能的原因 —— 只指出问题来源, 不写修复步骤)
_STATUS_MEANING = {
    400: ("请求格式或参数有误", "可能是模型名或专属参数不被该服务商接受"),
    401: ("身份验证未通过", "可能是 api_key 不对"),
    403: ("已认证但无权访问", "可能是该 key 未开通此模型"),
    405: ("请求方法不被允许", "可能也是接口地址不对"),
    408: ("请求超时", None),
    413: ("请求体过大", None),
    422: ("参数校验失败", "可能是专属参数不符合该服务商要求"),
    429: ("请求过于频繁 (限流)", "可能是并发太高或配额用完"),
}


def _status_title(status: int, body: str = "") -> str:
    """非 200 状态码的标题: 状态码 + 码义 + 该场景下最可能的原因

    404 特殊: OpenAI 惯例把"模型不存在"也报成 404, 兼容厂商沿用,
    所以用响应内容区分是模型名错还是接口地址错。
    """
    if status == 404:
        if "model" in body.lower() or "模型" in body:
            return "LLM 请求失败 HTTP 404 —— 资源不存在（可能是模型名不对）"
        return "LLM 请求失败 HTTP 404 —— 资源不存在（可能是接口地址填错）"
    m = _STATUS_MEANING.get(status)
    if status >= 500:
        m = ("服务商服务器错误", "可能与本地配置无关")
    if not m:
        return f"LLM 请求失败 HTTP {status}"
    meaning, cause = m
    title = f"LLM 请求失败 HTTP {status} —— {meaning}"
    if cause:
        title += f"（{cause}）"
    return title


def _sleep_retry(seconds: float, cancel_check=None) -> None:
    """可取消的退避等待，避免停止后还要完整睡完重试间隔。"""
    import time as _time
    deadline = _time.monotonic() + max(0.0, seconds)
    while True:
        if cancel_check:
            cancel_check()
        left = deadline - _time.monotonic()
        if left <= 0:
            return
        _time.sleep(min(0.2, left))


def _provider_error(data, status: int = 200) -> StepFailedError | None:
    """识别结构化服务错误，不能把内容审核或额度问题误认为参数不兼容。"""
    if not isinstance(data, dict) or not data.get('error'):
        return None
    error = data['error']
    detail = error if isinstance(error, dict) else {'message': str(error)}
    code = str(detail.get('code') or detail.get('type') or '')
    message = str(detail.get('message') or '')
    signature = re.sub(r'[^a-z]', '', code.lower())
    blocked = any(word in signature for word in ('sensitivecontent', 'contentfilter', 'contentpolicy', 'safety', 'responsibleai'))
    blocked = blocked or any(word in message.lower() for word in
        ('sensitive content', 'sensitive information', 'content policy', 'content filter', '内容审核', '敏感内容'))
    if blocked:
        return StepFailedError('服务商内容审核拒绝了本次请求' + (f'（{code}）' if code else ''),
            code='LLM_CONTENT_REJECTED', detail=detail,
            suggestion='这不是输出格式错误。请查看服务商原因并核对本批评论与视频背景；程序不会改写内容规避审核。')
    return StepFailedError(f'服务商返回错误：{code or status} {message[:300]}'.strip(),
        code=f'LLM_HTTP_{status}' if status != 200 else 'LLM_PROVIDER_ERROR', detail=detail,
        suggestion='请根据服务商返回的错误码和原因检查配置或服务状态。')


def _body_error(body: str, status: int) -> StepFailedError | None:
    try:
        return _provider_error(json.loads(body), status)
    except (ValueError, TypeError):
        return None


def _unsupported_params(body: str, candidates) -> list[str]:
    """仅删除明确点名且不支持的可选参数，不能遇到任意 400 就剥离配置。"""
    text = body.lower()
    if not any(word in text for word in ('unsupported', 'not supported', 'unrecognized', 'unknown parameter',
                                         'unexpected keyword', '不支持', '未知参数')):
        return []
    return [key for key in candidates if key not in {'model', 'messages'}
            and re.search(r'(?<![\w])' + re.escape(str(key).lower()) + r'(?![\w])', text)]


def _checked_content(content, *, finish_reason=None, reasoning_chars=0, refusal=False) -> str:
    detail = {'finish_reason': finish_reason, 'reasoning_chars': reasoning_chars}
    if refusal or finish_reason in {'content_filter', 'safety', 'sensitive'}:
        raise StepFailedError('服务商拒绝生成本次内容', code='LLM_CONTENT_REJECTED', detail=detail,
            suggestion='请核对本批输入与服务商审核结果；不会将拒绝响应作为格式问题重试。')
    if finish_reason in {'length', 'max_tokens'}:
        raise StepFailedError('模型输出被长度限制截断，未返回完整结果', code='LLM_OUTPUT_TRUNCATED', detail=detail,
            suggestion='请检查模型输出 token 上限与思考预算；已截断结果不会当作完整正文使用。')
    if content is None or isinstance(content, str) and not content.strip():
        hint = '仅返回思考内容，没有最终正文' if reasoning_chars else '没有返回最终正文'
        raise StepFailedError(f'模型{hint}', code='LLM_EMPTY_RESPONSE', detail=detail,
            suggestion='这不是格式错误。请检查模型服务状态、输出 token 上限和思考参数；查看结束原因后再重试。')
    if not isinstance(content, str):
        raise StepFailedError('服务商返回了无法识别的正文结构', code='LLM_RESPONSE_INVALID', detail=detail,
            suggestion='请检查接口是否兼容文本聊天响应。')
    return content


def _content_text(value) -> str:
    """兼容字符串和文本块，不把非文本对象强转成伪正文。"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, dict) and isinstance(item.get('text'), str) for item in value):
        return ''.join(item['text'] for item in value)
    raise StepFailedError('服务商返回了无法识别的正文结构', code='LLM_RESPONSE_INVALID',
        suggestion='请检查接口是否兼容文本聊天响应。')


def _plain_content(data, *, trace=None, request_id='', name='') -> str:
    error = _provider_error(data)
    if error:
        raise error
    try:
        choice = data['choices'][0]
        message = choice['message']
        content = message.get('content')
        reasoning = message.get('reasoning_content') or message.get('reasoning') or ''
        finish = choice.get('finish_reason')
    except (TypeError, KeyError, IndexError, AttributeError) as exc:
        raise StepFailedError('服务商响应缺少 choices/message', code='LLM_RESPONSE_INVALID',
            suggestion='请检查接口响应格式或服务商状态。') from exc
    content = _content_text(content)
    if trace:
        trace('response_metadata', f'响应结束：{finish or "未提供结束原因"}', request_id=request_id,
              profile=name, finish_reason=finish, reasoning_chars=len(str(reasoning)), usage=data.get('usage'))
    return _checked_content(content, finish_reason=finish, reasoning_chars=len(str(reasoning)), refusal=bool(message.get('refusal')))


def _stream_content(response, *, name: str, request_id: str,
                    log=lambda m: None, trace=None, cancel_check=None,
                    started_at: float | None = None) -> str:
    """读取 OpenAI 兼容 SSE 流，并把增量交给实时预览回调。"""
    pieces: list[str] = []
    trace_buffer: list[str] = []
    reasoning_buffer: list[str] = []
    trace_chars = 0
    reasoning_chars = 0
    last_reasoning_report = 0
    reasoning_trace_chars = 0
    reasoning_trace_time = 0.0
    import time as _time
    reasoning_trace_time = _time.monotonic()
    trace_time = _time.monotonic()
    last_report = 0
    finish_reason, refusal, usage = None, False, None
    received = 0
    for raw_line in response.iter_lines():
        if cancel_check:
            cancel_check()
        line = str(raw_line or "").strip()
        if not line:
            continue
        data_text = line[5:].strip() if line.startswith("data:") else line
        if data_text == "[DONE]":
            break
        try:
            data = json.loads(data_text)
        except json.JSONDecodeError:
            # 兼容服务商偶尔发出的 event/keep-alive 文本。
            continue
        error = _provider_error(data)
        if error:
            raise error
        if not isinstance(data, dict):
            continue
        usage = data.get('usage') or usage
        choices = data.get("choices") or []
        if not choices:
            continue
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise StepFailedError('服务商返回了无法识别的 choices 结构', code='LLM_RESPONSE_INVALID',
                suggestion='请检查兼容接口响应格式。')
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        message = choice.get("message") or {}
        if not isinstance(delta, dict) or not isinstance(message, dict):
            raise StepFailedError('服务商返回了无法识别的 delta/message 结构', code='LLM_RESPONSE_INVALID',
                suggestion='请检查兼容接口响应格式。')
        finish_reason = choice.get('finish_reason') or finish_reason
        refusal = refusal or bool(delta.get('refusal') or message.get('refusal'))
        # 思考型模型常把内部推理放在 reasoning_content 中, 在最终 content
        # 之前可能持续很久。保存为独立的可折叠实时事件，绝不混入最终 content。
        reasoning_piece = delta.get("reasoning_content")
        if reasoning_piece is None:
            reasoning_piece = delta.get("reasoning")
        if reasoning_piece is None:
            reasoning_piece = message.get("reasoning_content")
        if reasoning_piece is None:
            reasoning_piece = message.get("reasoning")
        if reasoning_piece:
            if isinstance(reasoning_piece, list):
                reasoning_piece = "".join(
                    str(x.get("text", "")) if isinstance(x, dict) else str(x)
                    for x in reasoning_piece)
            reasoning_chars += len(str(reasoning_piece))
            if trace:
                reasoning_buffer.append(str(reasoning_piece))
                now = _time.monotonic()
                if (reasoning_chars - reasoning_trace_chars >= 100
                        or now - reasoning_trace_time >= 0.15):
                    trace("reasoning_delta", "".join(reasoning_buffer),
                          request_id=request_id, profile=name,
                          received_chars=reasoning_chars)
                    reasoning_buffer.clear()
                    reasoning_trace_chars = reasoning_chars
                    reasoning_trace_time = now
            if reasoning_chars - last_reasoning_report >= 200 or last_reasoning_report == 0:
                last_reasoning_report = reasoning_chars
                log(f"LLM 模型思考中: 已收到内部进度约 {reasoning_chars:,} 字符"
                    f"{'，已写入实时面板' if trace else ''}")
                if trace:
                    trace("status", f"模型思考中…已收到内部进度约 {reasoning_chars:,} 字符",
                          request_id=request_id, profile=name)
        piece = delta.get("content")
        if piece is None:
            piece = delta.get("text", "")
        if piece in (None, ""):
            # 有些兼容接口忽略 stream=true，仍返回一次普通 chat 响应。
            piece = message.get("content", "")
        piece = _content_text(piece)
        if not piece:
            continue
        pieces.append(piece)
        received += len(piece)
        if received == len(piece) and started_at is not None:
            log(f"LLM 首个可见 token: 配置={name}, 请求编号={request_id}, "
                f"等待 {_time.time() - started_at:.1f}s")
        if trace:
            if received == len(piece):
                trace("status", "已收到模型首字节，正在流式生成…",
                      request_id=request_id, profile=name)
            trace_buffer.append(piece)
            now = _time.monotonic()
            if received - trace_chars >= 100 or now - trace_time >= 0.15:
                trace("assistant_delta", "".join(trace_buffer), request_id=request_id,
                      profile=name, received_chars=received)
                trace_buffer.clear()
                trace_chars, trace_time = received, now
        if received - last_report >= 200:
            last_report = received
            log(f"LLM 流式接收: 配置={name}, 已收到 {received:,} 字符")
    if trace and trace_buffer:
        trace("assistant_delta", "".join(trace_buffer), request_id=request_id,
              profile=name, received_chars=sum(map(len, pieces)))
    if trace and reasoning_buffer:
        trace("reasoning_delta", "".join(reasoning_buffer), request_id=request_id,
              profile=name, received_chars=reasoning_chars)
    if trace:
        trace('response_metadata', f'响应结束：{finish_reason or "未提供结束原因"}', request_id=request_id,
              profile=name, finish_reason=finish_reason, reasoning_chars=reasoning_chars, usage=usage)
    return _checked_content(''.join(pieces), finish_reason=finish_reason,
                            reasoning_chars=reasoning_chars, refusal=refusal)


def _post(provider: dict, body: dict, log=lambda m: None,
          cancel_check=None, trace=None) -> str:
    """向单个 profile 发请求; 5xx/读超时重试, 429 立即切换配置组。

    限流不是本次请求内容的瞬时错误，继续重试只会浪费等待时间并扩大日志噪声；
    429 由 chat() 记入短暂冷却，后续字幕片段会直接跳过该配置。
    """
    key = provider.get("api_key", "")
    name = provider.get("name", "default")
    # 空地址在 httpx 里只会报英文 UnsupportedProtocol, 提前拦下给人话
    if not (provider.get("base_url") or "").strip():
        raise StepFailedError(
            f"「{name}」接口地址为空",
            suggestion="在设置页该组的「接口地址」填入服务商 API 地址后重试")
    # Key 为空 → 不带 Authorization 头 (本地 vLLM/Ollama 等无鉴权服务可裸跑;
    # 带空值反而会被 httpx 拒绝: Illegal header value b'Bearer ')
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    import time as _time
    started = _time.time()
    params = {k: v for k, v in body.items() if k not in {"model", "messages"}}
    response_format = params.pop("response_format", None)
    safe_params = {
        k: ("***" if any(word in str(k).lower()
                          for word in ("key", "token", "secret", "password")) else v)
        for k, v in params.items()
    }
    param_desc = ", ".join(f"{k}={v}" for k, v in safe_params.items()) or "服务商默认"
    format_desc = (f", 请求格式={response_format}" if response_format else "")
    input_chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
    request_id = uuid.uuid4().hex[:10]
    log(f"LLM 请求: 配置={name}, 模型={body.get('model') or '(未填)'}, "
        f"输入约 {input_chars:,} 字符, 参数={param_desc}{format_desc}, 响应=SSE流式(不支持时自动回退)")
    base_url = str(provider.get("base_url", ""))
    if "/api/plan/" in base_url.lower():
        log("⚠ 当前配置使用计划/思考端点，模型可能先进行较长内部推理，最终文本首字节会明显变慢")
    if trace:
        for message in body.get("messages", []):
            trace("request", str(message.get("content", "")),
                  request_id=request_id, role=message.get("role", "user"),
                  profile=name, model=body.get("model") or "(未填)",
                  input_chars=input_chars)
        trace("status", "等待模型首字节…", request_id=request_id,
              profile=name, model=body.get("model") or "(未填)")
    log(f"LLM 等待首字: 配置={name}, 请求编号={request_id}，服务商返回前可能需要较长时间")
    # 5xx/读超时 → 指数退避重试; 429 在上面立即切换 (避免无效等待)
    last_exc: Exception | None = None
    r = None
    content: str | None = None
    response_body = ""
    for attempt in range(1, 4):
        if cancel_check:
            cancel_check()
        try:
            with _client(provider.get("base_url", "")) as c:
                stream_body = {**body, "stream": True}
                if hasattr(c, "stream"):
                    with c.stream("POST", "/chat/completions", json=stream_body,
                                  headers=headers) as response:
                        r = response
                        if r.status_code == 200:
                            log(f"LLM 流式连接已建立: 配置={name}, 请求编号={request_id}")
                            content_type = str(r.headers.get("content-type", "")).lower()
                            if "text/event-stream" not in content_type:
                                log(f"⚠ 服务商返回的 Content-Type={content_type or '(空)'}，可能并未真正启用 SSE；"
                                    "将等完整响应后再显示模型输出")
                            if trace:
                                trace("status", "已建立流式连接，模型正在准备首个 token…",
                                      request_id=request_id, profile=name,
                                      model=body.get("model") or "(未填)")
                            if 'application/json' in content_type:
                                r.read()
                                try:
                                    data = r.json()
                                except ValueError as exc:
                                    raise StepFailedError('服务商响应不是有效的 JSON 信封', code='LLM_RESPONSE_INVALID',
                                        suggestion='请检查兼容接口是否返回标准聊天响应。') from exc
                                content = _plain_content(data, trace=trace, request_id=request_id, name=name)
                            else:
                                content = _stream_content(
                                    r, name=name, request_id=request_id, log=log,
                                    trace=trace, cancel_check=cancel_check,
                                    started_at=started)
                        else:
                            response_body = r.read().decode("utf-8", errors="replace")
                else:
                    # 测试替身或极简 HTTP 适配器可能只实现 post；保留非流式
                    # 兼容路径，不让观测能力改变既有调用契约。
                    r = c.post("/chat/completions", json=body, headers=headers)
                    response_body = str(getattr(r, "text", "") or "")
            if cancel_check:
                cancel_check()
            last_exc = None
        except httpx.TransportError as e:
            last_exc = e
            if isinstance(e, httpx.ConnectError):
                raise ServiceOfflineError(
                    f"「{name}」服务不可达: {provider.get('base_url')}",
                    detail=str(e),
                    suggestion="检查接口地址与端口是否正确, 对应服务是否已启动") from e
            if attempt < 3:
                log(f"LLM 传输异常: 配置={name}, {type(e).__name__}, "
                    f"第 {attempt}/3 次，将在 {3 * attempt:g}s 后重试")
            else:
                log(f"LLM 传输异常: 配置={name}, {type(e).__name__}, "
                    f"第 {attempt}/3 次，已达到重试上限")
            if attempt < 3:
                if trace:
                    trace("retry", "传输异常，准备重试", request_id=request_id)
                _sleep_retry(3 * attempt, cancel_check)
            continue
        if r.status_code == 429:
            log(f"LLM 响应: 配置={name}, HTTP 429 限流，本次立即跳过剩余重试")
            raise StepFailedError(
                _status_title(429, response_body), detail=response_body[:800], suggestion="",
                code="LLM_RATE_LIMITED")
        if r.status_code != 200:
            log(f"LLM 响应: 配置={name}, HTTP {r.status_code}, 第 {attempt}/3 次")
        if r.status_code == 200 or (r.status_code != 429 and r.status_code < 500):
            break
        retry_after = r.headers.get("Retry-After")
        wait = (float(retry_after)
                if (retry_after or "").replace(".", "").isdigit() else 5 * attempt)
        if attempt < 3:
            log(f"LLM 重试计划: 配置={name}, HTTP {r.status_code}, "
                f"{wait:g}s 后重试")
        else:
            log(f"LLM 重试计划: 配置={name}, HTTP {r.status_code}，已达到重试上限")
        if attempt < 3:
            if trace:
                trace("retry", "服务响应异常，准备重试", request_id=request_id)
            _sleep_retry(wait, cancel_check)
    if last_exc is not None:
        raise StepFailedError(
            f"LLM 请求超时/中断 (已重试): {type(last_exc).__name__}",
            detail=str(last_exc)[:500],
            suggestion="检查网络连接；思考型模型响应慢时可调大 llm.timeout, 或换更快模型。",
            code='LLM_TIMEOUT' if isinstance(last_exc, httpx.TimeoutException) else 'LLM_TRANSPORT')
    if r.status_code == 401:
        # 服务器已响应 → 认证信息以服务端返回为准, 不再自造建议
        raise ServiceOfflineError(
            "LLM 认证失败 (401) —— 身份验证未通过（可能是 api_key 不对）",
            code="LLM_AUTH", suggestion="")
    response_error = _body_error(response_body, r.status_code)
    if response_error and response_error.code == 'LLM_CONTENT_REJECTED':
        raise response_error
    if r.status_code == 400 and _unsupported_params(response_body, ['stream']):
        # 少数旧的 OpenAI 兼容服务不支持 stream=true；只对明确的流式参数
        # 拒绝做一次非流式兼容回退，避免把普通 400 隐藏成第二次请求。
        log(f"⚠ 「{name}」不支持流式响应，自动回退为普通响应")
        if trace:
            trace("retry", "流式请求被拒绝，回退普通响应", request_id=request_id)
        if cancel_check:
            cancel_check()
        with _client(provider.get("base_url", "")) as c:
            r = c.post("/chat/completions", json=body, headers=headers)
        if cancel_check:
            cancel_check()
        response_body = r.text
        content = None
    response_error = _body_error(response_body, r.status_code)
    if response_error and response_error.code == 'LLM_CONTENT_REJECTED':
        raise response_error
    unsupported = _unsupported_params(response_body, provider.get('params') or {})
    if r.status_code == 400 and unsupported:
        # 仅移除响应明确点名的可选项，保留核心字段和其他专属参数。
        log(f"⚠ 「{name}」不支持参数 {', '.join(unsupported)}，移除这些参数后重试一次")
        if trace:
            trace("retry", "专属参数被拒绝，移除参数后重试", request_id=request_id)
        if cancel_check:
            cancel_check()
        core = {k: v for k, v in body.items() if k not in unsupported}
        with _client(provider.get("base_url", "")) as c:
            r = c.post("/chat/completions", json=core, headers=headers)
        if cancel_check:
            cancel_check()
        response_body = r.text
    if r.status_code != 200:
        # 服务器有响应 → 错误码与原因以服务端返回为准 (detail), 本地只给
        # 状态码标题 + 码义; 显式清空 suggestion, 避免泄漏管线页默认建议
        response_error = _body_error(response_body, r.status_code)
        if response_error:
            raise response_error
        raise StepFailedError(_status_title(r.status_code, response_body),
                              code=f'LLM_HTTP_{r.status_code}',
                              detail=response_body[:800], suggestion="")
    if content is None:
        try:
            data = r.json()
        except ValueError as exc:
            raise StepFailedError('服务商响应不是有效的 JSON 信封', code='LLM_RESPONSE_INVALID',
                suggestion='请检查兼容接口是否返回标准聊天响应。') from exc
        content = _plain_content(data, trace=trace, request_id=request_id, name=name)
    if trace:
        trace("assistant_response", content, request_id=request_id,
              profile=name, model=body.get("model") or "(未填)",
              output_chars=len(content))
    log(f"LLM 完成: 配置={name}, 模型={body.get('model') or '(未填)'}, "
        f"耗时 {_time.time() - started:.1f}s, 输出 {len(content):,} 字符")
    return content


def test_provider(provider: dict) -> dict:
    """对单个配置发最小请求 (带该 profile 的专属参数), 返回测试结果 + 请求/响应详情

    request: 实际发送了什么 (url/model/temperature/专属参数/prompt);
    logs:    测试过程中的事件 (如专属参数被拒自动剥离重试); reply/detail: 收到了什么。
    """
    import time as _time
    t0 = _time.time()
    logs: list[str] = []
    prompt = "请只回复两个字母: OK"
    body = _payload(provider, [{"role": "user", "content": prompt}], None, json_mode=False)
    req_info = {"url": provider.get("base_url", "").rstrip("/") + "/chat/completions",
                "model": body.get("model"), "temperature": body.get("temperature"),
                "extra_params": provider.get("params") or {}, "prompt": prompt}
    try:
        reply = _post(provider, body, log=logs.append)
        return {"ok": True, "latency": round(_time.time() - t0, 1),
                "model": provider.get("model", ""), "reply": str(reply)[:120],
                "request": req_info, "logs": logs}
    except DubForgeError as e:
        return {"ok": False, "error": e.message, "suggestion": e.suggestion,
                "detail": str(e.detail or "")[:400], "request": req_info, "logs": logs}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "request": req_info, "logs": logs}


def check_online() -> bool:
    """连通性探测: 任何 HTTP 响应都算可达 (404/403 只说明该路径无此端点);
    只有连接层异常才算不可达。结果缓存 60s。"""
    profiles = _profiles()
    if not profiles:
        return False
    now = __import__("time").time()
    key = str(profiles[0].get("base_url", ""))
    with _offline_lock:
        hit = _offline_cache.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
    ok = False
    try:
        with _client(profiles[0].get("base_url", "")) as c:
            c.get("/models", headers={"Authorization":
                     f"Bearer {profiles[0].get('api_key', '')}"})
        ok = True                              # 收到任何 HTTP 响应 = 网络可达
    except Exception:
        ok = False
    with _offline_lock:
        _offline_cache[key] = (now, ok)
    return ok


def _post_with_heartbeat(provider: dict, body: dict, *, log=lambda m: None,
                         cancel_check=None, trace=None) -> str:
    """带静默心跳的请求: 服务商长时间不吐数据(思考不回传流、首 token 空白期)
    时, 每 4s 发一条心跳日志, 让上层进度按时间推进而不是整段冻结。

    任何真实日志都会刷新静默计时; 心跳本身不刷新。请求结束(含异常)立即停表。
    """
    import time as _time
    last_activity = [_time.monotonic()]
    stop = threading.Event()

    def _log_ts(msg: str) -> None:
        last_activity[0] = _time.monotonic()
        log(msg)

    def _watchdog() -> None:
        while not stop.wait(4.0):
            silent = _time.monotonic() - last_activity[0]
            log(f"LLM 静默等待中: 已 {silent:.0f}s 无新数据"
                "（请求仍在进行，可能是模型在思考）")

    watcher = threading.Thread(target=_watchdog, daemon=True)
    watcher.start()
    try:
        return _post(provider, body, log=_log_ts,
                     cancel_check=cancel_check, trace=trace)
    finally:
        stop.set()


def chat(prompt: str = "", system: str | None = None,
         temperature: float | None = None,
         log=lambda m: None, profile: str | None = None,
         cancel_check=None, trace=None,
         messages: list[dict] | None = None) -> str:
    """文本补全: 按启用的配置池顺序尝试, 单个失败自动切换下一个。

    messages 用于需要保留多轮上下文的重试；未提供时保持原来的单轮调用。
    """
    providers = _profiles(profile)
    if not providers:
        raise StepFailedError(
            "没有已启用的 LLM 配置",
            code='LLM_NOT_CONFIGURED',
            suggestion="在设置页至少启用一组配置并「应用设置」; "
                       "新配置需填入接口地址 / api_key / 模型名。")
    if messages is None:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}]
    else:
        messages = [dict(message) for message in messages]
    errors = []
    error_codes = []
    causes = []
    chain_started = __import__("time").monotonic()
    for provider in providers:
        remaining = _rate_limit_remaining(provider)
        if remaining > 0:
            seconds = max(1, round(remaining))
            msg = f"近期 HTTP 429 限流，冷却中（约 {seconds}s）"
            errors.append(f"「{provider.get('name')}」: {msg}")
            error_codes.append('LLM_RATE_LIMITED')
            causes.append(dict(profile=provider.get('name'), code='LLM_RATE_LIMITED', message=msg))
            log(f"⏭ LLM 配置「{provider.get('name', 'default')}」{msg}，跳过本次请求")
            continue
        try:
            return _post_with_heartbeat(
                provider, _payload(provider, messages, temperature,
                                   json_mode=False),
                log=log, cancel_check=cancel_check, trace=trace)
        except (ServiceOfflineError, StepFailedError) as e:
            errors.append(f"「{provider.get('name')}」: {e.message}")
            error_codes.append(e.code)
            causes.append(dict(profile=provider.get('name'), code=e.code, message=e.message,
                               detail=e.detail, suggestion=e.suggestion))
            if trace:
                trace('provider_error', e.message, profile=provider.get('name', ''), code=e.code,
                      detail=e.detail, suggestion=e.suggestion)
            if e.code == "LLM_RATE_LIMITED":
                _mark_rate_limited(provider)
                log(f"⚠ LLM 配置「{provider.get('name', 'default')}」触发限流，"
                    f"已冷却 {_RATE_LIMIT_COOLDOWN_SEC:.0f}s，切换下一组")
            else:
                log(f"⚠ LLM 配置「{provider.get('name', 'default')}」失败，准备切换下一组: {e.message}")
    log(f"LLM 降级链结束: {len(providers)} 组均未成功，耗时 "
        f"{__import__('time').monotonic() - chain_started:.1f}s")
    raise StepFailedError(
        "全部启用的 API 配置均失败 —— " + "；".join(errors)[:600],
        code=error_codes[0] if error_codes and len(set(error_codes)) == 1 else 'LLM_PROVIDERS_FAILED',
        detail={'providers': causes},
        suggestion=(causes[0].get('suggestion', '') if len(causes) == 1 else
                    '请查看各配置的具体错误与响应结束原因；不会把服务错误当作格式错误反复纠正。'))


def _escape_raw_control_chars(text: str) -> tuple[str, bool]:
    """JSON 字符串内的裸换行/制表符是高频小错误，可安全转义。"""
    out: list[str] = []
    changed = False
    in_string = False
    escaped = False
    replacements = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for ch in text:
        if in_string and not escaped and ch in replacements:
            out.append(replacements[ch])
            changed = True
        else:
            out.append(ch)
        if not in_string:
            if ch == '"':
                in_string = True
                escaped = False
            continue
        if escaped:
            escaped = False
        elif ch == '\\':
            escaped = True
        elif ch == '"':
            in_string = False
    return ''.join(out), changed


def _json_bracket_state(text: str) -> list[tuple[str, int]]:
    """返回尚未闭合的括号栈；扫描时跳过字符串里的括号。"""
    stack: list[tuple[str, int]] = []
    in_string = False
    escaped = False
    pairs = {')': '(', ']': '[', '}': '{'}
    for pos, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in '{[(':
            stack.append((ch, pos))
        elif ch in pairs:
            if stack and stack[-1][0] == pairs[ch]:
                stack.pop()
    return stack


def _close_missing_brackets(text: str) -> tuple[str, bool]:
    stack = _json_bracket_state(text)
    if not stack:
        return text, False
    # 常见输出被截断时缺少右括号；按嵌套反序补齐。若仍解析失败，
    # 后续候选不会采用，不会把坏数据硬塞给调用方。
    closers = {'(': ')', '[': ']', '{': '}'}
    suffix = ''.join(closers[ch] for ch, _ in reversed(stack))
    return text + suffix, True


def _json_repair_candidates(text: str) -> list[tuple[str, str]]:
    """只做确定性小修复；所有候选仍必须通过 json.loads 才会被采用。"""
    stages: list[tuple[str, str]] = []

    escaped, escaped_changed = _escape_raw_control_chars(text)
    if escaped_changed:
        stages.append((escaped, '转义字符串内裸换行/制表符'))

    # 兼容模型偶发输出的 bare numeric key：{1:"译文"}。
    quoted_keys = re.sub(r'([{,]\s*)(\d+)(\s*:)', r'\1"\2"\3', escaped)
    if quoted_keys != escaped:
        stages.append((quoted_keys, '为裸数字键补引号'))

    # 实际案例：..."326":"译文","观","327":"..."。这个孤立字符串处在
    # 对象成员之间，且后面紧跟数字键，删除它不会改变任何业务行。
    stray_pattern = r',\s*"(?:\\.|[^"\\])*"\s*(?=,\s*"\d+"\s*:)'
    for source, before in [(escaped, '转义字符串内裸换行/制表符'),
                           (quoted_keys, '为裸数字键补引号')]:
        dropped = re.sub(stray_pattern, '', source)
        if dropped != source:
            method = ('删除对象成员间的孤立字符串' if before == '转义字符串内裸换行/制表符'
                      else before + ' + 删除对象成员间的孤立字符串')
            stages.append((dropped, method))

    sources = stages[:] if stages else [(escaped, '')]
    expanded: list[tuple[str, str]] = list(stages)
    for source, method in sources:
        no_trailing_comma = re.sub(r',\s*([}\]])', r'\1', source)
        if no_trailing_comma != source:
            expanded.append((no_trailing_comma,
                             (method + ' + ' if method else '') + '删除尾逗号'))
        closed, closed_changed = _close_missing_brackets(no_trailing_comma)
        if closed_changed:
            expanded.append((closed,
                             (method + ' + ' if method else '') + '补齐缺失右括号'))
    return expanded


def _extract_json(text: str, *, return_repair: bool = False):
    """容错提取 JSON，并对确定性小错误先做本地自修复。

    找到平衡块后先走标准解析；失败时生成有限候选修复并再次标准解析。
    任何候选都必须通过 ``json.loads``，不做猜测式文本重建。
    """
    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", text,
                  flags=re.DOTALL).lstrip('\ufeff')
    # 取最早出现的顶层容器。对象里经常嵌套 lines 数组，固定先找 '['
    # 会错误截掉外层对象及其 handoff_summary。
    starts = [(text.find(opener), opener, closer)
              for opener, closer in (("[", "]"), ("{", "}"))
              if text.find(opener) >= 0]
    first_error: json.JSONDecodeError | None = None
    for start, opener, closer in sorted(starts, key=lambda item: item[0]):
        depth = 0
        closed_at: int | None = None
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    closed_at = i
                    break
        candidate = text[start:closed_at + 1] if closed_at is not None else text[start:]
        try:
            obj = json.loads(candidate)
            return (obj, '') if return_repair else obj
        except json.JSONDecodeError as e:
            first_error = first_error or e
            for repaired, method in _json_repair_candidates(candidate):
                try:
                    obj = json.loads(repaired)
                    return (obj, method) if return_repair else obj
                except json.JSONDecodeError:
                    continue

    detail = f"；原始解析错误: {first_error.msg} (char {first_error.pos})" if first_error else ""
    raise json.JSONDecodeError(f"未找到可解析的 JSON 块{detail}", text,
                               first_error.pos if first_error else 0)

def chat_validated(prompt: str, parser, validator=None, system: str | None = None,
                   max_retries: int = 3, temperature: float | None = None,
                   log=lambda m: None, profile: str | None = None,
                   cancel_check=None, trace=None, retry_prompt=None,
                   retry_request=None, retry_context=None,
                   failure_label: str = "输出"):
    """通用文本协议补全 + 解析 + 校验 + 重试。

    ``parser`` 负责把原始文本转成领域对象；返回 ``(对象, 修复说明)`` 时会
    记录一次确定性的本地修复。翻译使用严格的“行号. 译文”协议，JSON 只
    是其中一种解析器，不再把所有步骤都绑定到 JSON 模式。
    """
    last_err: Exception | None = None
    last_raw = ""
    retry_group = uuid.uuid4().hex[:10]
    conversation: list[dict] | None = None
    for attempt in range(1, max_retries + 1):
        if cancel_check:
            cancel_check()
        retry_hint = ""
        attempt_prompt = prompt
        context_mode = "conversation"
        if last_err is not None:
            # 先让调用方选择本轮上下文形态，再构造修复消息；二者可能依赖同一
            # 个判定结果（例如“少量缺行 → 独立补行请求”）。
            if callable(retry_context):
                context_mode = str(retry_context(last_err, attempt, last_raw)
                                   or "conversation")
            if callable(retry_request):
                attempt_prompt = str(retry_request(last_err, attempt) or prompt)
            if callable(retry_prompt):
                retry_hint = "\n\n" + str(retry_prompt(last_err, attempt) or "")
            else:
                retry_hint = (f"\n\n【重试提示】上一次输出解析或校验失败: {last_err}\n"
                              f"请严格按要求的输出格式重新输出，不要输出任何解释文字。")
        attempt_trace = trace
        if trace:
            def attempt_trace(event, content="", _attempt=attempt, **meta):
                trace(event, content, retry_group=retry_group,
                      retry_attempt=_attempt, retry_total=max_retries, **meta)
        user_content = attempt_prompt + retry_hint
        if conversation is None or context_mode == "fresh":
            # fresh 是一次新的无状态请求：只带稳定 system 和本轮修复输入，
            # 不把原始窗口、历史输出重复发给服务商。
            raw = chat(user_content, system=system, temperature=temperature,
                       log=log, profile=profile, cancel_check=cancel_check,
                       trace=attempt_trace)
            if conversation is None:
                conversation = ([{"role": "system", "content": system}] if system else [])
                conversation.extend([
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": raw},
                ])
        else:
            # conversation 校验重试使用真正的多轮消息：系统提示、原始窗口、
            # 模型上次输出、本轮修复要求全部保留。服务商仍会收到一次完整请求，
            # 但模型不需要猜测“上次输出”是什么。
            conversation.append({"role": "user", "content": user_content})
            raw = chat(system=None, temperature=temperature, log=log,
                       profile=profile, cancel_check=cancel_check,
                       trace=attempt_trace, messages=conversation)
            conversation.append({"role": "assistant", "content": raw})
        last_raw = raw
        if cancel_check:
            cancel_check()
        try:
            parsed = parser(raw)
            if isinstance(parsed, tuple) and len(parsed) == 2:
                obj, local_repair = parsed
            else:
                obj, local_repair = parsed, ""
            if trace and local_repair:
                trace("json_repair", f"本地自修复输出：{local_repair}")
            if validator:
                normalized = validator(obj)
                if trace and attempt > 1:
                    trace("validation_success", f"第 {attempt}/{max_retries} 次校验通过",
                          retry_group=retry_group, retry_attempt=attempt,
                          retry_total=max_retries)
                return obj if normalized is None else normalized
            if trace and attempt > 1:
                trace("validation_success", f"第 {attempt}/{max_retries} 次校验通过",
                      retry_group=retry_group, retry_attempt=attempt,
                      retry_total=max_retries)
            return obj
        except DubForgeError:
            # 取消、存储失败等业务错误不能当成模型格式错误反复调用。
            raise
        except Exception as e:  # noqa: BLE001 (解析/校验失败都要重试)
            last_err = e
            log(f"LLM {failure_label} 第 {attempt}/{max_retries} 次解析失败: {e}")
            if trace:
                trace("validation_error", str(e), retry_group=retry_group,
                      retry_attempt=attempt, retry_total=max_retries)
    raise StepFailedError(
        f"LLM 输出连续 {max_retries} 次无法通过{failure_label}校验: {last_err}",
        code='LLM_OUTPUT_INVALID',
        detail=last_raw[:1200],
        suggestion="可重试本步骤; 若反复失败, 换更强的模型 (config llm.model)。")


def chat_json(prompt: str, validator=None, system: str | None = None,
              max_retries: int = 3, temperature: float | None = None,
              log=lambda m: None, profile: str | None = None,
              cancel_check=None, trace=None, retry_prompt=None,
              retry_request=None, retry_context=None):
    """JSON 补全 + 校验 + 重试；通用 validated 调用的 JSON 专用入口。"""
    def json_parser(raw: str):
        return _extract_json(raw, return_repair=True)

    return chat_validated(
        prompt, json_parser, validator, system=system,
        max_retries=max_retries, temperature=temperature, log=log,
        profile=profile, cancel_check=cancel_check, trace=trace,
        retry_prompt=retry_prompt, retry_request=retry_request,
        retry_context=retry_context, failure_label="JSON")
