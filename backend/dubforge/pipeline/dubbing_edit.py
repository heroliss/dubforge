"""配音编辑结果：版本、声源选择和成片使用同一份项目状态。"""
import json
import re
import copy
import os
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.line_settings import identity, project_parameters
from dubforge.utils.fs import atomic_write_bytes


def take_name(line_id, take=1):
    if type(take) is not int or not 1 <= take <= 10000:
        raise DubForgeError('配音版本无效', code='DUB_TAKE_INVALID')
    return f'{line_id}.wav' if take == 1 else f'{line_id}_v{take}.wav'


def take_path(root, line_id, take=1):
    return root / 'artifacts' / 'tts' / take_name(line_id, take)


def file_version(path):
    try:
        s = path.stat()
        return f'{s.st_mtime_ns}-{s.st_size}'
    except OSError:
        return '0'


def take_ids(root, line_id):
    folder = root / 'artifacts' / 'tts'
    result = {1} if (folder / f'{line_id}.wav').is_file() else set()
    for path in folder.glob(f'{line_id}_v*.wav'):
        match = re.fullmatch(rf'{line_id}_v(\d+)\.wav', path.name)
        if match and 1 <= int(match[1]) <= 10000:
            result.add(int(match[1]))
    return sorted(result)


def take_index(root):
    """一次列出句级槽目录，整页读取不为每句话重复扫描文件夹。"""
    result = {}
    for path in (root / 'artifacts' / 'tts').glob('*.wav'):
        match = re.fullmatch(r'(\d+)(?:_v(\d+))?\.wav', path.name)
        if match and path.is_file() and 1 <= int(match[2] or 1) <= 10000:
            result.setdefault(int(match[1]), set()).add(int(match[2] or 1))
    return {ident: sorted(takes) for ident, takes in result.items()}


