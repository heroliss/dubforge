"""配音编辑结果：版本、声源选择和成片使用同一份项目状态。"""
import json
import re
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.line_settings import identity
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


def read_take(root, line, take):
    path = take_path(root, line['id'], take)
    try:
        data = json.loads(path.with_suffix('.take.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def playable(project, line, root, take, *, dependencies=None):
    if take is None:
        return False
    path = take_path(root, line['id'], take)
    if not path.is_file() or path.stat().st_size == 0:
        return False
    data = read_take(root, line, take)
    if data:
        # 参数改变不抹掉历史试听版本；文本、时间轴或实际文件改变才过期。
        return data.get('identity') == identity(line) and data.get('version') == file_version(path)
    # 老版单句音频继续兼容；尚无生成快照时沿用原来的严格缓存检查。
    from dubforge.pipeline.step_tts import line_cache_valid
    return line_cache_valid(project, line, root, dependencies=dependencies, take=take)


def record_take(project, line, root, take, fingerprint, settings, *, duration_retry=None):
    path = take_path(root, line['id'], take)
    atomic_write_bytes(path.with_suffix('.take.json'), json.dumps({
        'identity': identity(line), 'version': file_version(path),
        'fingerprint': fingerprint, 'settings': settings,
        **({'duration_retry': duration_retry} if duration_retry else {}),
    }, ensure_ascii=False).encode())


def selection(project, line, root=None, *, dependencies=None, artifacts=None):
    root = root or crud.project_dir(project.uid)
    saved = crud.project_params(project).get('dubbing_edit', {}).get(str(line['id']), {})
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
    return [{**line, **selection(project, line, root, dependencies=dependencies, artifacts=arts)} for line in lines]


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
