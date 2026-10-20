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
from contextlib import contextmanager

import httpx

from dubforge import config
from dubforge import llm_conversations
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


def _client(base: str):
    read_to = float(config.get("llm.timeout", 300))
    from dubforge.backends.llm_transport import CancellableClient
    return CancellableClient(base_url=base.rstrip("/"),
                        timeout=httpx.Timeout(connect=10, read=read_to,
                                              write=60, pool=10),
                        trust_env=False)  # 本地代理由 Clash 规则管, 不读环境代理


def _request_client(base: str, cancel_check):
    """将步骤取消检查绑定到网络等待；兼容现有测试传输替身。"""
    from dubforge.backends.llm_transport import CancellableClient
    client = _client(base)
    if isinstance(client, CancellableClient):
        client.cancel_check = cancel_check
    return client


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


def _transport_payload(body: dict, *, stream: bool | None = None) -> dict:
    """传输层与设置预览共用最终请求体；非流式回退不能重发流式选项。"""
    result = dict(body)
    if stream is None:
        stream = body.get('stream') is not False
        if not stream:
            for key in ('stream_options', 'tool_stream'):
                result.pop(key, None)
            return result
    if stream:
        result['stream'] = True
    else:
        for key in ('stream', 'stream_options', 'tool_stream'):
            result.pop(key, None)
    return result


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


_ACCOUNT_ERROR_CODES = {'LLM_QUOTA_EXHAUSTED', 'LLM_SUBSCRIPTION_INVALID'}


def _provider_error_detail(data) -> dict | None:
    """只读取服务商错误信封，不递归扫描正常正文或模型输出。"""
    if not isinstance(data, dict):
        return None
    error = data.get('error') or data.get('Error')
    for key in ('ResponseMetadata', 'Response'):
        envelope = data.get(key)
        if not error and isinstance(envelope, dict):
            error = envelope.get('Error') or envelope.get('error')
    if error:
        return error if isinstance(error, dict) else {'message': str(error)}
    # 部分兼容接口把错误码与消息直接放在顶层；成功码与聊天正文不算错误。
    code = data.get('code', data.get('Code'))
    if 'choices' not in data and code is not None and str(code).lower() not in {'', '0', '200', 'ok', 'success'}:
        return data
    return None


