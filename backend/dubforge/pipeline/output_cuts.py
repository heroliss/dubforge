"""项目成片裁剪：源时间轴上的非破坏性删除方案与时间映射。"""
import copy
import hashlib
import json
import math
import re
import subprocess
import threading
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.video_source import resolve_video
from dubforge.utils.fs import atomic_write_bytes

# 剪辑渲染合同升级只使有剪辑的旧成片失效，未剪辑版本保留缓存。
RENDER_VERSION = 2

_LOCK = threading.RLock()
_EPS = .000001
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}')
_VERSION_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}')


class OutputCutsError(DubForgeError):
    code = 'OUTPUT_CUTS_INVALID'
    suggestion = '请检查待删除片段的起止时间，保存后重新合成成片。'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _number(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise OutputCutsError(f'{label}必须是有限的秒数')
    return round(float(value), 6)


@lru_cache(maxsize=128)
def _duration(path, size, modified):
    # 文件版本作缓存键；轮询方案时不反复启动 ffprobe。
    from dubforge.pipeline.step_assemble import _ffprobe_duration
    seconds = _ffprobe_duration(Path(path))
    return seconds if math.isfinite(seconds) and seconds > 0 else None


@lru_cache(maxsize=128)
def _frame_rate(path, size, modified):
    """读取源视频平均帧率，供逐帧定位使用；失败时保留前端的保守步长。"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=avg_frame_rate,r_frame_rate', '-of', 'json', path],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=10, check=False)
        if result.returncode:
            return None
        stream = (json.loads(result.stdout or '{}').get('streams') or [{}])[0]
        for key in ('avg_frame_rate', 'r_frame_rate'):
            value = stream.get(key)
            if not value or value in {'0/0', 'N/A'}:
                continue
            rate = Fraction(str(value))
            if rate > 0 and math.isfinite(float(rate)):
                return round(float(rate), 6)
    except (OSError, ValueError, TypeError, KeyError, IndexError,
            ZeroDivisionError, subprocess.TimeoutExpired):
        pass
    return None


def source_context(deps):
    """返回源身份与真实时长；文件版本变化后重新探测。"""
    path = resolve_video(deps.project, deps.root)
    if path is None:
        return {'source_fingerprint': '', 'duration': None, 'frame_rate': None}
    try:
        stat = path.stat()
    except OSError:
        return {'source_fingerprint': '', 'duration': None, 'frame_rate': None}
    artifact = getattr(deps, 'arts', {}).get('download/video.mp4')
    identity = {'path': str(path.resolve()), 'size': stat.st_size,
                'modified': stat.st_mtime_ns,
                'source_kind': deps.project.source_kind,
                'source_url': deps.project.source_url,
                'artifact': getattr(artifact, 'sha256', '')}
    return {'source_fingerprint': _digest(identity),
            'duration': _duration(str(path.resolve()), stat.st_size, stat.st_mtime_ns),
            'frame_rate': _frame_rate(str(path.resolve()), stat.st_size, stat.st_mtime_ns)}


def _read(root):
    path = Path(root) / 'output_cuts.json'
    if not path.is_file():
        return {'schema': 1, 'enabled': False, 'ranges': []}, '0'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise OutputCutsError('成片剪辑方案读取失败，请检查项目文件后重试',
                              code='OUTPUT_CUTS_READ_FAILED') from exc
    if not isinstance(raw, dict) or raw.get('schema') not in {1, 2}:
        raise OutputCutsError('成片剪辑方案格式不受支持', code='OUTPUT_CUTS_READ_FAILED')
    # schema 2 只是在项目级方案旁增加版本级方案映射；旧 schema 1
    # 文档继续作为全局默认读取，避免已有项目升级后丢失剪辑范围。
    if raw.get('schema') == 2 and not isinstance(raw.get('versions', {}), dict):
        raise OutputCutsError('成片剪辑方案格式不受支持', code='OUTPUT_CUTS_READ_FAILED')
    return raw, _digest(raw)


def _version_plan(raw, version_id=None):
    """选择版本级方案；未建立版本方案时回退到旧项目级方案。"""
    if isinstance(version_id, str) and version_id and isinstance(raw.get('versions'), dict):
        value = raw['versions'].get(version_id)
        if isinstance(value, dict):
            return value, 'version'
    return raw, 'project'


def plan_scope(deps, version_id=None):
    """返回版本剪辑方案来源，供运行冻结区分全局回退与显式版本方案。"""
    version_id = _validate_version_id(version_id)
    with _LOCK:
        raw, _ = _read(deps.root)
        _, scope = _version_plan(raw, version_id)
        return scope


def _validate_version_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _VERSION_IDENTIFIER.fullmatch(value):
        raise OutputCutsError('成片版本标识无效，请刷新后重试。', code='OUTPUT_CUTS_VERSION_INVALID')
    return value


def _baseline_for(deps, version_id, *, verify_file=False):
    """读取已登记成片的不可变剪辑快照，作为新版本的起点。"""
    if not version_id:
        return None
    try:
        from dubforge.pipeline import assembly_versions
        records = assembly_versions.results(deps.project.uid)
        record = records.get(version_id)
        if record is None:
            return None
        saved = json.loads(record.snapshot or '{}')
        cuts = saved.get('output_cuts')
        if cuts is not None and (not isinstance(cuts, dict) or not isinstance(cuts.get('ranges'), list)):
            return None
        artifact = assembly_versions.result_artifact(deps, {'id': version_id}, records)
        path = deps.path(artifact.step_key, artifact.name) if artifact is not None else None
        if (artifact is None or not path or not path.is_file()
                or getattr(artifact, 'sha256', '') != getattr(record, 'artifact_hash', '')
                or getattr(artifact, 'size', 0) != path.stat().st_size):
            return None
        if verify_file:
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != record.artifact_hash:
                    return None
        source_ranges = (cuts or {}).get('ranges', []) if isinstance(cuts, dict) else []
        # 成片登记的 effective_ranges 为纯时间区间（没有编辑器 id）；
        # 重新载入到版本草稿时补充稳定的基准标识，便于恢复/禁用某一段。
        ranges = []
        for index, row in enumerate(source_ranges, 1):
            if not isinstance(row, dict):
                continue
            ranges.append({
                'id': str(row.get('id') or f'baseline-{version_id}-{index}'),
                'start': row.get('start'), 'end': row.get('end'),
                'reason': row.get('reason', '来自旧成片'), 'enabled': row.get('enabled', True),
            })
        return {
            'version_id': version_id,
            'artifact_id': getattr(artifact, 'id', None),
            'artifact_hash': getattr(record, 'artifact_hash', ''),
            'generated_at': saved.get('generated_at'),
            'source_fingerprint': (cuts or {}).get('source_fingerprint') if isinstance(cuts, dict) else None,
            'source_duration': (cuts or {}).get('source_duration') if isinstance(cuts, dict) else None,
            'output_duration': (cuts or {}).get('output_duration') if isinstance(cuts, dict) else None,
            'ranges': ranges,
            'enabled': bool(cuts),
        }
    except (OSError, TypeError, ValueError, KeyError, AttributeError):
        return None


def _baseline_options(deps):
    """提供 UI 可读的旧成片基准清单，不读取当前工作稿冒充历史产物。"""
    try:
        from dubforge.pipeline import assembly_versions
        names = {row['id']: row['name'] for row in assembly_versions.catalog(deps.project.uid)['versions']}
        records = assembly_versions.results(deps.project.uid)
        result = []
        for version_id, record in records.items():
            if version_id not in names:
                continue
            item = _baseline_for(deps, version_id)
            if not item:
                continue
            result.append({
                'id': version_id,
                'version_id': version_id,
                'name': names[version_id],
                'artifact_id': item.get('artifact_id'),
                'artifact_hash': item.get('artifact_hash'),
                'generated_at': item.get('generated_at'),
                'duration': item.get('output_duration') or item.get('source_duration'),
                'has_cuts': bool(item.get('ranges')),
                'range_count': len(item.get('ranges') or []),
            })
        return result
    except (DubForgeError, OSError, TypeError, ValueError, KeyError, AttributeError):
        return []


def merge_ranges(ranges):
    """重叠与相邻删除区间合并，不改变用户的逐条备注。"""
    result = []
    for row in sorted(ranges, key=lambda item: (item['start'], item['end'])):
        if result and row['start'] <= result[-1]['end'] + _EPS:
            result[-1]['end'] = max(result[-1]['end'], row['end'])
        else:
            result.append({'start': row['start'], 'end': row['end']})
    return result


def _validate_ranges(raw, duration):
    if not isinstance(raw, list) or len(raw) > 200:
        raise OutputCutsError('删除片段必须是列表，最多保存 200 段；请合并相邻范围后重试')
    result, ids = [], set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict) or set(item) - {'id', 'start', 'end', 'reason', 'enabled'}:
            raise OutputCutsError(f'第 {index} 段的格式无效')
        identifier = item.get('id')
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier) or identifier in ids:
            raise OutputCutsError(f'第 {index} 段的标识无效或重复')
        start = _number(item.get('start'), f'第 {index} 段起点')
        end = _number(item.get('end'), f'第 {index} 段终点')
        if start < 0 or end <= start or duration is not None and end > duration + _EPS:
            raise OutputCutsError(f'第 {index} 段需满足 0 ≤ 起点 < 终点 ≤ 视频时长')
        enabled = item.get('enabled', True)
        if type(enabled) is not bool:
            raise OutputCutsError(f'第 {index} 段的启用状态必须是布尔值')
        reason = item.get('reason', '')
        if not isinstance(reason, str) or len(reason) > 500:
            raise OutputCutsError(f'第 {index} 段备注最多为 500 字')
        ids.add(identifier)
        result.append({'id': identifier, 'start': start, 'end': min(end, duration) if duration is not None else end,
                       'reason': reason.strip(), 'enabled': enabled})
    return result


def _effective(raw, duration):
    if type(raw.get('enabled')) is not bool:
        raise OutputCutsError('剪辑方案的启用状态无效')
    rows = _validate_ranges(raw.get('ranges', []), duration)
    ranges = merge_ranges([row for row in rows if row['enabled']]) if raw['enabled'] else []
    if duration is not None and duration - sum(row['end'] - row['start'] for row in ranges) <= _EPS:
        raise OutputCutsError('删除后没有可保留画面，请至少保留一段视频', code='OUTPUT_CUTS_EMPTY')
    return rows, ranges


def _timeline_editor_plan(deps, version_id):
    """未另存编辑草稿时，剪辑版从冻结的实际删除范围开始编辑；读取不写盘。"""
    if not version_id:
        return None
    try:
        from dubforge.pipeline import assembly_versions, assembly_timeline
        version = next((row for row in assembly_versions.catalog(deps.project.uid)['versions']
                        if row['id'] == version_id), None)
        if not version or not version.get('timeline'):
            return None
        value = assembly_timeline.load_plan(deps, version)
        snapshot = value.get('output_cuts')
        source_ranges = (snapshot or {}).get('ranges', [])
        # 量化后的源坐标属于不可变方案；补充编辑器所需的稳定 id，不能用
        # 项目默认清单替代，否则新成片刚生成就会被误报为剪辑待应用。
        ranges = [{'id': f'timeline-{version["timeline"]["plan_id"]}-{index}',
                   'start': row['start'], 'end': row['end'],
                   'reason': row.get('reason', '来自本版本剪辑记录'), 'enabled': True}
                  for index, row in enumerate(source_ranges, 1)]
        return {'enabled': bool(snapshot), 'ranges': ranges,
                'source_fingerprint': (snapshot or {}).get('source_fingerprint')
                or value['source_fingerprint']}
    except (DubForgeError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # 无效引用沿用既有项目默认读取行为；来源校验与正式生成仍会拒绝
        # 无效的剪辑方案。这里不修复、不覆盖用户已保存的任何清单。
        return None


def _view(deps, version_id=None, baseline_version_id=None):
    raw, revision = _read(deps.root)
    version_id = _validate_version_id(version_id)
    baseline_version_id = _validate_version_id(baseline_version_id)
    selected, scope = _version_plan(raw, version_id)
    if scope != 'version' and baseline_version_id is None:
        timeline_plan = _timeline_editor_plan(deps, version_id)
        if timeline_plan is not None:
            selected, scope = timeline_plan, 'version'
    # 浏览“以旧成片为基准”时，未保存的版本级草稿可以直接展示基准
    # 的源时间轴范围；保存仍需显式提交 version_id，避免 GET 产生状态。
    baseline = None
    if baseline_version_id and version_id:
        # 仅加载登记摘要，真正保存时再做全文件哈希；切换下拉选项不应
        # 因读取数 GB 旧成片而阻塞编辑器。
        baseline = _baseline_for(deps, baseline_version_id)
        if baseline is not None:
            selected = {**selected, 'enabled': baseline['enabled'], 'ranges': baseline['ranges'],
                        'source_fingerprint': baseline.get('source_fingerprint')}
    if scope == 'version' and baseline_version_id is None:
        baseline_ref = selected.get('baseline')
        if isinstance(baseline_ref, dict):
            baseline = copy.deepcopy(baseline_ref)
    context = source_context(deps)
    duration = context['duration']
    changed = bool(selected.get('source_fingerprint') and
                   selected['source_fingerprint'] != context['source_fingerprint'])
    rows, effective = _effective(selected, None if changed else duration)
    warnings = []
    if duration is None:
        warnings.append('源视频尚未就绪或无法读取时长，暂不能保存剪辑方案。')
    if changed:
        warnings.append('源视频已变化，旧剪辑方案尚未应用；请检查范围并确认重新绑定。')
        effective = []
    removed = round(sum(row['end'] - row['start'] for row in effective), 6)
    plans = raw.get('versions', {}) if isinstance(raw.get('versions'), dict) else {}
    plan_summaries = {key: {
        'version_id': key,
        'enabled': bool(value.get('enabled')),
        'range_count': len(value.get('ranges') or []) if isinstance(value, dict) else 0,
        'baseline': copy.deepcopy(value.get('baseline')) if isinstance(value, dict) else None,
    } for key, value in plans.items() if isinstance(key, str) and isinstance(value, dict)}
    return {**context, 'revision': revision, 'version_id': version_id, 'scope': scope,
            'enabled': bool(selected.get('enabled')), 'ranges': rows,
            'effective_ranges': effective, 'removed_duration': removed,
            'output_duration': round(duration - removed, 6) if duration is not None else None,
            'warnings': warnings, 'source_changed': changed,
            'bound_source_fingerprint': selected.get('source_fingerprint'),
            'baseline': baseline,
            # 前端历史字段名兼容；两者始终指向同一不可变基准摘要。
            'cut_baseline': copy.deepcopy(baseline),
            'plans': plan_summaries,
            'baselines': _baseline_options(deps),
            'source_url': f'/api/projects/{deps.project.uid}/workbench/video' if context['source_fingerprint'] else None}


def view(uid, version_id=None, baseline_version_id=None):
    from dubforge.pipeline.dependencies import Dependencies
    project = crud.get_project(uid)
    if project is None:
        raise OutputCutsError('项目不存在', code='PROJECT_NOT_FOUND')
    with _LOCK:
        return _view(Dependencies(project), version_id, baseline_version_id)


def save(uid, body):
    """乐观并发保存；当前任务已冻结的方案不受新保存影响。"""
    from dubforge import runtime_edit
    from dubforge.pipeline.dependencies import Dependencies
    if not isinstance(body, dict) or set(body) - {'revision', 'source_fingerprint', 'enabled', 'ranges',
                                                  'rebind_source', 'version_id', 'base_version_id',
                                                  'baseline_version_id'}:
        raise OutputCutsError('剪辑方案保存格式无效')
    version_id = _validate_version_id(body.get('version_id'))
    requested_base = body.get('base_version_id') or body.get('baseline_version_id')
    if body.get('base_version_id') and body.get('baseline_version_id') \
            and body['base_version_id'] != body['baseline_version_id']:
        raise OutputCutsError('旧成片基准不一致，请重新选择。', code='OUTPUT_CUTS_BASE_INVALID')
    base_version_id = _validate_version_id(requested_base)
    if version_id and base_version_id == version_id:
        raise OutputCutsError('剪辑基准必须是其他已生成的成片版本。',
                              code='OUTPUT_CUTS_BASE_INVALID')
    # 精确路径不冻结正在运行的合成；启动锁只覆盖短暂的保存边界。
    with runtime_edit.editing('project:' + uid, paths={'output_cuts'}), _LOCK:
        project = crud.get_project(uid)
        deps = Dependencies(project)
        current = _view(deps, version_id)
        if body.get('revision') != current['revision']:
            raise OutputCutsError('剪辑方案已被其他页面修改，请刷新后重试',
                                  code='OUTPUT_CUTS_CONFLICT')
        if not current['source_fingerprint'] or current['duration'] is None:
            raise OutputCutsError('源视频尚未就绪或无法读取时长，请先完成获取视频',
                                  code='OUTPUT_CUTS_SOURCE_UNAVAILABLE')
        if body.get('source_fingerprint') != current['source_fingerprint']:
            raise OutputCutsError('源视频已变化，请刷新并检查删除范围',
                                  code='OUTPUT_CUTS_SOURCE_CHANGED')
        if current['source_changed'] and body.get('rebind_source') is not True:
            raise OutputCutsError('请检查范围后明确确认将剪辑方案应用到当前源视频',
                                  code='OUTPUT_CUTS_SOURCE_CHANGED')
        enabled = body.get('enabled')
        if type(enabled) is not bool:
            raise OutputCutsError('请选择是否启用成片剪辑')
        raw, _ = _read(deps.root)
        if base_version_id:
            baseline = _baseline_for(deps, base_version_id, verify_file=True)
            if baseline is None:
                raise OutputCutsError('所选旧成片不存在或缺少有效剪辑记录，请重新选择。',
                                      code='OUTPUT_CUTS_BASE_UNAVAILABLE')
            # 旧成片必须来自同一源视频，否则源时间轴无法可靠映射。
            source_fp = baseline.get('source_fingerprint')
            if source_fp and source_fp != current['source_fingerprint']:
                raise OutputCutsError('旧成片与当前原视频不是同一版本，无法安全映射时间轴。',
                                      code='OUTPUT_CUTS_BASE_CHANGED')
        else:
            baseline = None
        selected = {'schema': 1, 'enabled': enabled,
                    'source_fingerprint': current['source_fingerprint'],
                    'source_duration': current['duration'], 'ranges': body.get('ranges', [])}
        selected['ranges'], _ = _effective(selected, current['duration'])
        if baseline:
            selected['baseline'] = {key: copy.deepcopy(baseline.get(key)) for key in
                                    ('version_id', 'artifact_id', 'artifact_hash', 'generated_at',
                                     'source_fingerprint', 'source_duration', 'output_duration',
                                     'enabled', 'ranges')}
        if version_id:
            # 版本级写入升级到 schema 2；项目级字段仍保留为旧方案回退值。
            document = copy.deepcopy(raw)
            document['schema'] = 2
            document.setdefault('versions', {})
            if not isinstance(document['versions'], dict):
                raise OutputCutsError('成片剪辑方案格式不受支持', code='OUTPUT_CUTS_READ_FAILED')
            document['versions'][version_id] = selected
            raw = document
        else:
            # 未指定版本时更新项目级默认；schema2 的版本映射必须保留，
            # 否则旧版全局编辑会悄悄抹掉其他版本的独立剪辑方案。
            raw = copy.deepcopy(raw) if raw.get('schema') == 2 else {'schema': 1}
            raw.update(enabled=enabled, source_fingerprint=current['source_fingerprint'],
                       source_duration=current['duration'], ranges=selected['ranges'])
        try:
            atomic_write_bytes(deps.root / 'output_cuts.json',
                               (json.dumps(raw, ensure_ascii=False, indent=2) + '\n').encode())
        except OSError as exc:
            raise OutputCutsError('成片剪辑方案保存失败，原方案已保留，请检查文件权限后重试',
                                  code='OUTPUT_CUTS_WRITE_FAILED') from exc
        return _view(deps, version_id)


def snapshot(deps, version_id=None):
    """只冻结有效渲染字段；备注和禁用片段不影响成片指纹。"""
    version_id = _validate_version_id(version_id)
    snapshots = getattr(deps, '_output_cuts_snapshots', None)
    if isinstance(snapshots, dict) and version_id in snapshots:
        return copy.deepcopy(snapshots[version_id])
    if version_id is None and hasattr(deps, '_output_cuts_snapshot'):
        return copy.deepcopy(deps._output_cuts_snapshot)
    with _LOCK:
        raw, _ = _read(deps.root)
        selected, _ = _version_plan(raw, version_id)
        _, enabled_ranges = _effective(selected, None)
        if not enabled_ranges:
            return None
        context = getattr(deps, '_output_cuts_source_context', None) or source_context(deps)
        if not context['source_fingerprint'] or context['duration'] is None:
            raise OutputCutsError('无法读取成片剪辑所绑定的源视频', code='OUTPUT_CUTS_SOURCE_UNAVAILABLE')
        if selected.get('source_fingerprint') != context['source_fingerprint']:
            raise OutputCutsError('源视频已变化，请检查并重新绑定成片剪辑方案',
                                  code='OUTPUT_CUTS_SOURCE_CHANGED')
        _, ranges = _effective(selected, context['duration'])
        if not ranges:
            return None
        return {'schema': 1, 'source_fingerprint': context['source_fingerprint'],
                'ranges': copy.deepcopy(ranges), 'source_duration': context['duration'],
                'output_duration': round(context['duration'] - sum(row['end'] - row['start'] for row in ranges), 6)}


def fingerprint(value):
    """用于成片依赖比较；None 保留现有未剪辑产物的兼容性。"""
    return _digest({'renderer': RENDER_VERSION, 'plan': value}) if value else None


def mux_timing_options(duration):
    """补齐 concat 丢失的帧时长，并让音视频以保留区间终点结束。"""
    duration = _number(duration, '剪辑成片时长')
    if duration <= 0:
        raise OutputCutsError('剪辑成片时长必须大于 0')
    # concat 的帧率未知，x264 因而输出 duration=0；MP4 的末帧会被标成 discard。
    # MP4 中间 sample 时长由相邻 DTS 确定；缺时长的 sample 只需给出共同终点。
    # STARTDTS 保留 B 帧的负向解码预滚，不改变任何 PTS/DTS，也不把 VFR 重采样为 CFR。
    endpoint = f'max(1,round({duration:.6f}/TB+STARTDTS-DTS))'
    expression = f'if(gt(DURATION,0),DURATION,{endpoint})'.replace(',', r'\,')
    return ['-bsf:v', 'setts=pts=PTS:dts=DTS:duration=' + expression,
            '-t', f'{duration:.6f}']


def keep_segments(value, start=0, end=None):
    """返回源保留区间和所选输出内的位置；裁剪预览也使用源绝对时间。"""
    if end is None:
        if value is None:
            raise OutputCutsError('未提供保留区间终点')
        end = value['source_duration']
    start, end = _number(start, '区域起点'), _number(end, '区域终点')
    if start < 0 or end <= start:
        raise OutputCutsError('待合成区域起止时间无效')
    if value is not None:
        end = min(end, value['source_duration'])
    cursor, output, result = start, 0., []
    for row in (value or {}).get('ranges', []):
        left, right = max(start, row['start']), min(end, row['end'])
        if right <= left:
            continue
        if left > cursor + _EPS:
            length = left - cursor
            result.append({'start': cursor, 'end': left, 'output_start': output, 'output_end': output + length})
            output += length
        cursor = max(cursor, right)
    if cursor < end - _EPS:
        result.append({'start': cursor, 'end': end, 'output_start': output, 'output_end': output + end - cursor})
    return result


def split_interval(value, start, end):
    """字幕/章节与保留区间求交；输出时点始终相对完整成片。"""
    if value is None:
        return [{'start': start, 'end': end, 'output_start': start, 'output_end': end}] if end > start else []
    result = []
    for row in keep_segments(value):
        left, right = max(start, row['start']), min(end, row['end'])
        if right > left + _EPS:
            result.append({'start': left, 'end': right,
                           'output_start': row['output_start'] + left - row['start'],
                           'output_end': row['output_start'] + right - row['start']})
    return result


def map_time(value, seconds, bias='next'):
    """删除内部时点折叠到接缝；章节须用区间映射，不能只映射起点。"""
    if value is None:
        return seconds
    segments = keep_segments(value)
    for row in segments:
        if seconds < row['start']:
            return row['output_start']
        if seconds <= row['end']:
            return row['output_start'] + max(0., seconds - row['start'])
    return value['output_duration']


def unmap_time(value, seconds, bias='next'):
    """将旧成片（裁剪后）时间映射回原视频时间轴。

    ``map_time`` 用于章节/字幕从源到输出；编辑旧成片时需要反向映射，
    否则用户在旧成片 00:31:40 标记的范围会错误地再次按源 00:31:40
    删除。删除接缝处按 ``bias`` 选择接缝前或接缝后的源位置。
    """
    if value is None:
        return seconds
    seconds = _number(seconds, '成片时间')
    segments = keep_segments(value)
    if not segments:
        return 0.
    for index, row in enumerate(segments):
        if seconds < row['output_start'] - _EPS:
            return row['start']
        if seconds < row['output_end'] - _EPS:
            return row['start'] + max(0., min(row['end'] - row['start'], seconds - row['output_start']))
        if abs(seconds - row['output_end']) <= _EPS:
            if index + 1 < len(segments) and bias == 'next':
                return segments[index + 1]['start']
            return row['end']
        if index + 1 < len(segments) and seconds < segments[index + 1]['output_start']:
            return row['end'] if bias == 'previous' else segments[index + 1]['start']
    return segments[-1]['end']


def map_interval(value, start, end):
    """返回源区间在输出成片上的所有保留子区间，供时间轴编辑预览。"""
    start, end = _number(start, '源区间起点'), _number(end, '源区间终点')
    if end <= start:
        return []
    return split_interval(value, start, end)


def map_chapters(value, chapters):
    if value is None:
        return copy.deepcopy(chapters)
    ordered = sorted(chapters, key=lambda row: row.get('start_time', row.get('start', 0)))
    result = []
    for index, row in enumerate(ordered):
        start = float(row.get('start_time', row.get('start', 0)))
        next_start = (ordered[index + 1].get('start_time', ordered[index + 1].get('start', value['source_duration']))
                      if index + 1 < len(ordered) else value['source_duration'])
        end = float(row.get('end_time', row.get('end', next_start)) or next_start)
        retained = split_interval(value, start, end)
        if not retained:
            continue
        left, right = retained[0]['output_start'], retained[-1]['output_end']
        if result and abs(result[-1]['start_time'] - left) <= _EPS:
            continue
        item = {**row, 'start_time': round(left, 6), 'end_time': round(right, 6)}
        if 'start' in row:
            item['start'] = item['start_time']
        if 'end' in row:
            item['end'] = item['end_time']
        result.append(item)
    return result


def audio_cut_fade(value, row):
    """只淡化真实剪辑接缝；任意试片边界不能额外改变声音。"""
    if value is None:
        return ''
    length = row['end'] - row['start']
    fade = min(.005, length / 4)
    full = keep_segments(value)
    before = any(abs(row['start'] - item['start']) <= _EPS and item['start'] > _EPS for item in full)
    after = any(abs(row['end'] - item['end']) <= _EPS and item['end'] < value['source_duration'] - _EPS
                for item in full)
    return ((f',afade=t=in:st=0:d={fade:.6f}' if before else '')
            + (f',afade=t=out:st={max(0., length-fade):.6f}:d={fade:.6f}' if after else ''))


def filter_graph(value, video_label, audio_label=None, start=0, duration=None):
    """烧录与混音之后统一裁剪，避免字幕、评论、音频各走不同时间轴。"""
    end = start + duration if duration is not None else None
    segments = keep_segments(value, start, end)
    if not segments:
        raise OutputCutsError('所选试合成区域全部位于删除片段内，请重新选择区域',
                              code='OUTPUT_CUTS_PREVIEW_EMPTY')
    def label(item):
        return item if item.startswith('[') else f'[{item}]'
    count = len(segments)
    parts = []
    if count > 1:
        parts.append(label(video_label) + f'split={count}' + ''.join(f'[ocvs{i}]' for i in range(count)))
        if audio_label is not None:
            parts.append(label(audio_label) + f'asplit={count}' + ''.join(f'[ocas{i}]' for i in range(count)))
    for index, row in enumerate(segments):
        left, right = row['start'] - start, row['end'] - start
        vin = f'[ocvs{index}]' if count > 1 else label(video_label)
        parts.append(f'{vin}trim=start={left:.6f}:end={right:.6f},setpts=PTS-STARTPTS[ocv{index}]')
        if audio_label is not None:
            ain = f'[ocas{index}]' if count > 1 else label(audio_label)
            # 仅在真实剪辑接缝加 5ms 淡化，不交叉叠音、不挪动任何时间点。
            soften = audio_cut_fade(value, row)
            parts.append(f'{ain}atrim=start={left:.6f}:end={right:.6f},asetpts=PTS-STARTPTS{soften}[oca{index}]')
    if count > 1:
        inputs = ''.join(f'[ocv{i}]' + (f'[oca{i}]' if audio_label is not None else '') for i in range(count))
        parts.append(inputs + f'concat=n={count}:v=1:a={int(audio_label is not None)}[ocv]' + ('[oca]' if audio_label is not None else ''))
        video, audio = 'ocv', 'oca' if audio_label is not None else None
    else:
        video, audio = 'ocv0', 'oca0' if audio_label is not None else None
    return ';'.join(parts), label(video), label(audio) if audio else None, round(segments[-1]['output_end'], 6)
