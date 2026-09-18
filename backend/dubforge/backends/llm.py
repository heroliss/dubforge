# -*- coding: utf-8 -*-
"""
LLM 后端 (OpenAI 兼容 chat.completions)
========================================
- 用 httpx 直连, 不引 openai SDK (保持依赖轻)
- chat(): 文本补全; chat_json(): JSON 模式 + 容错提取 + 校验重试
- 配置: config llm 段 (base_url/api_key/model/temperature/max_workers)
"""
import json
import re
import threading

import httpx

from dubforge import config
from dubforge.errors import DubForgeError, ServiceOfflineError, StepFailedError

_offline_cache: dict = {}
_offline_lock = threading.Lock()


def _client(base: str) -> httpx.Client:
    read_to = float(config.get("llm.timeout", 300))
    return httpx.Client(base_url=base.rstrip("/"),
                        timeout=httpx.Timeout(connect=10, read=read_to,
                                              write=60, pool=10),
                        trust_env=False)  # 本地代理由 Clash 规则管, 不读环境代理


def _profiles() -> list[dict]:
    """启用的 API 配置池 (按列表顺序; 旧版单配置自动兼容)"""
    cfg = config.load_config().get("llm", {})
    ps = [p for p in (cfg.get("profiles") or []) if p.get("enabled", True)]
    if not ps and cfg.get("base_url") and cfg.get("api_key"):
        ps = [{"name": "default", "base_url": cfg["base_url"],
               "api_key": cfg["api_key"], "model": cfg.get("model", "")}]
    return ps


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


def _post(provider: dict, body: dict, log=lambda m: None) -> str:
    """向单个 profile 发请求; 429/5xx/读超时 → 指数退避重试"""
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

    # 429/5xx/读超时 → 指数退避重试 (思考型模型慢 + 限流都是常态)
    import time as _time
    last_exc: Exception | None = None
    r = None
    for attempt in range(1, 4):
        try:
            with _client(provider.get("base_url", "")) as c:
                r = c.post("/chat/completions", json=body, headers=headers)
            last_exc = None
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_exc = e
            if isinstance(e, httpx.ConnectError):
                raise ServiceOfflineError(
                    f"「{name}」服务不可达: {provider.get('base_url')}",
                    detail=str(e),
                    suggestion="检查接口地址与端口是否正确, 对应服务是否已启动") from e
            _time.sleep(3 * attempt)
            continue
        if r.status_code == 200 or (r.status_code != 429 and r.status_code < 500):
            break
        retry_after = r.headers.get("Retry-After")
        wait = (float(retry_after)
                if (retry_after or "").replace(".", "").isdigit() else 5 * attempt)
        _time.sleep(wait)
    if last_exc is not None:
        raise StepFailedError(
            f"LLM 请求超时/中断 (已重试): {type(last_exc).__name__}",
            detail=str(last_exc)[:500],
            suggestion="思考型模型响应慢时可调大 llm.timeout, 或换更快模型。")
    if r.status_code == 401:
        # 服务器已响应 → 认证信息以服务端返回为准, 不再自造建议
        raise ServiceOfflineError(
            "LLM 认证失败 (401) —— 身份验证未通过（可能是 api_key 不对）",
            code="LLM_AUTH", suggestion="")
    if r.status_code == 400 and provider.get("params"):
        # 该服务商不认某个专属参数 → 自适应降级: 剥离专属参数重发一次
        log(f"⚠ 「{name}」拒绝了专属参数, 已自动剥离重试 ({r.text[:120]})")
        core = {k: v for k, v in body.items()
                if k not in (provider.get("params") or {})}
        with _client(provider.get("base_url", "")) as c:
            r = c.post("/chat/completions", json=core, headers=headers)
    if r.status_code != 200:
        # 服务器有响应 → 错误码与原因以服务端返回为准 (detail), 本地只给
        # 状态码标题 + 码义; 显式清空 suggestion, 避免泄漏管线页默认建议
        raise StepFailedError(_status_title(r.status_code, r.text),
                              detail=r.text[:800], suggestion="")
    data = r.json()
    return data["choices"][0]["message"]["content"]


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


def chat(prompt: str, system: str | None = None,
         temperature: float | None = None,
         log=lambda m: None) -> str:
    """单轮文本补全: 按启用的配置池顺序尝试, 单个失败自动切换下一个"""
    providers = _profiles()
    if not providers:
        raise StepFailedError(
            "没有已启用的 LLM 配置",
            suggestion="在设置页至少启用一组配置并「应用设置」; "
                       "新配置需填入接口地址 / api_key / 模型名。")
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}]
    errors = []
    for provider in providers:
        try:
            return _post(provider, _payload(provider, messages, temperature,
                                            json_mode=False), log=log)
        except (ServiceOfflineError, StepFailedError) as e:
            errors.append(f"「{provider.get('name')}」: {e.message}")
    raise StepFailedError(
        "全部启用的 API 配置均失败 —— " + "；".join(errors)[:600],
        suggestion="在设置页用「测试连接」逐个排查; 常见: 密钥无效/资源包不足/地址错误。")


def _extract_json(text: str):
    """容错提取 JSON: 剥 markdown 围栏 → 找首个 [...] 或 {...} 平衡块"""
    text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", text, flags=re.S)
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise json.JSONDecodeError("未找到平衡的 JSON 块", text, 0)


def chat_json(prompt: str, validator=None, system: str | None = None,
              max_retries: int = 3, temperature: float | None = None,
              log=lambda m: None):
    """JSON 补全 + 校验 + 重试 (失败重试时附加纠错提示)

    validator: callable(obj) -> 归一化结果 (通过, 返回值作为 chat_json 的返回)
               或 raise ValueError(原因); 返回 None 则原样返回解析对象
    """
    last_err: Exception | None = None
    last_raw = ""
    for attempt in range(1, max_retries + 1):
        retry_hint = ""
        if last_err is not None:
            retry_hint = (f"\n\n【重试提示】上一次输出解析或校验失败: {last_err}\n"
                          f"请严格按要求的 JSON 结构重新输出, 不要输出任何解释文字。")
        raw = chat(prompt + retry_hint, system=system, temperature=temperature,
                   log=log)
        last_raw = raw
        try:
            obj = _extract_json(raw)
            if validator:
                normalized = validator(obj)
                return obj if normalized is None else normalized
            return obj
        except Exception as e:  # noqa: BLE001 (解析/校验失败都要重试)
            last_err = e
            log(f"LLM JSON 第 {attempt}/{max_retries} 次解析失败: {e}")
    raise StepFailedError(
        f"LLM 输出连续 {max_retries} 次无法通过 JSON 校验: {last_err}",
        detail=last_raw[:1200],
        suggestion="可重试本步骤; 若反复失败, 换更强的模型 (config llm.model)。")