def read_take(root, line, take):
    path = take_path(root, line['id'], take)
    try:
        data = json.loads(path.with_suffix('.take.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def file_read_identity(path):
    """路径与只读句柄双身份；Windows 同大小改写后恢复 mtime 仍须失效。"""
    before = path.stat()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
    after = path.stat()
    fields = lambda stat: (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)
    path_version, opened_version = fields(before), fields(opened)
    if (path_version != fields(after)
            or path_version[:2] + path_version[3:] != opened_version[:2] + opened_version[3:]):
        raise OSError('file changed during metadata read')
    return path_version, opened_version


def take_snapshot(root, line, take, *, dependencies=None):
    """请求内复用解析结果；改写、替换、删除记录后立即重新读取。"""
    cache = getattr(dependencies, '_take_snapshots', None)
    if cache is None:
        return read_take(root, line, take)
    path = take_path(root, line['id'], take).with_suffix('.take.json')
    try:
        version = file_read_identity(path)
    except OSError:
        cache.pop(str(path), None)
        return {}
    previous = cache.get(str(path))
    if previous is not None and previous[0] == version:
        return copy.deepcopy(previous[1])
    data = read_take(root, line, take)
    # 无效或读取中变化的记录不能登记为稳定结果。
    try:
        stable = version == file_read_identity(path)
    except OSError:
        stable = False
    if data and stable:
        cache[str(path)] = (version, copy.deepcopy(data))
    else:
        cache.pop(str(path), None)
    return data


def _line_snapshot(line):
    return {key: copy.deepcopy(line[key]) for key in ('id', 'text', 'start', 'end', 'voice_timing') if key in line}


def recorded_line(project, line, data):
    """仅接受可由原 identity 核验的生成稿；历史缺失时不猜旧文字。"""
    candidates = [data.get('line_snapshot'), line]
    for candidate in candidates:
        if (isinstance(candidate, dict) and all(key in candidate for key in ('id', 'text', 'start', 'end'))
                and data.get('identity') == identity(candidate)):
            return candidate
    # 新快照与当前稿能自证时立即返回；只为真正缺少旧稿的历史记录解析
    # 项目JSON，避免长片轮询为每个槽反复解码完整项目精调记录。
    if project is not None and hasattr(project, 'params'):
        base = crud.project_params(project).get('subtitle_edits', {}).get(str(line['id']), {}).get('base')
        if isinstance(base, dict) and all(key in base for key in ('text', 'start', 'end')):
            candidate = dict(id=line['id'], **{key: base[key] for key in ('text', 'start', 'end')})
            if data.get('identity') == identity(candidate):
                return candidate
    return None


def current_reading(project, line, *, dependencies=None, cfg_get=None):
    from dubforge.pipeline.line_settings import effective_config
    from dubforge.pipeline import pronunciation
    if cfg_get is None:
        from dubforge.pipeline.dependencies import Dependencies
        cfg_get = (dependencies or Dependencies(project)).cfg
    params = project_parameters(project, dependencies)
    get = effective_config(project, line, cfg_get, params=params)
    return pronunciation.spoken_text(line, get) if get('tts.backend', 'indextts25') == 'indextts25' else line['text']


def recorded_reading(project, line, data):
    value = data.get('settings', {}).get('pronunciation_text') if isinstance(data.get('settings'), dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = data.get('spoken_text')
    if isinstance(value, str) and value:
        return value
    previous = recorded_line(project, line, data)
    return previous['text'] if previous else None


def snapshot_current(root, line, take, *, snapshot=None, version=None, project=None, dependencies=None, cfg_get=None):
    """判断带快照的单句音频是否仍对应当前文字、时间和文件版本。"""
    path = take_path(root, line['id'], take)
    data = take_snapshot(root, line, take, dependencies=dependencies) if snapshot is None else snapshot
    version = file_version(path) if version is None else version
    if not data or data.get('version') != version:
        return False
    if data.get('identity') == identity(line):
        return True
    if project is None:
        return False
    previous = recorded_line(project, line, data)
    if not previous or previous['id'] != line['id']:
        return False
    before = previous.get('voice_timing', [previous['start'], previous['end']])
    after = line.get('voice_timing', [line['start'], line['end']])
    # 固定朗读未变时，字幕改写不使同一句真实音频失效；上游换句/换时轴仍需核对。
    return (before == after and recorded_reading(project, line, data)
            == current_reading(project, line, dependencies=dependencies, cfg_get=cfg_get))


def playable(project, line, root, take, *, dependencies=None, snapshot=None, version=None):
    if take is None:
        return False
    path = take_path(root, line['id'], take)
    version = file_version(path) if version is None else version
    if version == '0' or version.endswith('-0'):
        return False
    data = take_snapshot(root, line, take, dependencies=dependencies) if snapshot is None else snapshot
    if data:
        # 参数改变不抹掉历史试听版本；文本、时间轴或实际文件改变才过期。
        return snapshot_current(root, line, take, snapshot=data, version=version,
                                project=project, dependencies=dependencies)
    # 老版单句音频继续兼容；尚无生成快照时沿用原来的严格缓存检查。
    from dubforge.pipeline.step_tts import line_cache_valid
    return line_cache_valid(project, line, root, dependencies=dependencies, take=take)


def record_take(project, line, root, take, fingerprint, settings, *, duration_retry=None, output_path=None):
    path = output_path or take_path(root, line['id'], take)
    atomic_write_bytes(path.with_suffix('.take.json'), json.dumps({
        'identity': identity(line), 'version': file_version(path),
        'fingerprint': fingerprint, 'settings': settings,
        'line_snapshot': _line_snapshot(line),
        'spoken_text': (settings.get('pronunciation_text') or line['text']),
        **({'duration_retry': duration_retry} if duration_retry else {}),
    }, ensure_ascii=False).encode())


def selection(project, line, root=None, *, dependencies=None, artifacts=None, params=None):
    root = root or crud.project_dir(project.uid)
    params = crud.project_params(project) if params is None else params
    saved = params.get('dubbing_edit', {}).get(str(line['id']), {})
    if saved.get('identity') == identity(line):
        return {'source': saved['source'], 'take': saved.get('take'), 'explicit': True}
    arts = artifacts if artifacts is not None else (dependencies.arts if dependencies is not None else crud.latest_artifacts(project.id, ['tts']))
    had_first = take_path(root, line['id'], 1).is_file() or f"tts/{line['id']}.wav" in arts
    return {'source': 'dub' if had_first else 'original',
            'take': 1 if had_first else None, 'explicit': False}


def save_selection(project, line, source, take):
    if source not in {'original', 'dub'}:
        raise DubForgeError('请选择原声或配音', code='DUB_SOURCE_INVALID')
    if take is not None:
        take_name(line['id'], take)
        if not playable(project, line, crud.project_dir(project.uid), take):
            raise DubForgeError('此配音版本尚未生成或输入已过期，请先合成', code='DUB_TAKE_MISSING')
    params = crud.project_params(project)
    params.setdefault('dubbing_edit', {})[str(line['id'])] = {
        'identity': identity(line), 'source': source, 'take': take,
    }
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    crud.write_manifest(project.uid)


def render_lines(project, lines, root=None, *, dependencies=None):
    root = root or crud.project_dir(project.uid)
    # 整张列表只读一次产物索引，不能每句各查询数据库。
    arts = dependencies.arts if dependencies is not None else crud.latest_artifacts(project.id, ['tts'])
    params = project_parameters(project, dependencies)
    return [{**line, **selection(project, line, root, dependencies=dependencies, artifacts=arts, params=params)} for line in lines]


def synthesis_lines(project, lines, root, *, dependencies=None):
    # 未编辑的项目仍可一键生成全部；明确选用原声或静音空位的句子不被批量误覆盖。
    return [line for line in production_lines(project, lines, root, dependencies=dependencies)
            if not line['explicit'] or (line['source'] == 'dub' and line['take'] is not None)]


def production_lines(project, lines, root=None, *, dependencies=None):
    """正式制作默认需要配音；未生成时的原声回退仅用于工作台试听。"""
    return [line if line['explicit'] else {**line, 'source': 'dub', 'take': 1}
            for line in render_lines(project, lines, root, dependencies=dependencies)]


def confirm_key(line_id, take):
    return str(line_id) if take == 1 else f'{line_id}_v{take}'
