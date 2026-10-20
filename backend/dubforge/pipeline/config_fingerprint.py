"""成品有效性与运行合同分离：换模型不使已完成文字失效。"""
import copy
import hashlib
import json
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEP_MAP


TEXT_EXECUTION_FIELDS = {
    'split': {'llm_profile', 'thinking_mode', 'llm_context_tokens',
              'llm_chunk_target_words', 'llm_chunk_overlap_words', 'llm_chunk_max_retries'},
    'translate': {'llm_profile', 'thinking_mode', 'max_context_tokens', 'max_window_lines'},
}

# 普通继承配置中的执行选项；与共享连接参数的继承规则不同。
EXECUTION_ONLY_PATHS = frozenset({'tts.placement.auto_repair'})


def _runtime_paths():
    from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
    return set(RUNTIME_PARAM_PATHS)


def _remove_path(values, path):
    parts = path.split('.')
    current = values
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def snapshot(deps, step, *, keys=None, legacy=False, execution=False):
    """默认核对内容要求；运行合同另计模型和容量，保持已确认流程的边界。"""
    keys = STEP_MAP[step].cfg_keys if keys is None else keys
    result = {key: copy.deepcopy(value) for key in keys if (value := deps.cfg(key)) is not None}
    if step == 'assemble' and isinstance(result.get('assemble'), dict):
        values = result['assemble']
        # 旧规范模式也须忽略新软件缺省字段，否则无法识别升级前的真实旧指纹。
        if values.get('video_encoder', 'software') == 'software':
            values.pop('video_encoder', None)
            values.pop('nvenc_cq', None)
        elif values.get('video_encoder') == 'nvenc':
            from dubforge.pipeline.video_encoding import NVENC_PROFILE
            values.pop('crf', None)
            values['encoder_profile'] = NVENC_PROFILE
    if step == 'translate':
        from dubforge.domain import crud
        from dubforge.translation_context import signature
        context = signature(crud.project_params(deps.project))
        if context:
            # 兼容旧配置表示也必须计入新采用的输入，不能升级旧指纹时跳过背景。
            result['translation_context'] = context
    if legacy:
        return result
    value = deps.cfg('split.use_llm', True)
    use_llm = True if value is None else bool(value)
    if execution and 'llm' in result and isinstance(result['llm'], dict):
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
            for key in ('llm_profile', 'llm_context_tokens', 'thinking_mode', 'target_word_count',
                        'llm_chunk_target_words', 'llm_chunk_overlap_words', 'llm_chunk_max_retries'):
                result['split'].pop(key, None)
            result.pop('asr.pause_threshold_seconds', None)
    if step in ('split', 'translate') and isinstance(result.get(step), dict):
        # fast是早期命名的兼容别名，实际参数与low相同。
        if result[step].get('thinking_mode') == 'fast':
            result[step]['thinking_mode'] = 'low'
    if step == 'transcribe' and isinstance(result.get('asr'), dict):
        result['asr'].pop('segmentation_mode', None)
    if step == 'assemble' and isinstance(result.get('assemble'), dict):
        values = result['assemble']
        # 关闭的新后期功能不改变历史声音，也不应仅因默认字段补齐令成片过期。
        if values.get('final_loudness_mode', 'off') == 'off':
            for name in ('final_loudness_mode', 'final_loudness_lufs', 'final_true_peak_db'):
                values.pop(name, None)
        if not values.get('sentence_leveling', False):
            for name in ('sentence_leveling', 'sentence_leveling_max_db'):
                values.pop(name, None)
    if step == 'tts' and isinstance(result.get('tts'), dict):
        for backend in ('indextts25', 'gptsovits'):
            if isinstance(result['tts'].get(backend), dict):
                result['tts'][backend].pop('project_dir', None)
                result['tts'][backend].pop('request_timeout_seconds', None)
        indextts = result['tts'].get('indextts25')
        if isinstance(indextts, dict):
            for part in ('timbre', 'emo'):
                if result['tts'].get('backend') != 'indextts25' or indextts.get(f'{part}_mode') != 'stable':
                    indextts.pop(f'{part}_reference_line_id', None)
                else:
                    indextts.setdefault(f'{part}_reference_line_id', 1)
            # 默认 5 秒与旧实现的已落盘音频兼容；只有用户改动上限时
            # 才让句级缓存失效，避免升级后整部视频无意义地过期。
            try:
                if float(indextts.get('dynamic_reference_max_seconds', 5.0)) == 5.0:
                    indextts.pop('dynamic_reference_max_seconds', None)
            except (TypeError, ValueError, OverflowError):
                pass
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
            for name in ('mute_audio', 'audio_mode'):
                result['content_review'].pop(name, None)
        elif step == 'comments':
            for name in ('mute_audio', 'audio_mode', 'blocked_ranges'):
                result['content_review'].pop(name, None)
        elif step == 'assemble':
            result['content_review'].pop('audio_mode', None)
            from dubforge.pipeline.content_redaction import audio_fingerprint
            if (audio := audio_fingerprint(deps)) is not None:
                result['content_review.audio_processing'] = audio
    if not execution:
        # 自动修复控制下一轮执行，已有音频与成片是确定输入，开关不令它们过期。
        _remove_path(result, 'tts.placement.auto_repair')
        if isinstance(result.get('tts.placement'), dict):
            result['tts.placement'].pop('auto_repair', None)
        # 已经生成的文字是确定输入；更换执行工具不等于更改这些文字。
        # 恢复响应缓存仍绑定实际模型，主动重新生成也会绕过缓存。
        result.pop('llm', None)
        if isinstance(result.get(step), dict):
            for field in TEXT_EXECUTION_FIELDS.get(step, ()):
                result[step].pop(field, None)
        for path in _runtime_paths():
            _remove_path(result, path)
    return result


