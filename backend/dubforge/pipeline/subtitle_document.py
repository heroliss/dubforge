"""字幕与配音共用的校对稿：覆盖原始产物，保存时不反向改写模型输出。"""
import hashlib
import json
import math

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.text_source import source_lines, translated_lines

FIELDS = {'source_text', 'text', 'start', 'end'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def records(project):
    return crud.project_params(project).get('subtitle_edits', {})


def read(ctx, *, translation=True, strict=True):
    source = source_lines(ctx)
    try:
        targets = {l['id']: l['text'] for l in translated_lines(ctx, source)}
    except (DubForgeError, OSError, ValueError):
        if translation:
            raise
        targets = {}
    edits = records(ctx.project)
    result = []
    for raw in source:
        base = {'source_text': raw['text'], 'text': targets.get(raw['id'], ''),
                'start': raw['start'], 'end': raw['end']}
        saved = edits.get(str(raw['id']), {})
        changes = saved.get('values', {})
        previous = saved.get('base', {})
        source_stale = bool(changes) and any(previous.get(k) != base[k] for k in ('source_text', 'start', 'end'))
        target_stale = 'text' in changes and previous.get('text') != base['text']
        stale = source_stale or target_stale
        applied = {} if source_stale else {k: v for k, v in changes.items() if k != 'text' or not target_stale}
        row = {'id': raw['id'], **base, **applied, 'base': base, 'edited': bool(changes),
               'edit_stale': stale, 'saved_edit': changes if stale else None,
               'translation_available': raw['id'] in targets,
               'edit_revision': _hash([raw['id'], base, saved])}
        # 手动挪动字幕/铺轨位置不改变已生成语音的身份；上游重分句仍使其失效。
        if any(row[k] != base[k] for k in ('start', 'end')):
            row['voice_timing'] = [base['start'], base['end']]
        if strict and (source_stale or (translation and target_stale)):
            raise DubForgeError(f"第 {raw['id']} 句的上游内容已变化，请在「字幕与配音」中核对并重新保存修改",
                                code='SUBTITLE_EDIT_STALE')
        result.append(row)
    orphaned = set(edits) - {str(l['id']) for l in source}
    if strict and orphaned:
        raise DubForgeError('上游分句已变化，部分校对记录不再对应现有句子，请先处理失效修改', code='SUBTITLE_EDIT_STALE')
    return result


def fingerprint(project, key, cfg):
    if key not in {'tts', 'dubtask', 'assemble', 'subtitles'}:
        return {}
    fields = {'text', 'start', 'end'}
    if key == 'subtitles':
        mode = cfg('subtitles.mode', 'all')
        fields = {'start', 'end'} | ({'source_text'} if mode == 'source' else {'text'} if mode == 'target' else {'source_text', 'text'})
    return {id: {'base': item.get('base'), 'values': values}
            for id, item in records(project).items()
            if (values := {k: v for k, v in item.get('values', {}).items() if k in fields})}


def save(project, deps, line_id, body):
    """在项目编辑锁内调用；版本检查防止旧页面覆盖新校对稿。"""
    deps.require('translate')  # 编辑只需有效整理稿，不要求翻译或配音服务。
    rows = read(deps, translation=False, strict=False)
    line = next((l for l in rows if l['id'] == line_id), None)
    if line is None:
        raise DubForgeError('句子已不存在，请刷新列表', code='LINE_NOT_FOUND')
    if body.get('revision') != line['edit_revision']:
        raise DubForgeError('本句或上游内容已变化，请重新打开编辑后再保存', code='SUBTITLE_EDIT_CONFLICT')
    reset = body.get('reset') is True
    values = {} if reset else body.get('values')
    if not isinstance(values, dict) or set(values) - FIELDS:
        raise DubForgeError('字幕修改格式无效', code='SUBTITLE_EDIT_INVALID')
    candidate = {**line, **values} if not reset else {**line, **line['base']}
    for k in ('source_text', 'text'):
        if not isinstance(candidate[k], str):
            raise DubForgeError('字幕文字格式无效', code='SUBTITLE_EDIT_INVALID')
        candidate[k] = ' '.join(candidate[k].split())
        if not candidate[k] and (k == 'source_text' or line['translation_available']):
            raise DubForgeError('原文和已有译文不能为空', code='SUBTITLE_EDIT_INVALID')
    if not line['translation_available'] and candidate['text']:
        raise DubForgeError('请先完成翻译，再编辑译文', code='DEPS_NOT_MET')
    for k in ('start', 'end'):
        value = candidate[k]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise DubForgeError('请输入有效的起止秒数', code='SUBTITLE_EDIT_INVALID')
        candidate[k] = round(float(value), 3)
    if candidate['start'] < 0 or candidate['end'] <= candidate['start']:
        raise DubForgeError('开始时间不能为负，结束时间必须晚于开始时间', code='SUBTITLE_EDIT_INVALID')
    index = rows.index(line)
    if ((index and candidate['start'] < rows[index - 1]['end'])
            or (index + 1 < len(rows) and candidate['end'] > rows[index + 1]['start'])):
        raise DubForgeError('时间不能与相邻句子重叠，请先调整相邻句子的边界', code='SUBTITLE_EDIT_INVALID')
    values = {k: candidate[k] for k in FIELDS if candidate[k] != line['base'][k]}
    before = {key: fingerprint(project, key, deps.cfg) for key in ('subtitles', 'dubtask', 'assemble')}
    params = crud.project_params(project)
    saved = params.setdefault('subtitle_edits', {})
    if values:
        saved[str(line_id)] = {'base': line['base'], 'values': values}
    else:
        saved.pop(str(line_id), None)
    # 人工修改仍是同一句，保留声源选择及独立设置；音频另按实际译文判断有效性。
    from dubforge.pipeline.line_settings import identity
    old_identity = identity(line)
    if any(candidate[k] != line['base'][k] for k in ('start', 'end')):
        candidate['voice_timing'] = [line['base']['start'], line['base']['end']]
    else:
        candidate.pop('voice_timing', None)
    for key in ('dubbing_edit', 'tts_lines'):
        item = params.get(key, {}).get(str(line_id))
        if item and item.get('identity') == old_identity:
            item['identity'] = identity(candidate)
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    # 兼容早期没有输入指纹的产物；编辑后也必须明确提示重新导出/合成。
    changed = {key for key, value in before.items() if fingerprint(project, key, deps.cfg) != value}
    if 'subtitles' in changed and deps.cfg('assemble.burn_subtitles', False):
        changed.add('assemble')
    for key in changed:
        step = crud.get_step(project.id, key)
        if step and step.status == 'done':
            step.status = 'stale'
            crud.save_step(step)
    crud.write_manifest(project.uid)
    return values
