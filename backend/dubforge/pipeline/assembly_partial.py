"""显式局部修订：固定旧成片基准，只重制用户采用的源时间范围。"""
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_replace, atomic_write_bytes

_ID = re.compile(r'[0-9a-f]{32}')
_GATE = threading.RLock()


class PartialError(DubForgeError):
    code = 'ASSEMBLY_PARTIAL_INVALID'
    suggestion = '请重新检查局部修订范围；基准成片和其他版本不会被修改。'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@lru_cache(maxsize=32)
def _checked_hash(path, size, modified):
    # 预检可复用同一文件版本的摘要；运行前 prepare 始终重新强校验基准。
    return _hash(path)


@lru_cache(maxsize=32)
def _inspect(path, size, modified):
    from dubforge.pipeline.segment_replace import inspect_base
    return inspect_base(Path(path))


@lru_cache(maxsize=32)
def _source_media(path, size, modified):
    try:
        run = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
            'stream=width,height,r_frame_rate,avg_frame_rate', '-of', 'json', path], capture_output=True,
            text=True, encoding='utf-8', timeout=30, check=False)
        stream = json.loads(run.stdout)['streams'][0]
        rate = Fraction(stream['avg_frame_rate'])
        if run.returncode or rate <= 0 or min(stream['width'], stream['height']) <= 0:
            raise ValueError()
        return dict(width=stream['width'], height=stream['height'], fps_num=rate.numerator, fps_den=rate.denominator)
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.TimeoutExpired) as exc:
        raise PartialError('无法核对原视频的画幅和帧率，请确认原视频可播放后重试。',
                           code='ASSEMBLY_PARTIAL_MEDIA') from exc


def _deps(uid):
    from dubforge.pipeline.dependencies import Dependencies
    project = crud.get_project(uid)
    if not project:
        raise PartialError('项目不存在', code='PROJECT_NOT_FOUND')
    return Dependencies(project)


def _source(deps):
    from dubforge.pipeline.output_cuts import source_context, snapshot
    if snapshot(deps):
        raise PartialError('当前项目启用了成片剪辑，局部修订暂不支持时间轴变化，请完整生成成片。',
                           code='ASSEMBLY_PARTIAL_TIMELINE')
    value = source_context(deps)
    if not value['source_fingerprint'] or not value['duration']:
        raise PartialError('原视频尚未就绪，无法核对局部修订时间轴。')
    return value