def _compatible_success_snapshot(deps, key):
    """只有完整成功历史能证明仅执行策略变化；未知旧 SHA 不猜测有效。"""
    from dubforge.pipeline import stale_explanation as history
    previous, _ = history._history(deps, key, None)
    if not previous or previous['scope'] != 'full':
        return False
    current = history.capture(deps, key)
    ignored = {'llm.profile.'}
    execution = {key + '.' + field for field in TEXT_EXECUTION_FIELDS.get(key, ())} | _runtime_paths()

    def values(entries):
        result = {}
        for name, entry in entries.items():
            if name in execution or any(name.startswith(prefix) for prefix in ignored):
                continue
            if not history._valid_key(name):
                return None
            safe = history._safe_previous(name, entry)
            if not safe or 'value' not in safe:
                return None
            result[name] = safe['value']
        return result

    before, after = values(previous['entries']), values(current['entries'])
    if before is None or not after or before != after:
        return False
    # 内容要求已变化时无需读取大产物；只有候选兼容历史才继续完整核验文件。
    from dubforge.domain import crud
    from dubforge.pipeline.recovery import RecoveryDependencies
    verified = RecoveryDependencies(deps.project, settings=deps.settings, overrides=deps.overrides)
    return (crud.step_artifacts_exist(deps.project.id, key, verify_hash=True)
            and all(verified.file_matches(producer, name) for producer, name, _ in deps.inputs(key)))


def promote_compatible_fingerprints():
    """严格旧 SHA 或完整成功历史证明内容要求未变时，兼容升级文字指纹。"""
    from dubforge.domain import crud
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import compute_fingerprint
    changed = 0
    for project in crud.list_projects():
        deps = Dependencies(project)
        updated = False
        for key, step in deps.steps.items():
            if step.status != 'done' or not step.input_fingerprint:
                continue
            current = compute_fingerprint(project, key, dependencies=deps)
            if current == step.input_fingerprint:
                continue
            # 路径型早期术语缓存没有内容摘要；只有新成功快照才可证明文件一致。
            modes = [{'legacy_execution': True}]
            if key in {'split', 'translate'} and not (key == 'translate' and deps.cfg('translate.custom_terms_path')):
                modes += [{'legacy_config': True}, {'legacy': True, 'legacy_config': True},
                          {'legacy': True, 'legacy_execution': True}]
            old = []
            for mode in modes:
                try:
                    old.append(compute_fingerprint(project, key, dependencies=deps, **mode))
                except (DubForgeError, ValueError, TypeError):
                    # 新执行配置无效不影响已有文字；仍可凭成功内容历史核对。
                    continue
            if step.input_fingerprint in old or _compatible_success_snapshot(deps, key):
                from dubforge.pipeline.stale_explanation import capture, _history
                previous, _ = _history(deps, key, None)
                step.input_fingerprint = current
                # 同步成功历史的标识与非模型条目，后续真实内容变化仍可准确说明。
                if previous:
                    updated_snapshot = capture(deps, key)
                    updated_snapshot['captured_at'] = previous.get('captured_at', updated_snapshot['captured_at'])
                    step.input_snapshot = json.dumps(updated_snapshot, ensure_ascii=False)
                crud.save_step(step)
                changed += 1
                updated = True
        if updated:
            crud.write_manifest(project.uid)
    return changed
