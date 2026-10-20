"""已有配音的待应用修改；物理文件、可试听状态与当前制作意图分开。"""
from pathlib import Path

from dubforge.pipeline import dubbing_edit, line_settings, pronunciation


def _reference_changed(get, recorded):
    from dubforge.pipeline.step_tts import REFERENCE_SNAPSHOT_KEY
    refs = recorded.get(REFERENCE_SNAPSHOT_KEY, {})
    refs = refs if isinstance(refs, dict) else {}
    for part in ('timbre', 'emo'):
        mode = get(f'tts.indextts25.{part}_mode')
        if mode != recorded.get(f'{part}_mode'):
            return True
        if mode == 'stable' and get(f'tts.indextts25.{part}_reference_line_id', 1) != recorded.get(f'{part}_reference_line_id', 1):
            return True
        if mode != 'preset':
            continue
        path = get(f'tts.indextts25.{part}_audio', '')
        if path != recorded.get(f'{part}_audio', ''):
            return True
        ref = refs.get(part, {})
        if isinstance(ref, dict) and ref.get('file_version') and ref['file_version'] != dubbing_edit.file_version(Path(path)):
            return True
    return False


def view(project, line, root, take, *, dependencies, snapshot, version, valid, current_fingerprint):
    """不修改声音或满意记录；旧文字失效同样可提示待应用，不能因不可试听漏标。"""
    physical = version != '0' and not version.endswith('-0')
    get = line_settings.effective_config(project, line, dependencies.cfg)
    independent = (get('tts.backend', 'indextts25') == 'indextts25'
                   and bool(get(pronunciation.PATH, '')))
    saved = snapshot.get('fingerprint')
    changed = bool(saved and saved != current_fingerprint)
    settings = snapshot.get('settings')
    # 新版按实际朗读输入计算。旧自定义朗读的摘要仅在严格回算一致时兼容，
    # 不能因算法规范化让无变化的历史声音显示待重做。
    if changed and valid and (independent or isinstance(settings, dict) and settings.get('pronunciation_text')):
        from dubforge.pipeline.step_tts import line_cache_valid
        changed = not line_cache_valid(project, line, root, dependencies=dependencies, take=take)
    pending = bool(physical and (not valid or changed))
    reasons = []
    if pending:
        before = dubbing_edit.recorded_reading(project, line, snapshot)
        after = dubbing_edit.current_reading(project, line, dependencies=dependencies, cfg_get=get)
        if before is not None and before != after:
            reasons.append('朗读文字已修改')
        previous = dubbing_edit.recorded_line(project, line, snapshot)
        if previous and previous.get('voice_timing', [previous['start'], previous['end']]) != line.get('voice_timing', [line['start'], line['end']]):
            reasons.append('句子时间已修改')
        if snapshot and snapshot.get('version') != version:
            reasons.append('配音文件已变化')
        if changed and get('tts.backend', 'indextts25') == 'indextts25' and isinstance(settings, dict) and _reference_changed(get, settings):
            reasons.append('参考音频设置或文件已变化')
        if not reasons:
            reasons.append('配音设置已修改' if valid else '文字或配音输入已变化')
    return dict(file_exists=physical, changes_pending=pending, change_reasons=reasons,
                reading_caption_independent=independent, settings_changed=bool(valid and changed))