def _provider_error(data, status: int = 200) -> StepFailedError | None:
    """识别结构化服务错误，不能把内容审核或额度问题误认为参数不兼容。"""
    detail = _provider_error_detail(data)
    if detail is None:
        return None
    code = str(detail.get('code') or detail.get('Code') or detail.get('type') or detail.get('Type') or '')
    message = str(detail.get('message') or detail.get('Message') or detail.get('msg') or '')
    signature = re.sub(r'[^a-z]', '', code.lower())
    blocked = any(word in signature for word in ('sensitivecontent', 'contentfilter', 'contentpolicy', 'safety', 'responsibleai'))
    blocked = blocked or any(word in message.lower() for word in
        ('sensitive content', 'sensitive information', 'content policy', 'content filter', '内容审核', '敏感内容'))
    # 部分兼容服务把明确审核拒绝放在消息中，type/code 仍是普通 invalid_request_error。
    # 只接受该业务原因本身或消息开头的明确短语，不能把任意 HTTP 400/参数错误归为单条内容失败。
    blocked = blocked or signature == 'contentexistsrisk' or bool(re.match(
        r'^\s*content\s+exists\s+risk(?:\s*$|\s*[.:：;；,，。(\[])', message, re.IGNORECASE))
    if blocked:
        return StepFailedError('服务商内容审核拒绝了本次请求' + (f'（{code}）' if code else ''),
            code='LLM_CONTENT_REJECTED', detail=detail,
            suggestion='这不是输出格式错误。请查看服务商原因并核对本批评论与视频背景；程序不会改写内容规避审核。')
    # HTTP 429 同时可能表示临时限流和账户资源耗尽，必须优先看明确的业务错误。
    # 不凭 quota/exceeded 等宽泛词推断余额，避免把每分钟额度误报为欠费。
    account_code = re.sub(r'[^a-z0-9]', '', code.lower())
    quota_empty = account_code in {'1113', 'insufficientquota', 'insufficientbalance',
        'balanceinsufficient', 'creditsexhausted', 'outofcredits', 'billinghardlimitreached'}
    quota_empty = quota_empty or any(word in message for word in
        ('余额不足', '无可用资源包', '资源包耗尽', '资源包已用完', '账户欠费'))
    quota_empty = quota_empty or bool(re.search(
        r'\binsufficient\s+(?:balance|credits?|funds)\b|\b(?:credits?|balance)\s+(?:is\s+|are\s+)?(?:exhausted|depleted)\b',
        message, re.IGNORECASE))
    if quota_empty:
        return StepFailedError('模型服务余额不足或可用资源已耗尽', code='LLM_QUOTA_EXHAUSTED', detail=detail,
            suggestion='请在服务商控制台补充余额或资源包，或在「模型与服务」改用可用配置，再检查并继续任务。')
    subscription_invalid = account_code in {'invalidsubscription', 'subscriptionexpired',
        'subscriptionnotfound', 'noactivesubscription'}
    subscription_invalid = subscription_invalid or any(word in message for word in
        ('订阅已过期', '订阅已失效', '未开通订阅', '无有效订阅'))
    subscription_invalid = subscription_invalid or bool(re.search(
        r'\bsubscription\s+(?:has\s+|is\s+)?(?:expired|invalid)\b|\b(?:no|not\s+have\s+(?:a\s+)?)\s*valid\s+[^.\n]{0,40}\bsubscription\b',
        message, re.IGNORECASE))
    if subscription_invalid:
        return StepFailedError('模型服务订阅未开通或已失效', code='LLM_SUBSCRIPTION_INVALID', detail=detail,
            suggestion='请在服务商控制台核对订阅与接口权限，或在「模型与服务」改用可用配置，再检查并继续任务。')
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
            suggestion='请检查模型响应 token 上限和请求总时限；已截断结果不会当作完整正文使用。')
    if content is None or isinstance(content, str) and not content.strip():
        hint = '仅返回思考内容，没有最终正文' if reasoning_chars else '没有返回最终正文'
        raise StepFailedError(f'模型{hint}', code='LLM_EMPTY_RESPONSE', detail=detail,
            suggestion='这不是格式错误。请检查模型服务状态、输出 token 上限和思考参数；查看结束原因后再重试。')
    if not isinstance(content, str):
        raise StepFailedError('服务商返回了无法识别的正文结构', code='LLM_RESPONSE_INVALID', detail=detail,
            suggestion='请检查接口是否兼容文本聊天响应。')
    return content


def _text_channels(value) -> tuple[str, str]:
    """按块类型分离正文和思考；未知对象不能强转成伪正文。"""
    if value is None:
        return '', ''
    if isinstance(value, str):
        return value, ''
    if isinstance(value, list):
        content, reasoning = [], []
        for item in value:
            if not isinstance(item, dict):
                break
            kind = item.get('type')
            if kind is not None and not isinstance(kind, str):
                break
            text = item.get('text')
            if kind in {'reasoning', 'thinking'}:
                text = text if text is not None else item.get('thinking')
                target = reasoning
            elif kind in {None, 'text', 'output_text'}:
                target = content
            else:
                break
            if not isinstance(text, str):
                break
            target.append(text)
        else:
            return ''.join(content), ''.join(reasoning)
    raise StepFailedError('服务商返回了无法识别的正文结构', code='LLM_RESPONSE_INVALID',
        suggestion='请检查接口是否兼容文本聊天响应。')


def _content_text(value) -> str:
    return _text_channels(value)[0]


def _message_channels(message, *, delta=False):
    value = message.get('content')
    if value is None and delta:
        value = message.get('text')
    content, block_reasoning = _text_channels(value)
    reasoning = message.get('reasoning_content')
    if reasoning is None:
        reasoning = message.get('reasoning')
    thinking_text, thinking_blocks = _text_channels(reasoning)
    return content, block_reasoning + thinking_text + thinking_blocks


