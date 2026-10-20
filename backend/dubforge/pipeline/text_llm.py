"""语义整理和翻译的请求级思考设置；不修改全局模型配置或降级顺序。

GLM-5.3 系列官方 Chat 接口支持 low/high/max；仅覆盖明确模型与官方入口，保留 thinking。
https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash
官方Codex套餐文档说明low/medium/high思考强度：
https://docs.volcengine.com/docs/ark/coding-plan-personal-ai-codex?lang=en
Agent Plan的Chat/Responses专属入口说明：
https://docs.volcengine.com/docs/ark/agent-plan-enterprise-opencode?lang=zh
2026-10-07本机隔离请求证实/api/plan/v3/chat/completions拒绝thinking参数。
Claude Code的Messages接口关闭思考说明不能推定Chat接口兼容；本模块只降低强度。
ark-code-latest是控制台路由别名，不能据此推断其他模型或代理的参数支持。
"""
from copy import deepcopy
from urllib.parse import urlsplit

from dubforge.errors import StepFailedError


_ARK_HOST = 'ark.cn-beijing.volces.com'
_ARK_PLAN_PATHS = {'/api/plan/v3', '/api/coding/v3'}
_GLM_HOST = 'open.bigmodel.cn'
_GLM_LOW_MODELS = {'glm-5.3', 'glm-5.3-flash', 'glm-5.3-flashx'}


def _low_adapter(profile: dict) -> str | None:
    """仅覆盖已核实的官方套餐入口与模型别名，不按域名后缀猜测兼容性。"""
    params = profile.get('params')
    model = params.get('model', profile.get('model')) if isinstance(params, dict) else profile.get('model')
    endpoint = profile.get('base_url')
    if not isinstance(endpoint, str):
        return None
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return None
        path = parsed.path.rstrip('/')
        if parsed.hostname == _ARK_HOST and path in _ARK_PLAN_PATHS and model == 'ark-code-latest':
            return 'ark_plan'
        if (parsed.hostname == _GLM_HOST and path in {'/api/paas/v4', '/api/coding/paas/v4'}
                and isinstance(model, str) and model.lower() in _GLM_LOW_MODELS):
            return 'glm_5_3'
        return None
    except ValueError:
        return None


def _supports_low_mode(profile: dict) -> bool:
    return _low_adapter(profile) is not None


def thinking_profiles(profiles: list[dict]) -> list[dict]:
    """仅提供思考参数摘要，不向表单发送地址、密钥或任意自定义参数。"""
    result = []
    for profile in profiles:
        params = profile.get('params', {})
        valid = isinstance(params, dict)
        params = params if valid else {}
        effort = params.get('reasoning_effort')
        thinking = params.get('thinking')
        thinking_type = thinking.get('type') if isinstance(thinking, dict) else None
        result.append(dict(
            name=str(profile.get('name') or 'default'), enabled=profile.get('enabled', True),
            supports_low=_supports_low_mode(profile), params_valid=valid,
            removes_thinking=_low_adapter(profile) == 'ark_plan',
            effort=(effort if isinstance(effort, str) and effort in
                    {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}
                    else '自定义值' if 'reasoning_effort' in params else '服务商默认'),
            has_thinking='thinking' in params,
            thinking_type=(thinking_type if isinstance(thinking_type, str) and thinking_type in
                           {'enabled', 'disabled', 'auto'} else '自定义值'),
        ))
    return result

def prepare_profiles(profiles: list[dict], mode: str = 'low', *, task: str = 'split',
                     log=lambda msg: None) -> list[dict]:
    """返回独立快照；low为低思考档位，兼容旧fast别名，不承诺速度或token用量。"""
    if task not in ('split', 'translate'):
        raise StepFailedError('文本处理步骤无效，只支持语义整理或翻译。',
                              code='TEXT_LLM_TASK_INVALID')
    label, prefix = ('语义整理', 'SPLIT') if task == 'split' else ('翻译', 'TRANSLATE')
    if mode not in ('low', 'fast', 'follow_profile'):
        raise StepFailedError(f'{label}思考模式无效，请选择“低思考强度（low）”或“跟随模型配置”。',
                              code=f'{prefix}_THINKING_MODE')
    if not isinstance(profiles, list) or any(not isinstance(p, dict) for p in profiles):
        raise StepFailedError(f'{label}模型快照格式无效，请检查模型配置组。',
                              code=f'{prefix}_LLM_PROFILE_INVALID')
    for index, profile in enumerate(profiles, 1):
        if 'params' in profile and not isinstance(profile['params'], dict):
            raise StepFailedError(f'第{index}个{label}模型的专属参数必须是对象，请检查模型配置。',
                                  code=f'{prefix}_LLM_PARAMS_INVALID')
    result = deepcopy(profiles)
    for index, profile in enumerate(result, 1):
        if mode == 'follow_profile':
            continue
        adapter = _low_adapter(profile)
        if adapter:
            params = profile.setdefault('params', {})
            changed = '；覆盖自定义 JSON 的 reasoning_effort' if 'reasoning_effort' in params else ''
            removed = '；移除本接口不兼容的 thinking 字段' if adapter == 'ark_plan' and 'thinking' in params else ''
            # 本机Chat路线已明确拒绝thinking；不把Messages接口参数混进Chat。
            if adapter == 'ark_plan':
                params.pop('thinking', None)
            params['reasoning_effort'] = 'low'
            log(f'{label}模型{index}：本步骤请求降低思考强度（low）{changed}{removed}，全局配置保持不变。')
        else:
            # 不输出连接地址或专属参数，避免错误日志意外包含认证内容。
            log(f'{label}模型{index}：未确认支持低思考强度参数，保留模型配置，仅由提示词要求尽快完成。')
    return result
