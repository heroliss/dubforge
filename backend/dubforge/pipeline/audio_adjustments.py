"""同一配音槽内的自动缩时历史；恢复完整声音，不改用户合成设置。"""
import hashlib
import json
import uuid
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit
from dubforge.pipeline.line_settings import identity
from dubforge.pipeline.tts_duration import audio_seconds
from dubforge.utils.fs import atomic_write_bytes


def _index_path(root, line, take):
    dubbing_edit.take_name(line['id'], take)
    return Path(root) / 'artifacts' / 'tts' / 'adjustments' / f"{line['id']}_v{take}.json"


def _read(root, line, take):
    try:
        data = json.loads(_index_path(root, line, take).read_text(encoding='utf-8'))
        return data if data.get('identity') == identity(line) and data.get('schema') == 1 else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _write(root, line, take, data):
    atomic_write_bytes(_index_path(root, line, take), json.dumps(data, ensure_ascii=False).encode())


def _snapshot(root, line, take, data, label):
    output = dubbing_edit.take_path(Path(root), line['id'], take)
    record = dubbing_edit.read_take(Path(root), line, take)
    entry_id = uuid.uuid4().hex
    folder = _index_path(root, line, take).parent
    audio = output.read_bytes()
    name = entry_id + '.wav'
    atomic_write_bytes(folder / name, audio)
    retry = record.get('duration_retry') or {}
    factor = retry.get('used_factor', record.get('settings', {}).get('duration_factor'))
    data['entries'].append(dict(id=entry_id, file=name, label=label, factor=factor,
                                seconds=audio_seconds(output), sha256=hashlib.sha256(audio).hexdigest(),
                                record=record,
                                fingerprint=output.with_suffix('.input').read_text(encoding='utf-8')))
    data.update(current_id=entry_id, current_version=dubbing_edit.file_version(output))


def begin(root, line, take):
    """首次缩时前归档本轮自然音频；已有同轮记录继续追加，不另建配音槽。"""
    root = Path(root)
    if not dubbing_edit.snapshot_current(root, line, take):
        return None
    data = _read(root, line, take)
    output = dubbing_edit.take_path(root, line['id'], take)
    if data.get('current_version') != dubbing_edit.file_version(output):
        data = dict(schema=1, generation=uuid.uuid4().hex, identity=identity(line), entries=[])
        _snapshot(root, line, take, data, '缩短前的配音')
        _write(root, line, take, data)
    return data['generation']


def finish(root, line, take, generation):
    """仅实际选用了新音频时保存调整结果，模型更长而保留旧结果时不造假记录。"""
    if not generation:
        return
    data = _read(root, line, take)
    output = dubbing_edit.take_path(Path(root), line['id'], take)
    if data.get('generation') != generation or not dubbing_edit.snapshot_current(Path(root), line, take):
        return
    if data.get('current_version') != dubbing_edit.file_version(output):
        _snapshot(root, line, take, data, '自动缩短后的配音')
        _write(root, line, take, data)
    else:
        current = next((entry for entry in data['entries'] if entry['id'] == data.get('current_id')), None)
        if current is not None:
            current['record'] = dubbing_edit.read_take(Path(root), line, take)
            _write(root, line, take, data)


def _revision(data):
    return hashlib.sha256(json.dumps([data.get('generation'), data.get('current_version'),
        data.get('current_id'), [(entry['id'], entry['sha256']) for entry in data.get('entries', [])]],
        sort_keys=True).encode()).hexdigest()


def view(root, line, take):
    data = _read(root, line, take)
    if (len(data.get('entries', [])) < 2
            or data.get('current_version') != dubbing_edit.file_version(
                dubbing_edit.take_path(Path(root), line['id'], take))):
        return None
    return dict(revision=_revision(data), current_id=data['current_id'],
                entries=[{key: entry[key] for key in ('id', 'label', 'factor', 'seconds')}
                         for entry in data['entries']])


def restore(project, line, root, take, body):
    """显式恢复当前槽的调整记录；其他槽、逐句设置和播放时间保持不变。"""
    root = Path(root)
    data = _read(root, line, take)
    current = view(root, line, take)
    if not current or body.get('revision') != current['revision']:
        raise DubForgeError('本句配音或缩时记录已变化，请重新打开侧栏后操作', code='AUDIO_ADJUSTMENT_CONFLICT')
    entry = next((item for item in data['entries'] if item['id'] == body.get('entry_id')), None)
    if entry is None:
        raise DubForgeError('请选择有效的自动缩时记录', code='AUDIO_ADJUSTMENT_INVALID')
    if entry['id'] == data['current_id']:
        return False
    folder = _index_path(root, line, take).parent
    name = entry.get('file', '')
    path = folder / name
    if Path(name).name != name or path.suffix != '.wav':
        raise DubForgeError('配音历史文件路径无效', code='AUDIO_ADJUSTMENT_INVALID')
    try:
        audio = path.read_bytes()
    except OSError as exc:
        raise DubForgeError('此配音历史文件缺失，未修改当前声音', code='AUDIO_ADJUSTMENT_MISSING') from exc
    if hashlib.sha256(audio).hexdigest() != entry['sha256'] or entry['record'].get('identity') != identity(line):
        raise DubForgeError('此配音历史已失效，未修改当前声音', code='AUDIO_ADJUSTMENT_INVALID')
    audio_seconds(path)
    output = dubbing_edit.take_path(root, line['id'], take)
    metadata = output.with_suffix('.take.json')
    marker = output.with_suffix('.input')
    previous = (output.read_bytes(), metadata.read_bytes(), marker.read_bytes())
    try:
        atomic_write_bytes(output, audio)
        record = {**entry['record'], 'version': dubbing_edit.file_version(output)}
        if not record.get('duration_retry') and type(entry.get('factor')) in (int, float):
            # 历史快照可证明实际因子，但缺少请求记录时不能猜测合成起始因子。
            record['duration_retry'] = dict(used_factor=entry['factor'], attempts=[], retries=0,
                                             seconds=entry['seconds'])
        if record.get('duration_retry'):
            record['duration_retry'] = {**record['duration_retry'], 'pending_layout': False}
        atomic_write_bytes(metadata, json.dumps(record, ensure_ascii=False).encode())
        atomic_write_bytes(marker, entry['fingerprint'].encode())
        data.update(current_id=entry['id'], current_version=dubbing_edit.file_version(output))
        _write(root, line, take, data)
    except Exception:
        atomic_write_bytes(output, previous[0])
        old_record = json.loads(previous[1])
        old_record['version'] = dubbing_edit.file_version(output)
        atomic_write_bytes(metadata, json.dumps(old_record, ensure_ascii=False).encode())
        atomic_write_bytes(marker, previous[2])
        # 回滚后的文件版本同样更新，历史仍可以继续使用。
        data.update(current_id=current['current_id'], current_version=dubbing_edit.file_version(output))
        _write(root, line, take, data)
        raise
    crud.add_artifact(project.id, 'tts', 'audio', output.name, output, str(output.relative_to(root)))
    return True