def _select_choice(choices):
    """单个文本请求只消费候选0，不能把交错候选拼成一份台词。"""
    if not isinstance(choices, list) or any(not isinstance(item, dict) for item in choices):
        raise StepFailedError('服务商返回了无法识别的 choices 结构', code='LLM_RESPONSE_INVALID')
    if not choices:
        return None
    if all('index' not in item for item in choices):
        if len(choices) == 1:
            return choices[0]
        raise StepFailedError('服务商返回多个未编号的候选，无法安全选择正文', code='LLM_RESPONSE_INVALID')
    indices = [item.get('index') for item in choices]
    if (any(not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in indices)
            or len(set(indices)) != len(indices)):
        raise StepFailedError('服务商返回了无法识别的候选编号', code='LLM_RESPONSE_INVALID')
    return next((item for item in choices if item['index'] == 0), None)


_GUARD_RETRY_CODES = {'LLM_OUTPUT_CONTRACT', 'LLM_OUTPUT_DEGENERATED', 'LLM_OUTPUT_BUDGET'}
_GUARD_ABORT_CODES = _GUARD_RETRY_CODES | {'LLM_REQUEST_DEADLINE'}


def _response_check(cancel_check, guard):
    if guard is None:
        return cancel_check
    def check():
        if cancel_check:
            cancel_check()
        guard.check()
    return check


@contextmanager
def _guarded_client(base, cancel_check, response_guard_factory, *, trace, request_id, name):
    """每次真实请求有独立守卫；包含响应头等待及连接关闭。"""
    guard = response_guard_factory() if response_guard_factory else None
    check = _response_check(cancel_check, guard)
    try:
        if check:
            check()
        with _request_client(base, check) as client:
            yield client, guard, check
    except StepFailedError as error:
        if error.code in _GUARD_ABORT_CODES and trace:
            trace('request_aborted', error.message, request_id=request_id, profile=name,
                  code=error.code, detail=error.detail, server_confirmed=False)
        raise


def _guard_feed(guard, piece, *, channel='content', prefix=''):
    if guard is None or not piece:
        return
    try:
        guard.feed(piece, channel=channel)
    except StepFailedError as error:
        if error.code in _GUARD_ABORT_CODES:
            detail = dict(error.detail) if isinstance(error.detail, dict) else {}
            detail['content_excerpt'] = (prefix + piece)[:240] if channel == 'content' else prefix[:240]
            detail['channel'] = channel
            error.detail = detail
        raise


def _plain_content(data, *, trace=None, request_id='', name='', guard=None) -> str:
    error = _provider_error(data)
    if error:
        raise error
    try:
        choice = _select_choice(data['choices'])
        message = choice['message']
        content, reasoning = _message_channels(message)
        finish = choice.get('finish_reason')
    except (TypeError, KeyError, IndexError, AttributeError) as exc:
        raise StepFailedError('服务商响应缺少 choices/message', code='LLM_RESPONSE_INVALID',
            suggestion='请检查接口响应格式或服务商状态。') from exc
    if finish is not None and not isinstance(finish, str):
        raise StepFailedError('服务商返回了无效的结束原因', code='LLM_RESPONSE_INVALID')
    if trace:
        trace('response_metadata', f'响应结束：{finish or "未提供结束原因"}', request_id=request_id,
              profile=name, finish_reason=finish, reasoning_chars=len(reasoning), usage=data.get('usage'),
              stream_mode='plain', selected_choice=choice.get('index', 0), choice_count=len(data['choices']))
    content = _checked_content(content, finish_reason=finish, reasoning_chars=len(reasoning), refusal=bool(message.get('refusal')))
    if guard:
        guard.check()
    _guard_feed(guard, reasoning, channel='reasoning')
    _guard_feed(guard, content)
    if trace and reasoning:
        trace('reasoning_delta', reasoning, request_id=request_id, profile=name, received_chars=len(reasoning))
    if guard:
        guard.finish()
    return content


def _plain_response(response, **options):
    try:
        data = response.json()
    except ValueError as exc:
        raise StepFailedError('服务商响应不是有效的 JSON 信封', code='LLM_RESPONSE_INVALID',
            suggestion='请检查兼容接口是否返回标准聊天响应。') from exc
    return _plain_content(data, **options)


