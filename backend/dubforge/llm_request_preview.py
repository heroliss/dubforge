"""复用真实请求构造的本地预览与参数校验；不读取密钥、不调用服务商。"""
from copy import deepcopy
import json
import re

from dubforge import config
from dubforge.errors import ConfigError


_CREDENTIAL_VALUE = re.compile(r'(?:\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{10,}|'
                               r'(?<![A-Za-z0-9])[0-9a-fA-F]{32}\.[A-Za-z0-9]{8}(?![A-Za-z0-9]))')


def validate_profile_params(profile: dict) -> None:
    if not isinstance(profile, dict):
        raise ConfigError('LLM 配置组必须是 JSON 对象。', code='LLM_PROFILE_INVALID')
    params = profile.get('params', {})
    if not isinstance(params, dict):
        raise ConfigError('LLM 专属参数必须是 JSON 对象，例如 {}；不能使用数组、字符串或空值。',
                          code='LLM_PARAMS_INVALID')
    try:
        json.dumps(params, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ConfigError('LLM 专属参数必须是有效的 JSON，数字不能是无穷值或 NaN。',
                          code='LLM_PARAMS_INVALID') from exc
    for key in ('stream', 'tool_stream'):
        if key in params and not isinstance(params[key], bool):
            raise ConfigError(f'LLM 参数 {key} 必须是 true 或 false。', code='LLM_PARAMS_INVALID')


def validate_profiles(profiles) -> None:
    if not isinstance(profiles, list):
        raise ConfigError('LLM 配置组列表必须是数组。', code='LLM_PROFILES_INVALID')
    for profile in profiles:
        validate_profile_params(profile)


def validate_settings(updates: dict) -> None:
    if 'llm.profiles' in updates:
        validate_profiles(updates['llm.profiles'])
    llm = updates.get('llm')
    if isinstance(llm, dict) and 'profiles' in llm:
        validate_profiles(llm['profiles'])


def _redact(value, path: str, redacted: list[str]):
    """仅识别认证字段，不能把 max_tokens 等普通参数当密钥。"""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            label = re.sub(r'[^a-z0-9]', '', str(key).lower())
            item_path = f'{path}.{key}' if path else str(key)
            if (label in {'passwd', 'token'}
                    or label.endswith(('apikey', 'authorization', 'password', 'secret',
                                       'accesstoken', 'refreshtoken', 'bearertoken', 'credentials'))):
                result[key] = '***'
                redacted.append(item_path)
            else:
                result[key] = _redact(item, item_path, redacted)
        return result
    if isinstance(value, list):
        return [_redact(item, f'{path}[{index}]', redacted) for index, item in enumerate(value)]
    if isinstance(value, str) and _CREDENTIAL_VALUE.search(value):
        redacted.append(path)
        return _CREDENTIAL_VALUE.sub('***', value)
    return value


def preview_request(profile: dict, *, mode: str = 'profile', thinking_mode: str | None = None) -> dict:
    from dubforge.backends.llm import _payload, _transport_payload
    from dubforge.pipeline.text_llm import prepare_profiles

    if mode not in ('profile', 'split', 'translate'):
        raise ConfigError('请求预览只支持模型配置、语义整理或翻译。', code='LLM_PREVIEW_MODE_INVALID')
    if thinking_mode is not None and thinking_mode not in ('low', 'follow_profile'):
        raise ConfigError('请求预览的思考模式只支持 low 或 follow_profile。',
                          code='LLM_PREVIEW_THINKING_INVALID')
    validate_profile_params(profile)
    for field in ('name', 'model', 'base_url'):
        if field in profile and not isinstance(profile[field], str):
            raise ConfigError(f'LLM 配置的 {field} 必须是文本。', code='LLM_PROFILE_INVALID')
    # 只接收构造所需字段，不读取保存的配置组或 API Key。
    provider = {field: deepcopy(profile[field]) for field in ('name', 'model', 'base_url', 'params')
                if field in profile}
    provider.setdefault('params', {})
    messages = [
        {'role': 'system', 'content': '<运行时生成的任务规则；这里仅为占位>'},
        {'role': 'user', 'content': '<运行时生成的正文、上下文及输出要求；这里仅为占位>'},
    ]
    notes = ['本地预览不会调用大模型，也不会保存设置。',
             'messages 为占位内容；真实任务会生成正文、上下文及重试消息。',
             '未填写的 temperature、top_p、reasoning_effort 等参数不会发送，使用服务商默认值。']
    before = _payload(provider, messages, None, json_mode=False)
    if mode != 'profile':
        selected_mode = thinking_mode if thinking_mode is not None else config.get(f'{mode}.thinking_mode', 'low')
        provider = prepare_profiles([provider], selected_mode, task=mode, log=notes.append)[0]
    configured = _payload(provider, messages, None, json_mode=False)
    body = _transport_payload(configured)
    if configured.get('stream') is False:
        notes.append('stream=false：使用普通响应；tool_stream、stream_options 仅用于流式请求，本次不发送。')
    else:
        notes.append('本次使用流式响应；未填写 stream 时程序自动发送 true，服务商明确不支持时回退普通响应。')
    if 'tool_stream' in body:
        notes.append('本任务没有调用工具；tool_stream 不会让普通字幕或评论文字流式输出得更快。')
    if 'model' in provider['params']:
        notes.append('专属参数中的 model 覆盖了配置组的模型名称。')
    if 'messages' in provider['params']:
        notes.append('专属参数中的 messages 覆盖了任务消息；实际任务也会使用该覆盖值，请谨慎核对。')
    changes = []
    for key in sorted(before.keys() | body.keys()):
        if key in before and key in body and before[key] == body[key]:
            continue
        change = {'path': key, 'kind': 'remove' if key not in body else 'add' if key not in before else 'override'}
        if key in before:
            change['before'] = before[key]
        if key in body:
            change['after'] = body[key]
        changes.append(change)
    redacted = []
    safe_body = _redact(body, 'body', redacted)
    safe_changes = _redact(changes, 'changes', redacted)
    if redacted:
        notes.append('预览中的认证字段已脱敏；HTTP 鉴权头不属于请求 JSON，不会显示。')
    return dict(ok=True, body=safe_body, notes=notes, changes=safe_changes,
                redacted_paths=redacted)
