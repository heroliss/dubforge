"""制作缓存只跟随实际消费的参数，连接凭据和等待策略不改变已有文字。"""
import copy
import hashlib
from pathlib import Path

from dubforge.pipeline.registry import STEP_MAP


def snapshot(deps, step, *, keys=None, legacy=False):
    """统一供产物指纹和工作流边界使用；不保存密钥、超时或未启用的模型。"""
    keys = STEP_MAP[step].cfg_keys if keys is None else keys
    result = {key: copy.deepcopy(value) for key in keys if (value := deps.cfg(key)) is not None}
    if legacy:
        return result
    value = deps.cfg('split.use_llm', True)
    use_llm = True if value is None else bool(value)
    if 'llm' in result and isinstance(result['llm'], dict):
        llm = result['llm']
        profiles = [p for p in llm.get('profiles', []) or []
                    if isinstance(p, dict) and p.get('enabled', True)]
        if not profiles and llm.get('base_url'):
            profiles = [dict(name='default', base_url=llm['base_url'], model=llm.get('model', ''))]
        selected = str(deps.cfg(f'{step}.llm_profile', '') or '').strip()
        if selected:
            profiles = [p for p in profiles if str(p.get('name', '')).strip() == selected]
        if step == 'translate' or step == 'split' and use_llm:
            # 快速整理覆盖了思考参数；无效的全局思考强度变化不使整理稿过期。
            from dubforge.pipeline.text_llm import prepare_profiles
            profiles = prepare_profiles(profiles, deps.cfg(f'{step}.thinking_mode', 'low'), task=step)
        # 名称仅用于选组；实际模型、服务地址、请求参数及降级顺序决定生成结果。
        result['llm'] = {'profiles': [{k: copy.deepcopy(v) for k, v in p.items()
                                     if k not in {'api_key', 'timeout', 'enabled', 'name'}} for p in profiles]}
        if step == 'split' and not use_llm:
            result.pop('llm')
    if step == 'split' and isinstance(result.get('split'), dict):
        # 全文整理不消费旧分窗配置，历史项目保留这些键也不影响有效性。
        for key in ('context_lines', 'window_max_chars', 'context_gap', 'allow_cross_line_merge'):
            result['split'].pop(key, None)
        if use_llm:
            # 字符限制仅本地规则消费；语义模式改它不应让已有结果过期。
            result['split'].pop('max_split_length', None)
        if not use_llm:
            for key in ('llm_profile', 'llm_context_tokens', 'thinking_mode', 'target_word_count'):
                result['split'].pop(key, None)
            result.pop('asr.pause_threshold_seconds', None)
    if step in ('split', 'translate') and isinstance(result.get(step), dict):
        # fast是早期命名的兼容别名，实际参数与low相同。
        if result[step].get('thinking_mode') == 'fast':
            result[step]['thinking_mode'] = 'low'
    if step == 'transcribe' and isinstance(result.get('asr'), dict):
        result['asr'].pop('segmentation_mode', None)
    if step == 'tts' and isinstance(result.get('tts'), dict):
        for backend in ('indextts25', 'gptsovits'):
            if isinstance(result['tts'].get(backend), dict):
                result['tts'][backend].pop('project_dir', None)
                result['tts'][backend].pop('request_timeout_seconds', None)
    if step == 'translate' and deps.cfg('translate.custom_terms_path'):
        path = Path(deps.cfg('translate.custom_terms_path'))
        try:
            with path.open('rb') as stream:
                result['custom_terms_file'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError:
            result['custom_terms_file'] = 'unavailable'
    if 'content_review' in result:
        if not deps.cfg('content_review.enabled', True):
            result['content_review'] = {'enabled': False}
        elif step == 'subtitles':
            result['content_review'].pop('mute_audio', None)
        elif step == 'comments':
            for name in ('mute_audio', 'blocked_ranges'):
                result['content_review'].pop(name, None)
    return result


def promote_compatible_fingerprints():
    """启动时仅迁移当前设置严格匹配的旧指纹，过期/失败/运行中结果保持原状。"""
    from dubforge.domain import crud
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import compute_fingerprint
    changed = 0
    for project in crud.list_projects():
        deps = Dependencies(project)
        updated = False
        for key, step in deps.steps.items():
            if step.status != 'done' or not step.input_fingerprint or 'llm' not in STEP_MAP[key].cfg_keys:
                continue
            # 旧术语表缓存未保存文件摘要，不能猜测同路径文件是否曾被修改。
            if key == 'translate' and deps.cfg('translate.custom_terms_path'):
                continue
            current = compute_fingerprint(project, key, dependencies=deps)
            if current == step.input_fingerprint:
                continue
            old = [compute_fingerprint(project, key, dependencies=deps, legacy_config=True),
                   compute_fingerprint(project, key, dependencies=deps, legacy=True, legacy_config=True)]
            if step.input_fingerprint in old:
                step.input_fingerprint = current
                crud.save_step(step)
                changed += 1
                updated = True
        if updated:
            crud.write_manifest(project.uid)
    return changed