class _StreamChannel:
    """delta是真增量；message是全量快照，只接受相同或前缀扩展。"""
    def __init__(self):
        self.pieces = []
        self.chars = 0
        self.prefix = ''

    def text(self):
        return ''.join(self.pieces)

    def snapshot_increment(self, snapshot):
        previous = self.text()
        if not snapshot.startswith(previous):
            raise StepFailedError('服务商流式消息快照与已收到内容不一致', code='LLM_RESPONSE_INVALID',
                suggestion='未猜测去重或改写正文，请检查接口流协议。')
        return snapshot[len(previous):]

    def append(self, piece):
        if piece:
            self.pieces.append(piece)
            self.chars += len(piece)
            if len(self.prefix) < 240:
                self.prefix = (self.prefix + piece)[:240]


def _stream_content(response, *, name: str, request_id: str,
                    log=lambda m: None, trace=None, cancel_check=None,
                    started_at: float | None = None, guard=None) -> str:
    """归一单候选SSE协议后再向日志和响应守卫交付真实增量。"""
    import time as _time
    channels = {'content': _StreamChannel(), 'reasoning': _StreamChannel()}
    buffers = {'content': [], 'reasoning': []}
    flushed = {'content': 0, 'reasoning': 0}
    times = {'content': _time.monotonic(), 'reasoning': _time.monotonic()}
    reports = {'content': 0, 'reasoning': 0}
    finish_reason, refusal, usage, done = None, False, None, False
    delta_frames = message_frames = choice_count = 0
    check = _response_check(cancel_check, guard)

    def deliver(piece, channel):
        if not piece:
            return
        if finish_reason is not None:
            raise StepFailedError('服务商在响应结束后继续返回内容', code='LLM_RESPONSE_INVALID')
        state = channels[channel]
        _guard_feed(guard, piece, channel=channel, prefix=channels['content'].prefix)
        state.append(piece)
        if channel == 'content' and state.chars == len(piece):
            if started_at is not None:
                log(f'LLM 首个可见 token: 配置={name}, 请求编号={request_id}, 等待 {_time.time() - started_at:.1f}s')
            if trace:
                trace('status', '已收到模型首字节，正在流式生成…', request_id=request_id, profile=name)
        if trace:
            buffers[channel].append(piece)
        now = _time.monotonic()
        if trace and (state.chars - flushed[channel] >= 100 or now - times[channel] >= .15):
            trace('assistant_delta' if channel == 'content' else 'reasoning_delta', ''.join(buffers[channel]),
                  request_id=request_id, profile=name, received_chars=state.chars)
            buffers[channel].clear()
            flushed[channel], times[channel] = state.chars, now
        if state.chars - reports[channel] >= 200 or channel == 'reasoning' and reports[channel] == 0:
            reports[channel] = state.chars
            if channel == 'reasoning':
                log(f'LLM 模型思考中: 已收到内部进度约 {state.chars:,} 字符' + ('，已写入实时面板' if trace else ''))
                if trace:
                    trace('status', f'模型思考中…已收到内部进度约 {state.chars:,} 字符', request_id=request_id, profile=name)
            else:
                log(f'LLM 流式接收: 配置={name}, 已收到 {state.chars:,} 字符')

    try:
        for raw_line in response.iter_lines():
            if check:
                check()
            line = str(raw_line or '').strip()
            if not line or line.startswith((':', 'event:', 'id:', 'retry:')):
                continue
            data_text = line[5:].strip() if line.startswith('data:') else line
            if data_text == '[DONE]':
                done = True
                break
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError as exc:
                if line.startswith('data:'):
                    raise StepFailedError('服务商返回了无效的SSE数据', code='LLM_RESPONSE_INVALID') from exc
                continue
            error = _provider_error(data)
            if error:
                raise error
            if not isinstance(data, dict):
                raise StepFailedError('服务商返回了无效的SSE信封', code='LLM_RESPONSE_INVALID')
            usage = data.get('usage') or usage
            choices = data.get('choices') or []
            if isinstance(choices, list):
                choice_count = max(choice_count, len(choices))
            choice = _select_choice(choices)
            if choice is None:
                continue
            delta, message = choice.get('delta') or {}, choice.get('message') or {}
            if not isinstance(delta, dict) or not isinstance(message, dict):
                raise StepFailedError('服务商返回了无法识别的 delta/message 结构', code='LLM_RESPONSE_INVALID')
            delta_frames += bool(delta)
            message_frames += bool(message)
            refusal = refusal or bool(delta.get('refusal') or message.get('refusal'))
            delta_content, delta_reasoning = _message_channels(delta, delta=True)
            message_content, message_reasoning = _message_channels(message)
            for channel, increment, snapshot in (
                    ('reasoning', delta_reasoning, message_reasoning),
                    ('content', delta_content, message_content)):
                deliver(increment, channel)
                if snapshot:
                    deliver(channels[channel].snapshot_increment(snapshot), channel)
            next_finish = choice.get('finish_reason')
            if next_finish is not None:
                if not isinstance(next_finish, str):
                    raise StepFailedError('服务商返回了无效的结束原因', code='LLM_RESPONSE_INVALID')
                if finish_reason is not None and finish_reason != next_finish:
                    raise StepFailedError('服务商返回了冲突的响应结束原因', code='LLM_RESPONSE_INVALID')
                finish_reason = next_finish
                if next_finish in {'length', 'max_tokens', 'content_filter', 'safety', 'sensitive'}:
                    _checked_content(channels['content'].text(), finish_reason=finish_reason,
                                     reasoning_chars=channels['reasoning'].chars, refusal=refusal)
        if check:
            check()
        if not done and finish_reason is None:
            raise StepFailedError('服务商流式连接提前结束，未确认完整响应', code='LLM_RESPONSE_INCOMPLETE',
                suggestion='未将已收到的片段当作完整正文使用，请检查服务或重试。')
        content = _checked_content(channels['content'].text(), finish_reason=finish_reason,
                                   reasoning_chars=channels['reasoning'].chars, refusal=refusal)
        if guard:
            guard.finish()
        return content
    finally:
        if trace:
            for channel in ('content', 'reasoning'):
                if buffers[channel]:
                    trace('assistant_delta' if channel == 'content' else 'reasoning_delta', ''.join(buffers[channel]),
                          request_id=request_id, profile=name, received_chars=channels[channel].chars)
            mode = 'mixed' if delta_frames and message_frames else 'message' if message_frames else 'delta'
            trace('response_metadata', f'响应结束：{finish_reason or "未提供结束原因"}', request_id=request_id,
                  profile=name, finish_reason=finish_reason, reasoning_chars=channels['reasoning'].chars,
                  content_chars=channels['content'].chars, usage=usage, stream_mode=mode,
                  delta_frames=delta_frames, message_frames=message_frames,
                  choice_count=choice_count, selected_choice=0, done_received=done,
                  terminal_received=finish_reason is not None)


