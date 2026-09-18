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
    body = {
        "model": provider.get("model", ""),
        "messages": messages,
        "temperature": temperature if temperature is not None
        else float(config.get("llm.temperature", 0.1)),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    # 思考模式: GLM-4.5+ 支持请求级开关; 字幕翻译不需要长思考, 关闭可大幅提速
    thinking = str(config.get("llm.thinking", "auto"))
    if thinking == "disabled":
        body["thinking"] = {"type": "disabled"}
    elif thinking == "enabled":
        body["thinking"] = {"type": "enabled"}
    # 专属参数最后合并: 同名键覆盖上面的通用默认 (如该模型官方推荐 temperature:1);
    # 留空 {} 则不多发任何字段, 完全跟随服务商默认
    body.update(provider.get("params") or {})
    return body


def _post(provider: dict, body: dict, log=lambda m: None) -> str:
    """向单个 profile 发请求; 429/5xx/读超时 → 指数退避重试"""
    key = provider.get("api_key", "")
    name = provider.get("name", "default")

    # 429/5xx/读超时 → 指数退避重试 (思考型模型慢 + 限流都是常态)
    import time as _time
    last_exc: Exception | None = None
    r = None
    for attempt in range(1, 4):
        try:
            with _client(provider.get("base_url", "")) as c:
                r = c.post("/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {key}"})
            last_exc = None
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_exc = e
            if isinstance(e, httpx.ConnectError):
                raise ServiceOfflineError(
                    f"「{name}」服务不可达: {provider.get('base_url')}",
                    detail=str(e)) from e
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
        raise ServiceOfflineError("LLM 认证失败 (401)", code="LLM_AUTH",
                                  suggestion=f"请检查配置「{name}」的 api_key")
    if r.status_code == 400 and provider.get("params"):
        # 该服务商不认某个专属参数 → 自适应降级: 剥离专属参数重发一次
        log(f"⚠ 「{name}」拒绝了专属参数, 已自动剥离重试 ({r.text[:120]})")
        core = {k: v for k, v in body.items()
                if k not in (provider.get("params") or {})}
        with _client(provider.get("base_url", "")) as c:
            r = c.post("/chat/completions", json=core,
                       headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise StepFailedError(f"LLM 请求失败 HTTP {r.status_code}",
                              detail=r.text[:800],
                              suggestion="429=配额限流 (稍后重试); 其他码见 detail。")
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
    body = _payload(provider, [{"role": "user", "content": prompt}], 0.0, json_mode=False)
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
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}]
    errors = []
    for provider in _profiles():
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
