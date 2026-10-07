"""全文语义整理的原始响应缓存；不接受或改写模型候选。"""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dubforge.errors import StepFailedError
from dubforge.utils.fs import atomic_write_bytes

_CACHE_KEY = re.compile(r"[0-9a-f]{64}")
_PRIVATE = {"key", "token", "auth", "bearer", "credential", "credentials", "password", "passwd",
            "secret", "cookie", "cookies", "header", "headers", "extraheaders", "requestheaders", "sig"}


def _private_key(key):
    name = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return (name in _PRIVATE or any(part in name for part in ("apikey", "authorization", "authentication", "credential",
                                                            "password", "secret", "cookie", "headers", "signature"))
            or name.endswith(("accesstoken", "refreshtoken", "bearertoken", "authtoken", "apitoken", "privatekey", "accesskey")))


def _public_parameters(value):
    """保留未知供应商的制作参数；递归剔除连接凭据，max_tokens等预算仍参与指纹。"""
    if isinstance(value, dict):
        return {str(key): _public_parameters(item) for key, item in value.items() if not _private_key(key)}
    if isinstance(value, (list, tuple)):
        return [_public_parameters(item) for item in value]
    return value


def _public_endpoint(value):
    """保留非敏感路由参数，轮换URL凭据不会使窗口缓存过期。"""
    if not isinstance(value, str):
        return value
    try:
        parsed = urlsplit(value)
        query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if not _private_key(key)]
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path, urlencode(query), ""))
    except ValueError:
        # 无效连接地址不能生成可复用的有效响应，也不能把未解析的认证串参与快照。
        return "<无效模型地址>"


def _public_profile(profile):
    if isinstance(profile, (list, tuple)):
        return [_public_profile(item) for item in profile]
    if not isinstance(profile, dict):
        return profile
    result = {key: profile[key] for key in ("name", "model", "temperature", "temp") if key in profile}
    if "base_url" in profile:
        result["base_url"] = _public_endpoint(profile["base_url"])
    if "params" in profile:
        result["params"] = _public_parameters(profile["params"])
    return result


def cache_key(prompt, profile, source, version, settings):
    """原文、实际提示、模型制作参数、规则及词锚点均由调用方冻结后绑定。"""
    payload = dict(prompt=prompt, profile=_public_profile(profile), source=source, version=version, settings=settings)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cache_path(ctx, key):
    if not isinstance(key, str) or not _CACHE_KEY.fullmatch(key):
        raise ValueError("语义整理缓存标识无效")
    return Path(ctx.pdir) / "cache" / "split" / f"{key}.json"


def read_cache(ctx, key):
    """缓存仅供重新校验；损坏、缺失或无法读取时重新请求，不登记任何产物。"""
    try:
        return json.loads(_cache_path(ctx, key).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def write_cache(ctx, key, value):
    """仅写原始响应；取消和存储失败不能伪装为模型质量失败。"""
    ctx.check_pause()
    ctx.check_cancel()
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    ctx.check_cancel()
    try:
        atomic_write_bytes(_cache_path(ctx, key), data)
    except (OSError, ValueError) as error:
        raise StepFailedError("语义整理缓存写入失败，请检查工作空间是否可写后重试", code="SPLIT_CACHE_WRITE") from error