def _post(provider: dict, body: dict, log=lambda m: None,
           cancel_check=None, trace=None, response_guard_factory=None) -> str:
    """向单个 profile 发请求；一般 5xx/读超时重试，账户故障或 429 立即切换。

    限流不是本次请求内容的瞬时错误，继续重试只会浪费等待时间并扩大日志噪声；
    临时 429 由 chat() 记入短暂冷却，余额与订阅错误保留各自原因。
    """
    llm_conversations.protect(provider)
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
    # 扩展参数允许嵌套 JSON；清洗仅用于日志，实际请求仍发送原始参数。
    secrets = llm_conversations.collect_secrets(provider)
    safe_params = llm_conversations.redact(params, secrets=secrets)
    safe_format = llm_conversations.redact(response_format, secrets=secrets)
    param_desc = ", ".join(f"{k}={v}" for k, v in safe_params.items()) or "服务商默认"
    format_desc = (f", 请求格式={safe_format}" if response_format else "")
    input_chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
    request_id = uuid.uuid4().hex[:10]
    streaming = body.get('stream') is not False
    first_body = _transport_payload(body)
    response_desc = 'SSE流式(不支持时自动回退)' if streaming else '普通响应(stream=false)'
    log(f"LLM 请求: 配置={name}, 模型={body.get('model') or '(未填)'}, "
        f"输入约 {input_chars:,} 字符, 参数={param_desc}{format_desc}, 响应={response_desc}")
    base_url = str(provider.get("base_url", ""))
    if "/api/plan/" in base_url.lower():
        log("⚠ 当前配置使用计划/思考端点，模型可能先进行较长内部推理，最终文本首字节会明显变慢")
    if trace:
        for message in body.get("messages", []):
            trace("request", str(message.get("content", "")),
                  request_id=request_id, role=message.get("role", "user"),
                  profile=name, model=body.get("model") or "(未填)",
                  input_chars=input_chars)
        trace("status", "等待模型首字节…" if streaming else "等待模型完整响应…", request_id=request_id,
              profile=name, model=body.get("model") or "(未填)")
    wait_label = '首字' if streaming else '完整响应'
    log(f"LLM 等待{wait_label}: 配置={name}, 请求编号={request_id}，服务商返回前可能需要较长时间")
    # 5xx/读超时 → 指数退避重试; 429 在上面立即切换 (避免无效等待)
    last_exc: Exception | None = None
    r = None
    content: str | None = None
    response_body = ""
    for attempt in range(1, 4):
        if cancel_check:
            cancel_check()
        try:
            with _guarded_client(provider.get('base_url', ''), cancel_check, response_guard_factory,
                                 trace=trace, request_id=request_id, name=name) as (c, guard, check):
                if streaming and hasattr(c, "stream"):
                    with c.stream("POST", "/chat/completions", json=first_body,
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
                                content = _plain_content(data, trace=trace, request_id=request_id, name=name, guard=guard)
                            else:
                                content = _stream_content(
                                    r, name=name, request_id=request_id, log=log,
                                    trace=trace, cancel_check=cancel_check,
                                    started_at=started, guard=guard)
                        else:
                            response_body = r.read().decode("utf-8", errors="replace")
                else:
                    # 测试替身或极简 HTTP 适配器可能只实现 post；保留非流式
                    # 兼容路径，不让观测能力改变既有调用契约。
                    request_body = _transport_payload(body, stream=False) if streaming else first_body
                    r = c.post("/chat/completions", json=request_body, headers=headers)
                    response_body = str(getattr(r, "text", "") or "")
                    if check:
                        check()
                    if r.status_code == 200:
                        content = _plain_response(r, trace=trace, request_id=request_id, name=name, guard=guard)
            if cancel_check:
                cancel_check()
            last_exc = None
        except httpx.TransportError as e:
            if cancel_check:
                cancel_check()
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
        response_error = _body_error(response_body, r.status_code)
        if response_error and response_error.code in _ACCOUNT_ERROR_CODES:
            # 余额或订阅需要用户处理；重复请求与临时限流冷却都无法修复。
            raise response_error
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
    if response_error and response_error.code in {'LLM_CONTENT_REJECTED', *_ACCOUNT_ERROR_CODES}:
        raise response_error
    if streaming and r.status_code == 400 and _unsupported_params(response_body, ['stream']):
        # 少数旧的 OpenAI 兼容服务不支持 stream=true；只对明确的流式参数
        # 拒绝做一次非流式兼容回退，避免把普通 400 隐藏成第二次请求。
        log(f"⚠ 「{name}」不支持流式响应，自动回退为普通响应")
        if trace:
            trace("retry", "流式请求被拒绝，回退普通响应", request_id=request_id)
        if cancel_check:
            cancel_check()
        with _guarded_client(provider.get('base_url', ''), cancel_check, response_guard_factory,
                             trace=trace, request_id=request_id, name=name) as (c, guard, check):
            r = c.post("/chat/completions", json=_transport_payload(body, stream=False), headers=headers)
            if check:
                check()
            content = (_plain_response(r, trace=trace, request_id=request_id, name=name, guard=guard)
                       if r.status_code == 200 else None)
        if cancel_check:
            cancel_check()
        response_body = r.text
    response_error = _body_error(response_body, r.status_code)
    if response_error and response_error.code in {'LLM_CONTENT_REJECTED', *_ACCOUNT_ERROR_CODES}:
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
        with _guarded_client(provider.get('base_url', ''), cancel_check, response_guard_factory,
                             trace=trace, request_id=request_id, name=name) as (c, guard, check):
            r = c.post("/chat/completions", json=_transport_payload(core, stream=False), headers=headers)
            if check:
                check()
            content = (_plain_response(r, trace=trace, request_id=request_id, name=name, guard=guard)
                       if r.status_code == 200 else None)
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


@llm_conversations.capture
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
        reply = _post(provider, body, log=logs.append, trace=llm_conversations.trace)
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
                          cancel_check=None, trace=None, response_guard_factory=None) -> str:
    """带静默心跳的请求: 服务商长时间不吐数据(思考不回传流、首 token 空白期)
    时, 每 4s 发一条心跳日志, 让上层进度按时间推进而不是整段冻结。

    真实日志和流式正文/思考都会刷新静默计时；有新数据时不输出静默心跳。
    心跳本身不刷新计时，请求结束（含异常）立即停表。
    """
    import time as _time
    last_activity = [_time.monotonic()]
    stop = threading.Event()
    active_request = ['']

    def _log_ts(msg: str) -> None:
        last_activity[0] = _time.monotonic()
        log(msg)

    def _trace(event, content='', **meta):
        if event in {'assistant_delta', 'reasoning_delta', 'assistant_response', 'response_metadata'}:
            last_activity[0] = _time.monotonic()
        if meta.get('request_id'):
            active_request[0] = meta['request_id']
        if trace:
            trace(event, content, **meta)

    def _watchdog() -> None:
        while not stop.wait(4.0):
            silent = _time.monotonic() - last_activity[0]
            if silent < 4.0:
                continue
            log(f"LLM 静默等待中: 已 {silent:.0f}s 无新数据"
                "（请求仍在进行，可能是模型在思考）")

    watcher = threading.Thread(target=_watchdog, daemon=True)
    watcher.start()
    try:
        return _post(provider, body, log=_log_ts,
                      cancel_check=cancel_check, trace=_trace if trace else None,
                      **({'response_guard_factory': response_guard_factory} if response_guard_factory else {}))
    except DubForgeError as error:
        if error.code in {'STEP_CANCELLED', 'STEP_TIMEOUT'}:
            notice = 'LLM 本地请求已取消，连接已关闭；未收到服务端停止计算确认。'
            log(notice)
            if trace:
                trace('request_cancelled', notice, request_id=active_request[0], server_confirmed=False)
        raise
    finally:
        stop.set()
        watcher.join(timeout=.5)