def _base(deps, version_id):
    from dubforge.pipeline import assembly_versions as av
    record = av.results(deps.project.uid).get(version_id)
    if not record:
        raise PartialError('请选择已有正式成片的版本作为基准。')
    art = av.result_artifact(deps, {'id': version_id})
    path = deps.path(art.step_key, art.name) if art else None
    if not art or not path or not path.is_file() or art.sha256 != record.artifact_hash:
        raise PartialError('基准成片文件已丢失或登记不一致，请重新生成或选择。')
    try:
        saved = json.loads(record.snapshot)
        av.validate({key: saved[key] for key in
                     ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')})
        if (not isinstance(saved.get('values'), dict)
                or type(saved['values'].get('assemble.crf')) is not int
                or not 0 <= saved['values']['assemble.crf'] <= 51):
            raise ValueError('render settings')
    except (ValueError, TypeError, KeyError, DubForgeError) as exc:
        raise PartialError('基准成片缺少有效的成功设置记录，请先完整生成。') from exc
    if saved.get('output_cuts') or saved.get('review_render'):
        raise PartialError('该基准成片经过剪辑或旧审查修订，无法核对源时间轴，请完整生成。',
                           code='ASSEMBLY_PARTIAL_TIMELINE')
    from dubforge.pipeline.video_encoding import require_software_baseline
    require_software_baseline(saved)
    return record, art, path, saved


def context(uid):
    deps = _deps(uid)
    from dubforge.pipeline import assembly_versions as av
    from dubforge.pipeline.subtitle_document import read
    from dubforge.pipeline.output_cuts import source_context
    source = source_context(deps)
    baselines = []
    for version in av.catalog(uid)['versions']:
        try:
            record, art, _, saved = _base(deps, version['id'])
        except PartialError:
            continue
        baselines.append(dict(version_id=version['id'], name=version['name'], artifact_id=art.id,
                              artifact_hash=record.artifact_hash, created_at=art.created_at.isoformat(),
                              duration=source['duration'], partial=bool(saved.get('partial'))))
    baselines.sort(key=lambda row: (row['created_at'], row['artifact_id']), reverse=True)
    try:
        lines = [dict(id=row['id'], start=row['start'], end=row['end'],
                      source_start=row['start'], source_end=row['end'],
                      text=row.get('text') or row['source_text']) for row in read(deps, translation=False)]
        if baselines:
            _, _, _, saved = _base(deps, baselines[0]['version_id'])
            events = _subtitle_ranges(deps, saved)
            for row in lines:
                if row['id'] in events:
                    row['start'] = min(row['start'], events[row['id']]['start'])
                    row['end'] = max(row['end'], events[row['id']]['end'])
    except (DubForgeError, OSError):
        lines = []
    return dict(baselines=baselines, lines=lines, duration=source['duration'] or 0)


def _subtitle_ranges(deps, saved):
    """包含当前呈现及旧成功样式的最短显示扩展，不伪称还原历史文字。"""
    from dubforge.pipeline import assembly_versions as av
    from dubforge.pipeline.version_render import subtitle_lines
    definition = {k: copy.deepcopy(saved[k]) for k in
                  ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
    current = av.resolve(deps, definition)
    result = {}
    for version in (saved, current):
        if version['burn_subtitles']:
            for row in subtitle_lines(deps.for_version(version), version):
                previous = result.get(row.index, dict(start=row.start, end=row.end))
                result[row.index] = dict(start=min(previous['start'], row.start), end=max(previous['end'], row.end))
    return result


def _ranges(body, deps, saved, duration):
    from dubforge.pipeline.output_cuts import merge_ranges
    raw = body.get('ranges', [])
    ids = body.get('line_ids', [])
    if (not isinstance(raw, list) or len(raw) > 200 or not isinstance(ids, list)
            or len(ids) > 10000 or any(type(i) is not int or i <= 0 for i in ids)):
        raise PartialError('请选择有效句子或最多 200 段时间范围。')
    ranges = []
    for row in raw:
        if (not isinstance(row, dict) or set(row) - {'start', 'end'}
                or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) for k in ('start', 'end'))
                or row['start'] < 0 or row['end'] <= row['start'] or row['end'] > duration + .001):
            raise PartialError('替换范围需位于原视频内，结束时间必须晚于开始时间。')
        ranges.append(dict(start=round(float(row['start']), 6), end=round(min(float(row['end']), duration), 6)))
    if ids:
        from dubforge.pipeline.subtitle_document import read
        from dubforge.pipeline.step_subtitles import _apply_voice_timing
        rows = read(deps, translation=False)
        # 显示时钟与句子原始时钟取并集，避免当前配音排布后的字幕落在选择之外。
        shown = _apply_voice_timing(deps.for_version(saved), rows) if saved.get('dubbing') else rows
        by_id = {row['id']: row for row in shown}
        original = {row['id']: row for row in rows}
        events = _subtitle_ranges(deps, saved)
        snapshot = saved.get('sentence_inputs')
        recorded = (snapshot.get('lines', {}) if isinstance(snapshot, dict)
                    and snapshot.get('schema') == 1 and isinstance(snapshot.get('lines'), dict) else {})
        if set(ids) - set(by_id):
            raise PartialError('部分句子已不存在，请刷新后重新选择。')
        for line_id in set(ids):
            a, b = original[line_id], by_id[line_id]
            c = events.get(line_id, b)
            spans = [a, b, c]
            previous = recorded.get(str(line_id))
            old = previous.get('range') if isinstance(previous, dict) and previous.get('known') is True else None
            if (isinstance(old, dict) and all(type(old.get(key)) in (int, float)
                    and math.isfinite(old[key]) for key in ('start', 'end'))
                    and 0 <= old['start'] < old['end'] <= duration + .001):
                # 成功成片记录的是当时真实显示及声音区域；当前时间编辑不能
                # 用重算后的字幕事件替代旧位置，否则旧字幕会残留在基片中。
                spans.append(old)
            ranges.append(dict(start=max(0., min(span['start'] for span in spans)),
                               end=min(duration, max(span['end'] for span in spans))))
    if not ranges:
        raise PartialError('至少选择一句或一段替换范围。')
    merged = merge_ranges(ranges)
    if len(merged) > 200:
        raise PartialError('合并后仍超过 200 段，请分批创建局部修订或完整生成。')
    return merged


def plan(uid, body):
    if not isinstance(body, dict) or set(body) - {'base_version_id', 'line_ids', 'ranges', 'replace_audio', 'name', 'fingerprint', 'update_all'}:
        raise PartialError('局部修订方案格式无效。')
    if (not isinstance(body.get('base_version_id'), str) or type(body.get('replace_audio', False)) is not bool
            or type(body.get('update_all', False)) is not bool):
        raise PartialError('请选择基准成片并明确是否替换声音。')
    deps = _deps(uid)
    source = _source(deps)
    record, art, path, saved = _base(deps, body['base_version_id'])
    from dubforge.pipeline.segment_replace import plan_windows
    stat = path.stat()
    info = copy.deepcopy(_inspect(str(path), stat.st_size, stat.st_mtime_ns))
    duration = float(info['duration'])
    if abs(duration - source['duration']) > max(.1, 2 * info.get('fps_den', 1) / info.get('fps_num', 25)):
        raise PartialError('基准成片与原视频时长不一致，无法等时替换，请完整生成。', code='ASSEMBLY_PARTIAL_TIMELINE')
    from dubforge.pipeline.video_source import resolve_video
    video = resolve_video(deps.project, deps.root)
    source_stat = video.stat()
    media = _source_media(str(video), source_stat.st_size, source_stat.st_mtime_ns)
    if (any(info.get(k, media[k]) != media[k] for k in ('width', 'height'))
            or Fraction(info['fps_num'], info['fps_den']) != Fraction(media['fps_num'], media['fps_den'])):
        raise PartialError('基准成片与原视频的画幅或帧率不同，无法安全局部替换，请完整生成。',
                           code='ASSEMBLY_PARTIAL_TIMELINE')
    if stat.st_size != art.size or _checked_hash(str(path), stat.st_size, stat.st_mtime_ns) != record.artifact_hash:
        raise PartialError('基准成片内容已变化，请重新检查后再建立局部修订。', code='ASSEMBLY_PARTIAL_BASE_CHANGED')
    ranges = _ranges(body, deps, saved, duration)
    windows = plan_windows(info, ranges)
    definition = {k: copy.deepcopy(saved[k]) for k in ('dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
    if body.get('update_all'):
        from dubforge.pipeline import assembly_versions as av, assembly_sentence_changes
        comparison = assembly_sentence_changes.view(uid, body['base_version_id'])
        suggestion = comparison['suggestion']
        if (not suggestion['can_partial'] or suggestion['replace_audio'] and not body.get('replace_audio')
                or any(not any(got['start'] <= needed['start'] and got['end'] >= needed['end'] for got in ranges)
                       for needed in suggestion['ranges'])):
            raise PartialError('全部改动或修订范围已变化，请重新检查全部改动后建立更新版本。', code='ASSEMBLY_UPDATE_RECHECK')
        desired = next(v for v in av.catalog(uid)['versions'] if v['id'] == body['base_version_id'])
        definition = {k: copy.deepcopy(desired[k]) for k in ('dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
        if definition['dubbing'] != saved['dubbing']:
            raise PartialError('声音类型已变化，请完整生成成片。', code='ASSEMBLY_UPDATE_RECHECK')
    definition['overrides']['assemble.crf'] = saved['values']['assemble.crf']
    from dubforge.pipeline.audio_quality import read_report
    report = read_report(path)
    data = dict(schema=1, project_uid=uid, base_version_id=body['base_version_id'], base_name=saved['name'],
                base_artifact_id=art.id, base_path=str(path), base_sha256=art.sha256, base_spec=info,
                base_size=stat.st_size,
                base_version=saved, source_identity=source, duration=duration,
                source_media=media,
                baseline_audio_report=(dict(base_sha256=art.sha256, report=report) if report else None),
                ranges=ranges, replace_audio=body.get('replace_audio', False), definition=definition,
                line_ids=sorted(set(body.get('line_ids', []))))
    if body.get('update_all'):
        data['update_all'] = True
        data['expected_current_inputs_hash'] = _current_inputs_hash(deps,
            dict(id=body['base_version_id'], name=saved['name'], **definition))
    data['fingerprint'] = _digest(data)
    data.update(expanded_ranges=windows.get('expanded_ranges', windows.get('windows', [])),
                encode_seconds=windows.get('encode_seconds', sum(r['end'] - r['start'] for r in ranges)),
                summary='只替换所选范围；范围外保留基准成片，其他项目修改不会自动带入。若改过句子时间，请补充旧字幕所在的时间范围。')
    return data


def _current_inputs_hash(deps, version):
    """全部更新方案绑定实际呈现，不把未应用的TTS意图或字幕旁支当成新内容。"""
    from dubforge import comment_studio
    from dubforge.pipeline import assembly_versions as av
    from dubforge.pipeline.assembly_sentence_changes import capture
    from dubforge.pipeline.assembly_change_groups import global_scope
    plain = {key: copy.deepcopy(version[key]) for key in
             ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
    overlay = comment_studio.effective_overlay(deps.project.uid, comment_studio._read(deps.project).get('overlay'))
    snapshot = capture(deps, av.resolve(deps, plain, comment_plan=overlay))
    return _digest(dict(global_inputs=global_scope(snapshot['global_inputs']),
        comments=snapshot['comment_inputs'], lines={key: {part: row[part] for part in ('subtitle', 'audio')}
                                                    for key, row in snapshot['lines'].items()}))


def create(uid, body):
    from dubforge.project_archive import assert_active
    # 此处只捕获独立的不可变基片，不保存项目设置；大文件读取不能占据
    # 全局任务启动锁。并发替换通过入队前后摘要校验和原子改名保护。
    with _GATE:
        assert_active(uid)
        value = plan(uid, body)
        if not body.get('fingerprint') or body['fingerprint'] != value['fingerprint']:
            raise PartialError('基准或修订范围已变化，请重新检查后添加。', code='ASSEMBLY_PARTIAL_CONFLICT')
        name = body.get('name') or (value['base_name'][:60] + ' · 局部修订')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise PartialError('请填写 1–80 字的版本名称。')
        deps = _deps(uid)
        folder = deps.root / 'output' / '.partial-baselines'
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (value['base_sha256'] + '.mp4')
        if destination.is_file():
            stat = destination.stat()
            if _checked_hash(str(destination), stat.st_size, stat.st_mtime_ns) != value['base_sha256']:
                raise PartialError('保存的基准副本内容已变化，请完整生成或重新选择基准。', code='ASSEMBLY_PARTIAL_BASE_CHANGED')
        else:
            pending = folder / ('.tmp-' + uuid.uuid4().hex + '.mp4')
            try:
                try:
                    os.link(value['base_path'], pending)
                except OSError:
                    shutil.copyfile(value['base_path'], pending)
                if _hash(pending) != value['base_sha256']:
                    raise PartialError('保存基准期间文件发生变化，请重新检查。', code='ASSEMBLY_PARTIAL_BASE_CHANGED')
                atomic_replace(pending, destination)
            finally:
                pending.unlink(missing_ok=True)
        plan_id = uuid.uuid4().hex
        value.update(id=plan_id, base_path=str(destination))
        # 计划身份不可变；只存在本项目中，其他项目不能复用引用。
        immutable = {k: v for k, v in value.items() if k != 'fingerprint'}
        value['fingerprint'] = _digest(immutable)
        assert_active(uid)
        atomic_write_bytes(deps.root / 'output' / '.partial-plans' / (plan_id + '.json'),
                           json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        version = dict(id='partial-' + plan_id, name=name.strip(), **value['definition'], partial=dict(plan_id=plan_id))
        return dict(version=version, ranges=value['ranges'], expanded_ranges=value['expanded_ranges'],
                    duration=value['duration'], encode_seconds=value['encode_seconds'], summary=value['summary'])


def load_plan(deps, version, *, verify_hash=False):
    ref = version.get('partial')
    if not isinstance(ref, dict) or set(ref) != {'plan_id'} or not isinstance(ref['plan_id'], str) or not _ID.fullmatch(ref['plan_id']):
        raise PartialError('局部修订计划引用无效。')
    path = deps.root / 'output' / '.partial-plans' / (ref['plan_id'] + '.json')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if (value['schema'] != 1 or value['id'] != ref['plan_id'] or value['project_uid'] != deps.project.uid
                or value['fingerprint'] != _digest({k: v for k, v in value.items() if k != 'fingerprint'})):
            raise ValueError('identity')
        expected = deps.root / 'output' / '.partial-baselines' / (value['base_sha256'] + '.mp4')
        if (not re.fullmatch(r'[0-9a-f]{64}', value['base_sha256'])
                or Path(value['base_path']).resolve() != expected.resolve() or not expected.is_file()):
            raise ValueError('baseline')
        if expected.stat().st_size != value['base_size']:
            raise PartialError('局部修订的基准副本已变化，请重新选择基准。', code='ASSEMBLY_PARTIAL_BASE_CHANGED')
        if verify_hash and value.get('update_all'):
            try:
                current_inputs = _current_inputs_hash(deps, version)
            except DubForgeError as exc:
                raise PartialError('全部更新方案的当前输入已变化或尚未就绪，请重新检查全部改动。',
                                   code='ASSEMBLY_UPDATE_RECHECK') from exc
            if current_inputs != value.get('expected_current_inputs_hash'):
                raise PartialError('全部更新方案建立后又有内容或设置变化，请重新检查全部改动后建立更新版本。',
                                   code='ASSEMBLY_UPDATE_RECHECK')
        current_source = _source(deps)
        # 帧率等展示元数据后来加入 source_context，不能使旧计划仅因多了
        # 一个字段失效；源身份由指纹和时长决定，画幅/帧率在建计划时另验。
        if any(value['source_identity'].get(key) != current_source.get(key)
               for key in ('source_fingerprint', 'duration')):
            raise PartialError('局部修订的原视频或剪辑时间轴已变化，请重新建立修订版本。', code='ASSEMBLY_PARTIAL_TIMELINE')
        if verify_hash and _hash(expected) != value['base_sha256']:
            raise PartialError('局部修订的基准副本已变化，请重新选择基准。', code='ASSEMBLY_PARTIAL_BASE_CHANGED')
        return copy.deepcopy(value)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PartialError('本项目找不到有效的局部修订计划；不能跨项目复制此版本，请重新建立。',
                           code='ASSEMBLY_PARTIAL_PLAN_MISSING') from exc


def prepare(ctx, version):
    deps = ctx.dependencies() if hasattr(ctx, 'dependencies') else ctx
    return load_plan(deps, version, verify_hash=True)


def fingerprint(deps, version):
    value = load_plan(deps, version)
    return dict(plan_id=value['id'], plan_fingerprint=value['fingerprint'], baseline=value['base_sha256'])