@llm_conversations.capture
def chat(prompt: str = "", system: str | None = None,
         temperature: float | None = None,
         log=lambda m: None, profile: str | None = None,
         cancel_check=None, trace=None,
          messages: list[dict] | None = None, response_guard_factory=None) -> str:
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
                log=log, cancel_check=cancel_check, trace=trace,
                **({'response_guard_factory': response_guard_factory} if response_guard_factory else {}))
        except (ServiceOfflineError, StepFailedError) as e:
            if e.code in _GUARD_ABORT_CODES:
                # 响应契约/退化只能在调用方同一重试预算内处理；不能暗中换模型重算。
                raise
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

@llm_conversations.capture
def chat_validated(prompt: str, parser, validator=None, system: str | None = None,
                   max_retries: int = 3, temperature: float | None = None,
                   log=lambda m: None, profile: str | None = None,
                   cancel_check=None, trace=None, retry_prompt=None,
                   retry_request=None, retry_context=None,
                   retry_profile=None,
                   failure_label: str = "输出", response_guard_factory=None):
    """通用文本协议补全 + 解析 + 校验 + 重试。

    ``parser`` 负责把原始文本转成领域对象；返回 ``(对象, 修复说明)`` 时会
    记录一次确定性的本地修复。翻译使用严格的“行号. 译文”协议，JSON 只
    是其中一种解析器，不再把所有步骤都绑定到 JSON 模式。
    """
    last_err: Exception | None = None
    last_raw = ""
    guard_validation_failures = 0
    retry_group = uuid.uuid4().hex[:10]
    conversation: list[dict] | None = None
    active_profile = profile
    for attempt in range(1, max_retries + 1):
        if cancel_check:
            cancel_check()
        retry_hint = ""
        attempt_prompt = prompt
        context_mode = "conversation"
        guard_retry = isinstance(last_err, StepFailedError) and last_err.code in _GUARD_RETRY_CODES
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
            if guard_retry:
                # 调用方先重置局部补行/编号契约；异常流始终重新建对话，不携带退化候选。
                context_mode = 'fresh'
            if callable(retry_profile):
                selected_profile = retry_profile(last_err, attempt, active_profile)
                if selected_profile is not None:
                    # 响应正文超过本地预算通常是确定性的：同一配置再次发送
                    # 同一请求不会改变结果，只会重复等待并制造相同错误。
                    # 允许调用方先切换到另一组快照；若快照没有变化，
                    # 直接保留原错误交给上层，避免无界的预算错误重试。
                    if (getattr(last_err, 'code', '') == 'LLM_OUTPUT_BUDGET'
                            and selected_profile == active_profile):
                        detail = dict(last_err.detail) if isinstance(last_err.detail, dict) else {}
                        detail.update(validation_attempts=guard_validation_failures,
                                      guard_validation_failures=guard_validation_failures)
                        last_err.detail = detail
                        last_err.guard_validation_failures = guard_validation_failures
                        log(f'LLM {failure_label} 已达到响应正文预算；配置未变化，停止重复请求')
                        raise last_err
                    active_profile = selected_profile
        attempt_trace = trace
        if trace:
            def attempt_trace(event, content="", _attempt=attempt, **meta):
                trace(event, content, retry_group=retry_group,
                      retry_attempt=_attempt, retry_total=max_retries, **meta)
        user_content = attempt_prompt + retry_hint
        guard_options = {'response_guard_factory': response_guard_factory} if response_guard_factory else {}
        try:
            if conversation is None or context_mode == "fresh":
                # fresh 是一次新的无状态请求：只带稳定 system 和本轮修复输入，
                # 不把原始窗口、历史输出重复发给服务商。
                raw = chat(user_content, system=system, temperature=temperature,
                           log=log, profile=active_profile, cancel_check=cancel_check,
                           trace=attempt_trace, **guard_options)
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
                raw = chat(conversation[-1]["content"], system=None, temperature=temperature, log=log,
                           profile=active_profile, cancel_check=cancel_check,
                           trace=attempt_trace, messages=conversation, **guard_options)
                conversation.append({"role": "assistant", "content": raw})
        except StepFailedError as error:
            if error.code not in _GUARD_RETRY_CODES:
                raise
            guard_validation_failures += 1
            last_err, last_raw, conversation = error, '', None
            log(f'LLM {failure_label} 第 {attempt}/{max_retries} 次响应已中止: {error.message}')
            if trace:
                trace('validation_error', error.message[:300], retry_group=retry_group,
                      retry_attempt=attempt, retry_total=max_retries, code=error.code)
            if attempt == max_retries:
                error.detail = {**(error.detail if isinstance(error.detail, dict) else {}),
                                'validation_attempts': max_retries,
                                'guard_validation_failures': guard_validation_failures}
                error.guard_validation_failures = guard_validation_failures
                raise
            continue
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
                if trace:
                    trace("validation_success", f"第 {attempt}/{max_retries} 次校验通过",
                          retry_group=retry_group, retry_attempt=attempt,
                          retry_total=max_retries)
                return obj if normalized is None else normalized
            if trace:
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
    failure = StepFailedError(
        f"LLM 输出连续 {max_retries} 次无法通过{failure_label}校验: {last_err}",
        code='LLM_OUTPUT_INVALID',
        detail=last_raw[:1200],
        suggestion="可重试本步骤; 若反复失败, 换更强的模型 (config llm.model)。")
    failure.guard_validation_failures = guard_validation_failures
    raise failure


def chat_json(prompt: str, validator=None, system: str | None = None,
              max_retries: int = 3, temperature: float | None = None,
              log=lambda m: None, profile: str | None = None,
              cancel_check=None, trace=None, retry_prompt=None,
              retry_request=None, retry_context=None, response_guard_factory=None):
    """JSON 补全 + 校验 + 重试；通用 validated 调用的 JSON 专用入口。"""
    def json_parser(raw: str):
        return _extract_json(raw, return_repair=True)

    return chat_validated(
        prompt, json_parser, validator, system=system,
        max_retries=max_retries, temperature=temperature, log=log,
        profile=profile, cancel_check=cancel_check, trace=trace,
        retry_prompt=retry_prompt, retry_request=retry_request,
        retry_context=retry_context, failure_label="JSON", response_guard_factory=response_guard_factory)
