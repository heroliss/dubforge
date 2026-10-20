"""
步骤 8: tts —— 批量配音
=======================
输入: 语义时间轴、逐句译文 + preprocess 的 vocal.wav (dynamic/stable 参考源)
后端: config tts.backend → indextts25 | gptsovits
句级缓存: artifacts/tts/{id}.wav 与旁置 .input 指纹匹配才复用 (包括工作台试配音频)
参考解析 (双维度, 移植自工作台验证过的语义):
  preset  → config 路径; stable → 指定源句原声; dynamic → vocal 按行时间切
  dynamic 默认使用本句原声，短句只提示；切片发生瞬时失败时才降级到稳定参考或预设参考。
"""
import json
import hashlib
import math
import datetime as dt
from dubforge.progress import RateProgress
from dubforge.execution_progress import report_stage
import stat
import subprocess
import tempfile
from pathlib import Path

from dubforge.backends import tts_gptsovits, tts_indextts25
from dubforge.errors import DubForgeError
from dubforge.domain import crud
from dubforge.utils.fs import atomic_replace, atomic_write_bytes
from dubforge.pipeline.line_settings import effective_config, project_parameters
from dubforge.pipeline import dubbing_edit, dubbing_session


class DubbingTimingError(DubForgeError):
    code = 'TTS_TIMING_INVALID'
    suggestion = '请在字幕/配音工作台修正本句的起止时间，或重新整理语义时间轴后再补齐配音。'


class _SentenceExecution:
    """真实单句回调进入统一进度；重复轮询、迟到回调不冒充模型活动。"""

    def __init__(self, ctx, code, label, *, completed=None, total=None, **metrics):
        self.ctx, self.code, self.label = ctx, code, label
        self.completed, self.total = completed, total
        self.open, self.value = True, None
        self.metrics = dict(metrics, sentence_progress=None, sentence_attempt=None,
            sentence_attempts=None, sentence_started_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self.metrics.setdefault('duration_factor', None)
        self._report()

    def _report(self):
        report_stage(self.ctx, self.code, self.label, completed=self.completed,
                     total=self.total, unit='句', **self.metrics)

    def attempt(self, index, total, factor):
        if self.open:
            self.value = None
            self.metrics.update(sentence_progress=None, sentence_attempt=index + 1,
                                sentence_attempts=total, duration_factor=factor)
            self._report()

    def update(self, value):
        if (not self.open or type(value) not in (int, float) or not math.isfinite(value)
                or not 0 <= value <= 1 or (self.value is not None and value <= self.value)):
            return False
        self.value = value
        self.metrics['sentence_progress'] = value
        self._report()
        return True

    def close(self):
        self.open = False


# 参考切片属于本地短操作，偶发的文件锁、ffmpeg 瞬时失败或服务重启时的
# IO 抖动不应直接让整句配音失败。次数保持很小，避免把真正无效的时间轴
# 长时间重试；动态参考最终仍会在 ``_resolve_ref`` 中有明确的降级路径。
REFERENCE_SLICE_ATTEMPTS = 3
# IndexTTS 2.5 的 s2mel 会把参考 mel 与目标 mel 拼接后进行多步 CFM 推理。
# 动态参考过长会让低显存设备的序列和显存压力急剧上升；默认 5 秒仍足以
# 提供稳定说话人/情感特征，实际值由步骤参数控制，不改变字幕时间轴。
DYNAMIC_REFERENCE_MAX_SECONDS = 5.0
# 模型音频身份与播放排布版本分离；调整窗口策略不能让历史精调声音全部失效。
TIMING_STRATEGY_VERSION = 'island-natural-first-v1'
REFERENCE_SNAPSHOT_KEY = 'resolved_references'
LEGACY_REFERENCE_KEY = '_legacy_dynamic_reference'


def _reference_line(deps, line_id):
    """参考说话人只取原始源句，不把字幕校对/自动排布的播放位置当成原声。"""
    if (type(line_id) not in (int, float) or not math.isfinite(line_id)
            or line_id < 1 or int(line_id) != line_id):
        raise tts_indextts25.ReferenceAudioError('参考句编号必须为正整数，请重新选择原声句子')
    line_id = int(line_id)
    from dubforge.pipeline.text_source import source_lines
    if not hasattr(deps, '_reference_source_lines'):
        deps._reference_source_lines = {row['id']: row for row in source_lines(deps)}
    row = deps._reference_source_lines.get(line_id)
    if row is None:
        raise tts_indextts25.ReferenceAudioError(f'参考原声第 {line_id} 句已不存在，请重新选择参考句')
    _validate_line_timing(row)
    return row


def _reference_descriptor(deps, line_id):
    row = _reference_line(deps, line_id)
    return dict(mode='sentence', line_id=row['id'], start=float(row['start']),
                duration=min(15., float(row['end']) - float(row['start'])))


def _validate_line_timing(line):
    try:
        start, end = float(line['start']), float(line['end'])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DubbingTimingError(
            f"句 {line.get('id', '?')} 时间轴无效：开始 {line.get('start')} 秒，结束 {line.get('end')} 秒；结束必须晚于开始且时间必须有效。") from None


def _write_voice_timing(ctx, task, pdir):
    """选句和全流程均保存报告；报告是诊断，消费者按当前选择即时重排。"""
    from dubforge.pipeline import voice_timing
    lines = dubbing_edit.production_lines(ctx.project, task['lines'], pdir)
    inputs = [{**line, 'source': line['source'] if line['source'] == 'original'
               else 'dub' if line.get('take') is not None else 'silent'} for line in lines]
    layout = voice_timing.plan(ctx.project, pdir, inputs, ctx.cfg)
    payload = {'schema': 2, 'strategy': voice_timing.STRATEGY_VERSION,
               **layout['timing'], 'mode': 'adaptive' if layout['timing']['ready'] else 'review_required'}
    path = ctx.artifact_dir() / 'voice_timing.json'
    ctx.write_json('voice_timing.json', payload)
    ctx.register_file('voice_timing.json', path, 'json')
    if payload['ready']:
        ctx.log(f"✅ 局部配音排布完成：{payload['shifted']} 句利用相邻空位；原文、译文与配音同步")
    elif payload.get('missing'):
        conflicts = (f"；另有 {payload['unresolved']} 句仍需处理时间冲突"
                     if payload['unresolved'] else '')
        ctx.log(f"⚠ 配音排布尚未完成：{payload['missing']} 句所选音频缺失或不可用，"
                f"等待有效音频后重新排布{conflicts}；已完成音频保留")
    else:
        ctx.log(f"⚠ 局部排布仍有 {payload['unresolved']} 句需缩短；其他可行窗口照常使用，不裁剪音频")
    return payload


def _repair_voice_timing(ctx, task, pdir, generated):
    """先滑动，再有限缩短；自动流程可修复旧配音，并保存同槽声音历史。"""
    from dubforge.pipeline import timing_repair, voice_timing
    from dubforge.pipeline.dubbing_policy import synthesis_project, record_generation_policy, repair_settings
    policy = ctx.task_data.get('policy') if ctx.task_data.get('scope') == 'policy' else None
    automatic = repair_settings(ctx.cfg, policy)['enabled']
    params = crud.project_params(ctx.project)
    if hasattr(ctx, '_tts_line_settings_snapshot'):
        params['tts_lines'] = ctx._tts_line_settings_snapshot
    frozen_project = ctx.project.model_copy(update={'params': json.dumps(params, ensure_ascii=False)})
    project = synthesis_project(frozen_project, policy) if policy else frozen_project
    targets = {row['id']: row for row in ctx.task_data.get('lines', [])}
    exhausted = set()
    while True:
        report_stage(ctx, 'placing_voices', '利用相邻空位自适应排布配音')
        report = _write_voice_timing(ctx, task, pdir)
        if (not voice_timing.settings(ctx.cfg)['enabled'] and not report.get('manual')) or report['ready']:
            return report
        if not automatic:
            return report
        if policy:
            # 新配音可能改变邻句的剩余容量；运行时重新核对，不只依赖预检时的冲突。
            for timing_row in report['lines']:
                line_id = timing_row['id']
                if not timing_row.get('needs_scale') or line_id in targets or line_id in exhausted:
                    continue
                line = next(row for row in task['lines'] if row['id'] == line_id)
                take = timing_row.get('take')
                try:
                    timing_repair.context(ctx.project, line, pdir, take, ctx.runtime_cfg)
                except DubForgeError:
                    exhausted.add(line_id)
                    continue
                from dubforge.pipeline.line_settings import identity
                row = dict(id=line_id, take=take, kind='repair', status='skipped',
                           timing_pending=True, identity=identity(line))
                ctx.task_data['lines'].append(row)
                targets[line_id] = row
                generated.add(line_id)
                ctx.save_task_data()
        candidates = [row for row in report['lines'] if row.get('needs_scale')
                      and row['id'] in generated and row['id'] not in exhausted]
        if not candidates:
            return report
        # 先解决不足最多的句子；一次真实模型结果后重新排布，避免对整个窗口过度缩速。
        timing = max(candidates, key=lambda row: float(row.get('seconds') or 0)
                     - (row['allocated_end'] - row['allocated_start']))
        line = next(row for row in task['lines'] if row['id'] == timing['id'])
        take = timing.get('take') or targets.get(line['id'], {}).get('take') or 1
        row = targets[line['id']]
        repair_only = row.get('kind') == 'repair'
        snapshot_repair = repair_only or row.get('live_updated')
        synth_project, get = project, effective_config(project, line, ctx.runtime_cfg)
        if snapshot_repair:
            synth_project, get = timing_repair.context(ctx.project, line, pdir, take, ctx.runtime_cfg)
        if get('tts.backend', 'indextts25') != 'indextts25':
            exhausted.add(line['id'])
            continue
        record = dubbing_edit.read_take(pdir, line, take)
        remaining = timing_repair.remaining_factors(record, get)
        attempted = row.get('timing_factors', [])
        if attempted:
            remaining = [f for f in remaining if f < min(attempted) - 1e-9]
        if not remaining:
            exhausted.add(line['id'])
            continue
        ctx.check_pause()
        ctx.check_cancel()
        factor = remaining[0]
        sentence = _SentenceExecution(ctx, 'shortening_voice', f'缩短第 {line["id"]} 句配音以消除时间冲突',
                                      current_id=line['id'], duration_factor=factor)
        row.update(status='running', timing_phase='shorten', duration_factor=factor)
        ctx.save_task_data()
        budget = max(.01, timing['allocated_end'] - timing['allocated_start'])
        ctx.log(f"句 {line['id']}：局部滑动后 {float(timing.get('seconds') or 0):.2f}s 仍超出 {budget:.2f}s 容量，按因子 {factor:g} 重试")
        from dubforge.pipeline import audio_adjustments
        adjustment = audio_adjustments.begin(pdir, line, take)
        # 保留同轮最短结果；模型长度不单调时不会把好版本换成长版本。
        try:
            with dubbing_session.use(ctx.project, line['id'], from_pipeline=True) as progress:
                def report_current(value, *, sentence=sentence, progress=progress):
                    if sentence.update(value):
                        progress(value)

                synth_line(synth_project, line, pdir, force=True, log=ctx.log, cfg_get=get,
                           take=take, check_cancel=ctx.check_cancel, progress=report_current,
                           on_attempt=sentence.attempt,
                           artifact_metadata=ctx.artifact_audit(),
                           timing={'available': budget, 'factors': [factor], 'keep_existing': True,
                                   'repair': automatic})
        except Exception as exc:
            row.update(status='failed', error=exc.message if isinstance(exc, DubForgeError) else str(exc))
            ctx.save_task_data()
            raise
        finally:
            sentence.close()
        if snapshot_repair:
            # 时间修复沿用声音的原始生成意图；项目后来换音色不应借修复混入。
            saved = dubbing_edit.read_take(pdir, line, take)
            for key in ('fingerprint', 'line_settings_mode', 'operation_id', 'live_revision'):
                if key in record:
                    saved[key] = record[key]
            output = dubbing_edit.take_path(pdir, line['id'], take)
            atomic_write_bytes(output.with_suffix('.take.json'), json.dumps(saved, ensure_ascii=False).encode())
            if record.get('fingerprint'):
                atomic_write_bytes(output.with_suffix('.input'), record['fingerprint'].encode())
        elif policy:
            record_generation_policy(pdir, line, take, policy, ctx.task_data['operation_id'])
        audio_adjustments.finish(pdir, line, take, adjustment)
        row.setdefault('timing_factors', []).append(factor)
        row.update(status='done', result_version=dubbing_edit.file_version(dubbing_edit.take_path(pdir, line['id'], take)))
        ctx.save_task_data()


def line_fingerprint(project, line, cfg_get=None, *, dependencies=None, _legacy=False, _legacy_service=False, _legacy_reading=False):
    """句级缓存绑定实际文本、时间和合成设置，工作台与批量配音共用。"""
    from dubforge.pipeline.dependencies import Dependencies
    deps = dependencies or Dependencies(project)
    cfg_get = effective_config(project, line, cfg_get or deps.cfg, params=project_parameters(project, deps))
    backend = cfg_get("tts.backend", "indextts25")
    settings = dict(cfg_get(f"tts.{backend}", {}) or {})
    settings = {k: cfg_get(f"tts.{backend}.{k}", v) for k, v in settings.items()
                if k not in {"preset_baseline", "source_preset", "project_dir", "duration_retry_factors", "request_timeout_seconds"}}
    if not _legacy_service:
        # 服务搬迁不改变已有音频；兼容旧指纹时才保留当时的连接地址。
        settings.pop('url', None)
    # 新增控件的空值与旧实现固定传入的值相同，不使已有音频无故失效。
    if backend == "indextts25":
        settings.pop(REFERENCE_SNAPSHOT_KEY, None)
        settings.pop(LEGACY_REFERENCE_KEY, None)
        for part in ('timbre', 'emo'):
            # 未使用的选择器不能令自定义文件/逐句参考的已有配音无故过期。
            if settings.get(f'{part}_mode') != 'stable':
                settings.pop(f'{part}_reference_line_id', None)
            else:
                settings.setdefault(f'{part}_reference_line_id', 1)
        for key in ("emo_text", "emo_random"):
            if not settings.get(key):
                settings.pop(key, None)
        from dubforge.pipeline import pronunciation
        reading = pronunciation.spoken_text(line, cfg_get)
        # 不启用时完全保持旧指纹；历史兼容路径也不能绕过非空注音。
        settings.pop(pronunciation.KEY, None)
        if _legacy_reading and reading != line['text']:
            settings[pronunciation.KEY] = reading
    references = {}
    for part in ("timbre", "emo"):
        if settings.get(f"{part}_mode") == "preset":
            path = Path(settings.get(f"{part}_audio") or "")
            if path.is_file():
                stat = path.stat()
                references[part] = (stat.st_size, stat.st_mtime_ns)
        elif settings.get(f"{part}_mode") in {"stable", "dynamic"}:
            art = deps.arts.get("preprocess/vocal.wav")
            references[part] = art.sha256 if art else "missing"
            if settings.get(f'{part}_mode') == 'stable':
                try:
                    reference = _reference_descriptor(deps, settings.get(f'{part}_reference_line_id', 1))
                except DubForgeError as exc:
                    # 列表仍要能显示失效的旧设置；真正合成时返回具体中文错误。
                    reference = {'unavailable': exc.code, 'line_id': settings.get(f'{part}_reference_line_id', 1)}
                references[part] = dict(source=references[part], **reference)
                vocal = deps.root / 'audio/vocal.wav'
                if 'start' in reference and vocal.is_file():
                    references[part]['source_version'] = _reference_fingerprint(vocal, reference['start'], reference['duration'])
    # 预估时长、间隙和提示标记不改变合成输入，不能让已合成音频失效。
    keys = ('id', 'text', 'start', 'end', 'duration', 'est_dur', 'gap', 'speed_flag') if _legacy else ('id', 'text', 'start', 'end')
    content = {k: line[k] for k in keys if k in line}
    if not _legacy_reading and 'voice_timing' in line:
        # 人工字幕/播放位置保留原TTS参考时钟，只影响铺轨，不要求重生成。
        content['start'], content['end'] = line['voice_timing']
    if backend == 'indextts25' and not _legacy_reading:
        content['text'] = reading
    data = {"line": content, "backend": backend, "settings": settings,
            "references": references, "timing_strategy": TIMING_STRATEGY_VERSION}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def line_cache_valid(project, line, pdir, cfg_get=None, *, dependencies=None, take=1):
    marker = dubbing_edit.take_path(pdir, line['id'], take).with_suffix('.input')
    try:
        wav = marker.with_suffix(".wav")
        if not wav.is_file() or not wav.stat().st_size:
            return False
        snapshot = {}
        if wav.with_suffix('.take.json').exists():
            snapshot = dubbing_edit.read_take(pdir, line, take)
            from dubforge.pipeline.line_settings import identity
            if not dubbing_edit.snapshot_current(pdir, line, take, snapshot=snapshot,
                    project=project, dependencies=dependencies, cfg_get=cfg_get):
                return False
            # 正常 dynamic 的指纹不绑定未启用的选择器；只有真正回退过的版本
            # 才核对当时的备用句，避免原声重分句后继续复用另一位说话人的切片。
            recorded_settings = snapshot.get('settings')
            recorded_refs = recorded_settings.get(REFERENCE_SNAPSHOT_KEY, {}) if isinstance(recorded_settings, dict) else {}
            if not isinstance(recorded_refs, dict):
                return False
            if any(value.get('fallback') for value in recorded_refs.values() if isinstance(value, dict)):
                from dubforge.pipeline.dependencies import Dependencies
                deps = dependencies or Dependencies(project)
                for part, value in recorded_refs.items():
                    if not isinstance(value, dict) or not value.get('fallback'):
                        continue
                    base = effective_config(project, line, cfg_get or deps.cfg, params=project_parameters(project, deps))
                    if base(f'tts.indextts25.{part}_mode') != 'dynamic':
                        continue
                    try:
                        if value.get('mode') == 'sentence':
                            current = _reference_descriptor(deps, 1)
                            if any(current[key] != value.get(key) for key in ('line_id', 'start', 'duration')):
                                return False
                            if value.get('source_version') != _reference_fingerprint(pdir / 'audio/vocal.wav',
                                    current['start'], current['duration']):
                                return False
                        elif value.get('mode') == 'preset':
                            if (value.get('path') != base(f'tts.indextts25.{part}_audio', '')
                                    or value.get('file_version') != dubbing_edit.file_version(Path(value['path']))):
                                return False
                    except (DubForgeError, OSError):
                        return False
        saved = marker.read_text(encoding="utf-8")
        candidates = [line]
        if 'voice_timing' in line and not wav.with_suffix('.take.json').exists():
            candidates.append({**line, 'start': line['voice_timing'][0], 'end': line['voice_timing'][1]})
        previous = dubbing_edit.recorded_line(project, line, snapshot)
        if previous and previous.get('text') != line['text']:
            # 严格旧指纹回算仅适用于真实朗读仍相同的字幕改写，不接受任意历史文字。
            previous = {**line, **previous}
            candidates.append(previous)
        from dubforge.pipeline.dependencies import Dependencies
        current_deps = dependencies or Dependencies(project)
        current_cfg = effective_config(project, line, cfg_get or current_deps.cfg,
                                       params=project_parameters(project, current_deps))
        actual_reading = dubbing_edit.current_reading(project, line, dependencies=current_deps, cfg_get=current_cfg)
        def candidate_cfg(key, default=None):
            value = current_cfg(key, default)
            # 回算旧字幕时仍使用现在实际传给模型的朗读文字，不能让旧字幕
            # 使已经迁移到当前句子的独立朗读设置意外失效。
            if key == 'tts.indextts25.pronunciation_text':
                return actual_reading
            if key == 'tts.indextts25' and isinstance(value, dict):
                return {**value, 'pronunciation_text': actual_reading}
            return value
        if any(saved == line_fingerprint(project, candidate, candidate_cfg,
                   dependencies=dependencies, _legacy=legacy, _legacy_service=service, _legacy_reading=legacy_reading)
               for candidate in candidates for legacy in (False, True) for service in (False, True)
               for legacy_reading in (False, True)):
            return True
        # 成功版本保存了旧地址时，可严格复算旧输入；只替换地址，仍核对
        # 当前文本、时间、音色参数和参考文件。不用未知旧 SHA 猜测有效性。
        recorded = snapshot.get('settings')
        if (snapshot.get('fingerprint') != saved or not isinstance(recorded, dict)
                or not isinstance(recorded.get('url'), str)):
            return False
        from dubforge.pipeline.dependencies import Dependencies
        deps = dependencies or Dependencies(project)
        base = candidate_cfg
        backend = base('tts.backend', 'indextts25')
        def historical_service(key, default=None):
            value = base(key, default)
            if key == f'tts.{backend}.url':
                return recorded['url']
            if key == f'tts.{backend}' and isinstance(value, dict):
                return {**value, 'url': recorded['url']}
            return value
        return any(saved == line_fingerprint(project, candidate, historical_service,
                   dependencies=deps, _legacy=legacy, _legacy_service=True, _legacy_reading=legacy_reading)
                   for candidate in candidates for legacy in (False, True) for legacy_reading in (False, True))
    except OSError:
        return False


def _reference_fingerprint(src: Path, start: float, dur: float) -> str:
    """参考切片只绑定原声版本和实际切片范围，不绑定句子或合成参数。"""
    if not math.isfinite(start) or not math.isfinite(dur) or start < 0 or dur <= 0:
        raise tts_indextts25.ReferenceAudioError(f'参考切片范围无效：开始 {start} 秒，时长 {dur} 秒')
    try:
        source = src.resolve()
        version = source.stat()
        if not stat.S_ISREG(version.st_mode):
            raise OSError('原声路径不是文件')
    except OSError as exc:
        raise tts_indextts25.ReferenceAudioError(
            f'无法读取参考原声文件：{src}', detail=str(exc)) from exc
    data = [str(source), version.st_size, version.st_mtime_ns, version.st_ino,
            f'{start:.6f}', f'{dur:.6f}', 32000, 1]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def _cut(src: Path, dst: Path, start: float, dur: float) -> Path:
    """输入标记匹配且有音频帧才复用；并发切片使用各自的临时文件。"""
    fingerprint = _reference_fingerprint(src, start, dur)
    marker = dst.with_suffix('.input')
    try:
        cached = marker.read_text(encoding='utf-8') == fingerprint
    except (OSError, UnicodeError):
        cached = False
    if cached and dst.is_file():
        try:
            tts_indextts25.validate_reference(dst, '缓存')
        except tts_indextts25.ReferenceAudioError:
            pass  # 无效旧缓存只在成功生成新参考后原子替换。
        else:
            return dst
    last_error = None
    for _attempt in range(REFERENCE_SLICE_ATTEMPTS):
        tmp = None
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                dir=dst.parent, prefix='.reference-', suffix='.wav.tmp', delete=False)
            tmp = Path(handle.name)
            handle.close()
            subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.6f}",
                            "-t", f"{dur:.6f}", "-ar", "32000", "-ac", "1",
                            "-f", "wav", str(tmp)], check=True, capture_output=True)
            tts_indextts25.validate_reference(tmp, '切片')
            if _reference_fingerprint(src, start, dur) != fingerprint:
                raise tts_indextts25.ReferenceAudioError('参考原声在切片期间发生变化，请重试')
            atomic_replace(tmp, dst)
            atomic_write_bytes(marker, fingerprint.encode())
            return dst
        except subprocess.CalledProcessError as exc:
            last_error = tts_indextts25.ReferenceAudioError(
                '无法从原声中读取有效参考切片',
                detail=exc.stderr.decode('utf-8', errors='replace') if exc.stderr else str(exc))
        except tts_indextts25.ReferenceAudioError as exc:
            last_error = exc
        except OSError as exc:
            last_error = tts_indextts25.ReferenceAudioError(
                '无法读取或保存参考切片', detail=str(exc))
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    # 保留最后一次的稳定错误码和详细原因；调用方可据此决定是否降级参考源。
    raise last_error or tts_indextts25.ReferenceAudioError('无法生成参考切片')


def _resolve_ref(ctx, mode: str, custom: str, line: dict,
                 vocal: Path, cache_dir: Path, tag: str, log=lambda m: None,
                 max_seconds: float = DYNAMIC_REFERENCE_MAX_SECONDS, *,
                 reference_line=None, frozen_reference=None, resolved=None, legacy_dynamic=False) -> Path:
    label = '音色' if tag == 'timbre' else '情感'

    def remember(value):
        if resolved is not None:
            resolved[tag] = value

    def sentence_reference():
        chosen = reference_line
        if chosen is None:
            raise tts_indextts25.ReferenceAudioError(f'{label}参考句尚未选择或已不存在，请重新选择原声句子')
        _validate_line_timing(chosen)
        start, duration = float(chosen['start']), min(15., float(chosen['end']) - float(chosen['start']))
        key = _reference_fingerprint(vocal, start, duration)
        path = _cut(vocal, cache_dir / f'{key}.wav', start, duration)
        remember(dict(mode='sentence', line_id=chosen['id'], start=start, duration=duration,
                      source_version=key, text=chosen.get('text', '')))
        log(f"句 {line['id']} {label}参考：原声第 {chosen['id']} 句 {start:.2f}–{start + duration:.2f} 秒 → {path}"
            + ('；模型最多使用前 15 秒' if float(chosen['end']) - start > 15 else ''))
        return path

    # 自动缩时必须重现该版本实际使用的片段，不能借当前新选择换说话人。
    if frozen_reference is not None:
        if not isinstance(frozen_reference, dict):
            raise tts_indextts25.ReferenceAudioError('历史配音的参考片段快照无效，请手动重新生成此句')
        if frozen_reference.get('mode') == 'preset':
            path = Path(frozen_reference.get('path') or '')
            if (not path.is_file() or frozen_reference.get('file_version') not in (None, dubbing_edit.file_version(path))):
                raise tts_indextts25.ReferenceAudioError('历史配音使用的参考文件已不存在或发生变化，请手动重新生成此句')
            remember(dict(frozen_reference))
            return path
        start, duration = frozen_reference.get('start'), frozen_reference.get('duration')
        if type(start) not in (int, float) or type(duration) not in (int, float):
            raise tts_indextts25.ReferenceAudioError('历史配音的参考时间不完整，请手动重新生成此句')
        key = _reference_fingerprint(vocal, start, duration)
        if key != frozen_reference.get('source_version'):
            raise tts_indextts25.ReferenceAudioError('历史配音的原声参考已变化，不能自动换参考缩时；请手动重新生成此句')
        path = _cut(vocal, cache_dir / f'{key}.wav', start, duration)
        remember(dict(frozen_reference))
        log(f"句 {line['id']} {label}参考：沿用此配音实际参考 {start:.2f}–{start + duration:.2f} 秒 → {path}")
        return path
    if mode == "preset":
        p = Path(custom or "")
        if not p.is_file():
            raise DubForgeError(
                f"{label}参考音频不存在: {p}", code="REF_MISSING",
                suggestion="请填写有效的参考音频路径，或选择指定句原声或本句原声。")
        remember(dict(mode='preset', path=str(p), file_version=dubbing_edit.file_version(p)))
        log(f"句 {line['id']} {label}参考：自定义音频 {p}")
        return p
    if mode == "stable":
        return sentence_reference()
    if mode == "dynamic":
        _validate_line_timing(line)
        reference_start, reference_end = line.get('voice_timing', [line['start'], line['end']])
        original_dur = reference_end - reference_start
        try:
            configured_max = float(max_seconds)
        except (TypeError, ValueError, OverflowError):
            configured_max = DYNAMIC_REFERENCE_MAX_SECONDS
        if not math.isfinite(configured_max):
            configured_max = DYNAMIC_REFERENCE_MAX_SECONDS
        configured_max = min(15.0, max(0.5, configured_max))
        dur = min(original_dur, configured_max)
        key = _reference_fingerprint(vocal, reference_start, dur)
        try:
            path = _cut(vocal, cache_dir / f'{key}.wav', reference_start, dur)
        except tts_indextts25.ReferenceAudioError as dynamic_error:
            if legacy_dynamic:
                # 仅历史 dynamic 无实际参考快照时重放旧合同；新制作永不走开头 8 秒。
                try:
                    old_key = _reference_fingerprint(vocal, 0., 8.)
                    old_path = _cut(vocal, cache_dir / f'{old_key}.wav', 0., 8.)
                except tts_indextts25.ReferenceAudioError:
                    preset = Path(custom or '')
                    if not preset.is_file():
                        raise dynamic_error
                    remember(dict(mode='preset', path=str(preset), file_version=dubbing_edit.file_version(preset), fallback=True))
                    return preset
                remember(dict(mode='legacy_opening', start=0., duration=8., source_version=old_key, fallback=True))
                log(f"句 {line['id']} {label}参考：沿用旧配音动态参考合同，动态切片失败后使用历史开头 8 秒")
                return old_path
            # 动态切片的短重试已耗尽；只明确回退配置的参考句，再用备用文件。
            # 不再假定开头 8 秒有人声或与本句为同一说话人。
            try:
                fallback = sentence_reference()
            except (DubForgeError, OSError) as reference_error:
                preset = Path(custom or '')
                if preset.is_file():
                    remember(dict(mode='preset', path=str(preset), file_version=dubbing_edit.file_version(preset), fallback=True))
                    log(f"句 {line['id']} {label}参考：动态切片和指定参考句均不可用，已回退备用音频 {preset}")
                    return preset
                raise dynamic_error from reference_error
            if resolved is not None:
                resolved[tag]['fallback'] = True
            log(f"句 {line['id']} {label}参考：本句切片失败，已回退指定参考句")
            return fallback
        remember(dict(mode='dynamic', start=float(reference_start), duration=float(dur), source_version=key))
        cap_note = (f"；原句参考 {original_dur:.2f}s，已限制为 {dur:.2f}s"
                    if original_dur > dur else '')
        log(f"句 {line['id']} {label}参考：本句 {reference_start:.2f}–{reference_end:.2f} 秒 → {path}"
            + cap_note
            + ('；参考不足 2 秒，若音色不稳可为本句指定同一说话人的较长参考' if dur < 2 else ''))
        return path
    raise DubForgeError(f"未知参考模式: {mode}", code="REF_MODE")


def synth_line(project, line: dict, pdir: Path, force: bool = False,
               log=lambda m: None, cfg_get=None, take=1, progress=None,
               on_attempt=lambda *args: None, can_retry=lambda: True, check_cancel=lambda: None,
               artifact_metadata=None, timing=None, output_path=None) -> dict:
    """合成单句 (工作台与批量步骤共用)。返回 {status, dur}"""
    from dubforge import config
    from dubforge.domain import crud

    if cfg_get is None:
        overrides = crud.project_step_params(project)
        cfg_get = lambda key, default=None: overrides.get(key, config.get(key, default))

    from dubforge import tts_service
    cfg_get = effective_config(project, line, cfg_get)
    with tts_service.use(cfg_get):
        return _synth_line(project, line, pdir, force, log, cfg_get, take, progress, on_attempt, can_retry, check_cancel,
                           artifact_metadata=artifact_metadata, timing=timing, output_path=output_path)


def _synth_line(project, line, pdir, force, log, cfg_get, take, progress, on_attempt, can_retry, check_cancel,
                *, artifact_metadata=None, timing=None, output_path=None):
    from dubforge.domain import crud

    out = output_path or dubbing_edit.take_path(pdir, line['id'], take)
    if (output_path is None and out.is_file() and out.stat().st_size > 0 and not force
            and line_cache_valid(project, line, pdir, cfg_get, take=take)):
        # 工作台先生成的音频也必须回到统一产物索引中。
        if f"tts/{out.name}" not in crud.latest_artifacts(project.id, ["tts"]):
            crud.add_artifact(project.id, "tts", "audio", out.name,
                              out, str(out.relative_to(pdir)), **(artifact_metadata or {}))
        return {"status": "skipped", "file": out}

    _validate_line_timing(line)
    from dubforge.pipeline.dependencies import Dependencies
    reference_deps = Dependencies(project)
    fingerprint = line_fingerprint(project, line, cfg_get, dependencies=reference_deps)
    backend = cfg_get("tts.backend", "indextts25")
    out.parent.mkdir(parents=True, exist_ok=True)
    duration_retry = None
    # 单句请求开始时固定一次超时；运行中保存的新值只影响下一句，
    # 避免同一远端请求的截止时间在合成过程中漂移。
    request_timeout_seconds = None
    if backend == "indextts25":
        try:
            request_timeout_seconds = max(0.0, float(
                cfg_get('tts.indextts25.request_timeout_seconds', 600) or 0))
        except (TypeError, ValueError, OverflowError):
            request_timeout_seconds = 600.0
        log(f"句 {line['id']} 单次配音请求超时={request_timeout_seconds:g}s；"
            "当前请求已固定，运行中修改从下一句生效")

    def section(path: str, keys: tuple[str, ...]) -> dict:
        base = dict(cfg_get(path, {}) or {})
        for key in keys:
            base[key] = cfg_get(f"{path}.{key}", base.get(key))
        return base

    if backend == "indextts25":
        cfg = section("tts.indextts25", (
            "url", "lang", "timbre_mode", "timbre_audio", "emo_mode", "emo_audio",
            "timbre_reference_line_id", "emo_reference_line_id",
            "emo_weight", "emo_vector", "emo_text", "emo_random", "duration_factor", "do_sample", "top_p",
            "top_k", "temperature", "length_penalty", "num_beams", "dynamic_reference_max_seconds",
            "repetition_penalty", "max_mel_tokens", "max_text_tokens_per_segment", "duration_retry_factors"))
        from dubforge.pipeline import pronunciation
        reading = pronunciation.spoken_text(line, cfg_get)
        cfg.pop(pronunciation.KEY, None)
        if reading != line['text']:
            cfg[pronunciation.KEY] = reading
        from dubforge.pipeline import tts_duration
        timing = timing or {}
        from dubforge.pipeline.dubbing_policy import repair_settings
        automatic = timing.get('repair', repair_settings(cfg_get)['enabled'])
        factors = tts_duration.attempt_factors(cfg['duration_factor'],
                                              cfg['duration_retry_factors'] if automatic else '')
        requested_factor = factors[0]
        if timing.get('defer') or not automatic:
            factors = factors[:1]
        if timing.get('factors') and automatic:
            factors = timing['factors']
        vocal = pdir / "audio" / "vocal.wav"
        # 参考缓存独立于句级合成指纹；相同原声版本与时间范围共用切片。
        cache_dir = pdir / "artifacts" / "tts" / "refs"
        dynamic_reference_max_seconds = cfg.get("dynamic_reference_max_seconds", DYNAMIC_REFERENCE_MAX_SECONDS)
        frozen_references = cfg_get(f'tts.indextts25.{REFERENCE_SNAPSHOT_KEY}', None)
        resolved_references = {}

        def reference_options(part):
            frozen = frozen_references.get(part) if isinstance(frozen_references, dict) else None
            selected = None
            if frozen is None and cfg.get(f'{part}_mode') in {'stable', 'dynamic'}:
                try:
                    selected_id = cfg_get(f'tts.indextts25.{part}_reference_line_id', 1) if cfg.get(f'{part}_mode') == 'stable' else 1
                    selected = _reference_line(reference_deps, selected_id)
                except DubForgeError:
                    if cfg.get(f'{part}_mode') == 'stable':
                        raise
                    # 逐句原声正常可用时不要求备用句存在；需要降级时再给明确错误。
            return dict(reference_line=selected, frozen_reference=frozen, resolved=resolved_references,
                        legacy_dynamic=bool(cfg_get(f'tts.indextts25.{LEGACY_REFERENCE_KEY}', False)))

        timbre = _resolve_ref(None, cfg.get("timbre_mode", "preset"),
                              cfg.get("timbre_audio", ""), line, vocal,
                              cache_dir, "timbre", log,
                              max_seconds=dynamic_reference_max_seconds, **reference_options('timbre'))
        emo_mode = cfg.get("emo_mode", "preset")
        emo_ref = None
        if emo_mode in ("preset", "stable", "dynamic"):
            emo_ref = _resolve_ref(None, emo_mode, cfg.get("emo_audio", ""),
                                   line, vocal, cache_dir, "emo", log,
                                   max_seconds=dynamic_reference_max_seconds, **reference_options('emo'))
        cfg[REFERENCE_SNAPSHOT_KEY] = resolved_references
        cfg.pop(LEGACY_REFERENCE_KEY, None)
        def generate(path, factor):
            active = True

            def forward(value):
                if active and progress is not None:
                    progress(value)

            try:
                return tts_indextts25.synth(
                    cfg.get("url", "http://127.0.0.1:7860/"),
                    reading, path, cfg.get("lang", "ZH"),
                    timbre_ref=timbre, emo_mode=emo_mode, emo_ref=emo_ref,
                    emo_weight=float(cfg.get("emo_weight", 1.0)),
                    emo_vector=cfg.get("emo_vector") or [],
                    emo_text=cfg.get("emo_text", ""), emo_random=cfg.get("emo_random", False),
                    duration_factor=factor,
                    advanced={k: cfg[k] for k in (
                        "do_sample", "top_p", "top_k", "temperature", "length_penalty",
                        "num_beams", "repetition_penalty", "max_mel_tokens",
                        "max_text_tokens_per_segment") if k in cfg},
                    log=log, progress=forward if progress is not None else None,
                    check_cancel=check_cancel,
                    timeout_seconds=request_timeout_seconds)
            finally:
                active = False  # 旧尝试的迟到进度不能污染下一次缩时重试。
        candidate_line = ({**line, 'adaptive_available_duration': timing['available']}
                          if 'available' in timing else line)
        existing = None
        if timing.get('keep_existing'):
            saved = dubbing_edit.read_take(pdir, line, take)
            existing = dict(saved.get('duration_retry') or {})
            # 旧版可有完整声音快照却无重试记录；保留其真实因子和音频，
            # 不能把本次候选因子或当前配置冒充旧声音的生成结果。
            used = existing.get('used_factor', (saved.get('settings') or {}).get('duration_factor'))
            if type(used) not in (int, float) or not math.isfinite(used) or not .5 <= used <= 2:
                raise DubForgeError('现有配音缺少可核验的实际时长因子，不能自动缩时；请先手动重新生成此句',
                                    code='TTS_TIMING_REPAIR_UNAVAILABLE')
            existing['used_factor'] = used
        duration_retry = tts_duration.synthesize(generate, out, candidate_line, factors,
            log=log, on_attempt=on_attempt, can_retry=can_retry,
            existing=existing)
        if timing.get('defer'):
            duration_retry['pending_layout'] = True
        # 版本快照记录实际采用的参数；输入指纹仍绑定用户请求配置。
        # 自动缩时仅作用于这次合成，不写回本句设置或影响后续合成的初始因子。
        cfg['duration_factor'] = duration_retry['used_factor']
        if cfg['duration_factor'] != requested_factor:
            log(f"句 {line['id']} 本次配音采用时长因子 {cfg['duration_factor']:g}；"
                f"设置仍为 {requested_factor:g}，下次合成从设置值开始")
    elif backend == "gptsovits":
        gcfg = section("tts.gptsovits", ("url", "character"))
        tts_gptsovits.synth(gcfg.get("url", "http://127.0.0.1:9880/"),
                            line["text"], out, "zh",
                            character=gcfg.get("character"), log=log)
    else:
        raise DubForgeError(f"未知 TTS 后端: {backend}", code="TTS_BACKEND")

    if output_path is None:
        crud.add_artifact(project.id, "tts", "audio", out.name,
                          out, str(out.relative_to(pdir)), **(artifact_metadata or {}))
    from dubforge.utils.fs import atomic_write_bytes
    atomic_write_bytes(out.with_suffix(".input"), fingerprint.encode())
    dubbing_edit.record_take(project, line, pdir, take, fingerprint, cfg if backend == 'indextts25' else gcfg,
                            duration_retry=duration_retry, output_path=output_path)
    return {"status": "synthesized", "file": out}


def run(ctx) -> None:
    report_stage(ctx, 'preparing_tts_service', '准备配音服务与运行资源')
    from dubforge import tts_service
    from dubforge.utils.gpu_memory import release_unused
    from dubforge.pipeline import live_dubbing
    live_dubbing.begin(ctx)
    release_unused(ctx.log)
    with tts_service.use(ctx.cfg):
        try:
            _run(ctx)
        except BaseException:
            task = getattr(ctx, '_tts_task', None)
            if task is not None:
                # 部分失败、请求超时或取消也更新已完成声音的派生报告。
                # 收尾只能排布，不得借此继续请求模型或遮蔽真正的失败原因。
                try:
                    _write_voice_timing(ctx, task, ctx.pdir)
                except Exception as exc:
                    try:
                        ctx.log(f'排布报告暂未更新，配音页将按当前音频重新计算：{exc}')
                    except Exception:
                        pass  # 日志也不可写时，仍保留原合成异常。
            raise
        finally:
            live_dubbing.end(ctx)


def _drain_live_updates(ctx, task, targets, generated):
    """当前请求结束后优先处理明确的逐句更新；失败不覆盖旧声音或阻断正常范围。"""
    from dubforge.pipeline import live_dubbing
    from dubforge.pipeline.dubbing_policy import repair_settings
    from dubforge.runner import StepCancelled
    import copy
    lines = {line['id']: line for line in task['lines']}
    while True:
        ctx.check_pause()
        request = live_dubbing.claim(ctx, lines)
        if request is None:
            return
        line = lines[request['id']]
        generation = request.get('kind') == 'generation'
        sentence = _SentenceExecution(ctx, 'synthesizing_queued_line' if generation else 'updating_line_settings',
                                     f'生成第 {line["id"]} 句到配音槽 {request["take"]}' if generation else
                                     f'更新第 {line["id"]} 句配音槽 {request["take"]}',
                                     current_id=line['id'], revision=request['revision'], take=request['take'])
        ctx.log(f'🎙 第 {line["id"]} 句从待配音队列生成到配音槽 {request["take"]}；成功后选用'
                if generation else
                f'🔄 第 {line["id"]} 句应用运行中精调 r{request["revision"]}，更新配音槽 {request["take"]}；成功后替换当前声音')
        # 本轮统一项目参数不能吞掉运行期间明确保存的本句意图。
        # 只叠加领取时的本句设置，其它制作参数仍使用本次执行快照。
        params = crud.project_params(ctx.project)
        params['tts_lines'] = {str(line['id']): dict(identity=line_settings_identity(line),
                                                   settings=request['settings'])}
        project = ctx.project.model_copy(update={'params': json.dumps(params, ensure_ascii=False)})
        original_task = copy.deepcopy(ctx.task_data)
        original_generated = set(generated)
        original_applied = copy.deepcopy(ctx._tts_applied_line_settings)
        candidate = live_dubbing.candidate_path(ctx, line)
        committed = False
        try:
            deps = ctx.dependencies()
            deps.project = project
            deps.require('workbench', line=line)
            cached = (generation and not request.get('force', True)
                      and dubbing_edit.playable(project, line, ctx.pdir, request['take'], dependencies=deps)
                      and line_cache_valid(project, line, ctx.pdir, ctx.runtime_cfg,
                                           dependencies=deps, take=request['take']))
            if not cached:
                with dubbing_session.use(ctx.project, line['id'], from_pipeline=True) as report:
                    def report_current(value, *, sentence=sentence, report=report):
                        if sentence.update(value):
                            report(value)

                    synth_line(project, line, ctx.pdir, force=True, log=ctx.log, cfg_get=ctx.runtime_cfg,
                               take=request['take'], progress=report_current, check_cancel=ctx.check_cancel,
                               on_attempt=sentence.attempt,
                               artifact_metadata=ctx.artifact_audit(),
                               timing={'defer': bool(ctx.cfg('tts.placement.enabled', True)),
                                       'repair': repair_settings(ctx.cfg, ctx.task_data.get('policy'))['enabled']},
                               output_path=candidate)
                record = json.loads(candidate.with_suffix('.take.json').read_text(encoding='utf-8'))
                record.update(line_settings_mode='preserve', live_revision=request['revision'],
                              operation_id=ctx.task_data.get('operation_id'))
                if generation:
                    record['generation_request_id'] = request.get('request_id')
                atomic_write_bytes(candidate.with_suffix('.take.json'),
                                   json.dumps(record, ensure_ascii=False).encode())
            version = dubbing_edit.file_version(
                dubbing_edit.take_path(ctx.pdir, line['id'], request['take']) if cached else candidate)
            if line['id'] in targets:
                targets[line['id']].update(status='skipped' if cached else 'done', take=request['take'],
                    live_revision=request['revision'], live_updated=True, timing_pending=not cached,
                    result_version=version)
            else:
                row = dict(id=line['id'], take=request['take'], status='skipped' if cached else 'done',
                           live_revision=request['revision'], live_updated=True, timing_pending=not cached,
                           result_version=version)
                ctx.task_data['lines'].append(row)
                targets[line['id']] = row
            if not cached:
                generated.add(line['id'])
            applied = ctx._tts_applied_line_settings
            if request['settings']:
                applied[str(line['id'])] = dict(identity=line_settings_identity(line), settings=request['settings'])
            else:
                applied.pop(str(line['id']), None)
            if cached:
                live_dubbing.complete_cached(ctx, request, line)
            else:
                live_dubbing.complete(ctx, request, line, candidate=candidate)
            committed = True
            ctx.log(f'✅ 第 {line["id"]} 句已' + ('复用' if cached else '生成到') + f'配音槽 {request["take"]}'
                    if generation else
                    f'✅ 第 {line["id"]} 句精调 r{request["revision"]} 已更新配音槽 {request["take"]}，请重新试听')
        except Exception as exc:
            if committed:
                # 事务之后仅剩日志；不能把已成功的选择降级或再次重做原目标。
                continue
            ctx.task_data = original_task
            targets.clear()
            targets.update({row['id']: row for row in ctx.task_data.get('lines', [])})
            generated.clear()
            generated.update(original_generated)
            ctx._tts_applied_line_settings = original_applied
            if isinstance(exc, StepCancelled):
                raise
            message = exc.message if isinstance(exc, DubForgeError) else str(exc)
            live_dubbing.finish(ctx, request, error=message)
            ctx.log(f'⚠ 第 {line["id"]} 句' + ('排队生成' if generation else '运行中精调') + f'失败：{message}；原选用配音保留')
            if generation and isinstance(exc, DubForgeError) and exc.code in {
                    'TTS_REQUEST_TIMEOUT', 'TTS_SERVICE_DRAINING', 'TTS_REQUEST_FAILED', 'SERVICE_OFFLINE'}:
                raise
        finally:
            sentence.close()
            live_dubbing.discard_candidate(candidate)


def line_settings_identity(line):
    from dubforge.pipeline.line_settings import identity
    return identity(line)


def _run(ctx) -> None:
    from dubforge.pipeline.dubbing_policy import repair_settings
    report_stage(ctx, 'preparing_dubbing', '读取逐句配音范围与生成策略')
    from dubforge.runner import StepCancelled
    from dubforge.pipeline.step_dubtask import build_task
    task = build_task(ctx)
    ctx._tts_task = task
    import copy
    frozen_line_settings = copy.deepcopy(crud.project_params(ctx.project).get('tts_lines', {}))
    ctx._tts_line_settings_snapshot = frozen_line_settings
    ctx._tts_applied_line_settings = copy.deepcopy(frozen_line_settings)
    if ctx.task_data.get('policy', {}).get('line_settings') == 'project':
        from dubforge.pipeline.pronunciation import preserve_records
        ctx._tts_applied_line_settings = preserve_records(frozen_line_settings)
    # 字数估时只能提示值得复核的句子，不能证明翻译错位或实际声音放不下。
    # 此时尚无 WAV，且相邻起点间隔没有计入前后滑动与联动排布的空余；
    # 不能在真实排布和有限缩时有机会执行前，用这个估算阻止批量制作。
    # 结构/时间有效性仍在模型请求前严格校验，最终冲突以真实音频为准。
    from dubforge.pipeline.translation_alignment import audit, blocking_issues, summary
    timing_audit = audit(task['lines'])
    report_stage(ctx, 'checking_dubbing_timing', '核对译文与配音时间轴', total=len(task['lines']), unit='句')
    review_items = blocking_issues(timing_audit)
    if timing_audit['issues']:
        ctx.log('⚠ ' + summary(timing_audit))
    if review_items:
        sample = []
        for issue in review_items[:8]:
            if issue.get('kind') == 'sequence_shift_suspect':
                sample.append('连续句估算朗读时长差异较大（第 ' +
                              '、'.join(str(item) for item in issue.get('ids', [])) + ' 句）')
                continue
            sample.append(f"第 {issue.get('id')} 句估算超出相邻起点间隔 "
                          f"{float(issue.get('excess', 0)):.2f} 秒")
        ctx.log(f"⚠ 配音前估时提示 {len(review_items)} 处：" + '；'.join(sample)
                + '。这是按文字长度的估算，不是实际配音时长，也不能据此认定跨行错位；'
                '将继续按所选策略处理，依据真实音频排布结果检查时间冲突。')
    pdir = ctx.pdir
    done = skipped = failed = 0
    generated = {row['id'] for row in getattr(ctx, 'task_data', {}).get('lines', [])
                 if row.get('timing_pending') and row.get('result_version')}
    selected = getattr(ctx, 'task_data', {}).get('scope') == 'selected'
    policy_run = getattr(ctx, 'task_data', {}).get('scope') == 'policy'
    if selected or policy_run:
        targets = {row['id']: row for row in ctx.task_data['lines']}
        lines = [{**line, 'take': targets[line['id']]['take']} for line in task['lines'] if line['id'] in targets]
    else:
        lines = dubbing_edit.synthesis_lines(ctx.project, [line for line in task['lines'] if line['text'].strip()], pdir)
        ctx.task_data = {'scope': 'all', 'lines': [
            {'id': line['id'], 'take': line.get('take') or 1, 'status': 'queued'} for line in lines]}
        targets = {row['id']: row for row in ctx.task_data['lines']}
    # 整批先核对时间轴，避免合成数百句后才发现某句原声切片为空。
    for line in lines:
        _validate_line_timing(line)
    ctx.save_task_data()
    from dubforge.pipeline import live_dubbing
    live_dubbing.ready(ctx)
    repair_count = sum(row.get('kind') == 'repair' for row in targets.values())
    ctx.log(f"本次配音范围：{len(lines)} 句" + ('（列表所选）' if selected else '（选用配音）')
            + (f'，其中 {repair_count} 句先复用声音，仅在滑动后仍不足时缩短' if repair_count else ''))
    if policy_run:
        ctx.log('本次配音策略：' + {'fill': '复用有效音频，只补缺失', 'changed': '更新参数已变化的配音',
                'all': '重新生成范围内全部配音'}[ctx.task_data['policy']['mode']]
                + ('；沿用本轮目标槽，仅补未完成句子' if ctx.task_data.get('resumed') else '')
                + ('；原文或译文已变化，重新确定本轮目标范围' if ctx.task_data.get('resume_reset') else ''))
        ctx.log('时间冲突处理：' + ('先滑动，仍不足时沿用原声音参数有限缩短，并保留同槽历史'
                                 if repair_settings(ctx.cfg, ctx.task_data['policy'])['enabled'] else '仅排布，不自动缩短声音'))
    from dubforge.pipeline.step_dubtask import estimate_seconds
    weights = [max(0.1, float(line.get("est_dur") or estimate_seconds(line["text"]))) for line in lines]
    total_weight = max(sum(weights), .1)
    progress = RateProgress(ctx, total_weight, eta_on_update=True)
    completed = 0.0
    for index, (line, weight) in enumerate(zip(lines, weights)):
        ctx.check_pause()
        ctx.check_cancel()
        _drain_live_updates(ctx, task, targets, generated)
        sentence = _SentenceExecution(ctx, 'synthesizing', f'处理第 {line["id"]} 句配音',
                                      completed=index, total=len(lines), current_id=line['id'], cached=skipped)
        cached = False
        row = targets[line['id']]
        # 未执行到的句子若已按运行中精调生成，不得再被原范围的旧参数覆盖。
        if row.get('live_updated'):
            skipped += 1
            completed += weight
            progress.update(completed, cached=True)
            continue
        row['status'] = 'running'
        ctx.save_task_data()
        try:
            take = row.get('take') or line.get('take') or 1
            # 本轮逐句覆盖意图与全局 force 分开；中断后已完成目标永不再次覆盖。
            force = (bool(row.get('force')) and not row.get('resume_completed') if policy_run
                     else bool(getattr(ctx, 'force', False)))
            if row.get('kind') == 'repair':
                # 旧配音先参与整窗排布；不要先用新参数重做一遍自然语音。
                if not dubbing_edit.playable(ctx.project, line, pdir, take):
                    raise DubForgeError('待修复配音已变化，请重新检查运行方案', code='DUB_TIMING_REPAIR_CONFLICT')
                generated.add(line['id'])
                row['timing_pending'] = True
                r = {'status': 'skipped'}
            elif not force and dubbing_edit.playable(ctx.project, line, pdir, take):
                r = {'status': 'skipped'}
            else:
                with dubbing_session.use(ctx.project, line['id'], from_pipeline=True) as report:
                    sample = {'open': True, 'value': 0.0, 'attempt': 0, 'total': 1}

                    def on_attempt(index, total, factor):
                        sample.update(value=0.0, attempt=index, total=total)
                        sentence.attempt(index, total, factor)
                        row.update(attempt=index, max_attempts=total, duration_factor=factor)
                        report(None, attempt=index, factor=factor)
                        ctx.save_task_data()

                    def can_retry():
                        # 停止时完成当前句的登记，不再额外调用模型；执行器随后处理取消。
                        try:
                            ctx.check_pause()
                            ctx.check_cancel()
                            return True
                        except StepCancelled:
                            return False

                    def report_current(value, *, sample=sample, report=report,
                                       completed=completed, weight=weight, sentence=sentence):
                        # 部分服务在结束后仍可能回调，不能影响下一句/下一轮任务。
                        if not sample['open'] or not sentence.update(value):
                            return
                        sample['value'] = max(sample['value'], min(1.0, max(0.0, value)))
                        report(sample['value'])
                        fraction = (sample['attempt'] + sample['value']) / sample['total']
                        ctx.report_progress(99 * (completed + weight * fraction) / total_weight)

                    try:
                        from dubforge.pipeline.dubbing_policy import synthesis_project
                        frozen_params = crud.project_params(ctx.project)
                        frozen_params['tts_lines'] = frozen_line_settings
                        frozen_project = ctx.project.model_copy(update={'params': json.dumps(frozen_params, ensure_ascii=False)})
                        synth_project = (synthesis_project(frozen_project, ctx.task_data['policy'])
                                         if policy_run else frozen_project)
                        r = synth_line(synth_project, line, pdir, force=force,
                                       log=ctx.log, cfg_get=ctx.runtime_cfg, take=take, progress=report_current,
                                       on_attempt=on_attempt, can_retry=can_retry, check_cancel=ctx.check_cancel,
                                       artifact_metadata=ctx.artifact_audit(),
                                       timing={'defer': bool(ctx.cfg('tts.placement.enabled', True)),
                                               'repair': repair_settings(ctx.cfg, ctx.task_data.get('policy'))['enabled']})
                    finally:
                        sample['open'] = False
                        sentence.close()
            if policy_run and r['status'] != 'skipped':
                from dubforge.pipeline.dubbing_policy import record_generation_policy
                record_generation_policy(pdir, line, take, ctx.task_data['policy'],
                                         ctx.task_data['operation_id'])
            if selected or (policy_run and row.get('kind') != 'repair'
                            and ctx.task_data['policy']['mode'] != 'fill' and not row.get('resume_completed')):
                # 逐句更新最新项目，避免后一句覆盖前一句的声源/版本选择。
                from dubforge.runner import runner
                with runner._start_lock:
                    ctx.project = crud.get_project(ctx.project.uid)
                    dubbing_edit.save_selection(ctx.project, line, 'dub', take)
            if r["status"] == "skipped":
                skipped += 1
                cached = True
            else:
                done += 1
                generated.add(line['id'])
                row['timing_pending'] = True
            row['status'] = 'skipped' if cached else 'done'
            row['result_version'] = dubbing_edit.file_version(dubbing_edit.take_path(pdir, line['id'], take))
            ctx.log(f"句 {line['id']}: {r['status']}")
        except StepCancelled:
            raise
        except Exception as e:  # noqa: BLE001 单句失败不阻断整批 (重跑补)
            if isinstance(e, DubForgeError) and e.code in {
                    'TTS_REQUEST_TIMEOUT', 'TTS_SERVICE_DRAINING', 'TTS_REQUEST_FAILED', 'SERVICE_OFFLINE'}:
                row.update(status='failed', error=e.message)
                ctx.save_task_data()
                raise  # 服务级故障停止整批，不能继续向同一阻塞服务提交几百句。
            failed += 1
            row.update(status='failed', error=e.message if isinstance(e, DubForgeError) else str(e))
            ctx.log(f"句 {line['id']} 失败: {type(e).__name__}: {e}")
            cached = True  # 失败句不贡献已成功合成的测速样本。
        completed += weight
        progress.update(completed, cached=cached)
        ctx.save_task_data()

    # 最后一轮检查与停止接收同锁，避免最后一句结束时保存的新精调被遗漏。
    from dubforge.pipeline import live_dubbing
    while True:
        _drain_live_updates(ctx, task, targets, generated)
        if live_dubbing.close_if_empty(ctx):
            break
    failed = sum(row.get('status') == 'failed' for row in targets.values())
    live_state = live_dubbing.view(crud.get_project(ctx.project.uid))
    failed += sum(row.get('kind') == 'generation' and row.get('state') == 'failed'
                  and targets.get(int(ident), {}).get('status') != 'failed'
                  for ident, row in live_state['lines'].items())
    if failed and not any(row.get('status') in {'done', 'skipped'} for row in targets.values()):
        raise DubForgeError(f"全部 {failed} 句合成失败", code="TTS_ALL_FAILED",
                            suggestion="查看逐句日志定位首错; 常见: 服务未启动/参考音频无效。")
    if failed:
        raise DubForgeError(f"仍有 {failed} 句配音失败，已生成的音频保留", code="TTS_PARTIAL_FAILED",
                            suggestion="查看逐句日志后重试，全部句子完成后再合成成片。")
    ctx.project = crud.get_project(ctx.project.uid)
    timing_report = _repair_voice_timing(ctx, task, pdir, generated)
    unresolved = set(timing_report.get('unresolved_ids', []))
    final_timing = {row['id']: row for row in timing_report['lines']}
    base_lines = {line['id']: line for line in task['lines']}
    for row in ctx.task_data.get('lines', []):
        if row['id'] in generated:
            row['timing_pending'] = row['id'] in unresolved
            timing = final_timing.get(row['id'])
            if timing and timing.get('take') == row.get('take'):
                line = base_lines[row['id']]
                record = dubbing_edit.read_take(pdir, line, row['take'])
                if record.get('duration_retry') and (timing_report['enabled'] or timing.get('manual_active')):
                    record['duration_retry'].update(
                        pending_layout=False,
                        available=round(timing['allocated_end'] - timing['allocated_start'], 6),
                        overrun=bool(timing['needs_scale']))
                    atomic_write_bytes(dubbing_edit.take_path(pdir, row['id'], row['take']).with_suffix('.take.json'),
                                       json.dumps(record, ensure_ascii=False).encode())
    ctx.save_task_data()
    remaining = unresolved if policy_run else unresolved & generated
    if remaining:
        prefix = ('滑动与已配置的缩短重试后，仍有配音无法放入局部窗口：第 '
                  if repair_settings(ctx.cfg, ctx.task_data.get('policy'))['enabled'] else
                  '自动缩时修复已关闭，当前排布仍有配音时间冲突：第 ')
        raise DubForgeError(prefix
                            + '、'.join(map(str, sorted(remaining)[:12])) + ' 句',
                            code='DUB_TIMING_UNRESOLVED',
                            suggestion='已保留最短完整音频与同槽缩时前记录。请查看剩余冲突，调整译文、人工位置、滑动范围或缩短因子后继续；不会裁切音频或强行生成重叠成片。')
    report_stage(ctx, 'saving_dubbing', '保存配音选择与运行回执', completed=len(targets), total=len(targets), unit='句',
                 generated=done, cached=skipped)
    if policy_run:
        from dubforge.chain_recovery import refresh_dubbing_selection
        refresh_dubbing_selection(ctx.project.uid, ctx.task_data)
        ctx.save_task_data()
    if any(row.get('live_updated') for row in ctx.task_data.get('lines', [])) and not selected:
        # 初始执行快照仍保留；成功基线只记录真正应用的逐句设置，
        # 不把“仅保存”或失败精调冒充已生成声音。
        from dubforge.pipeline.dependencies import Dependencies
        from dubforge.pipeline.stale_explanation import capture
        params = crud.project_params(ctx.project)
        params['tts_lines'] = ctx._tts_applied_line_settings
        project = ctx.project.model_copy(update={'params': json.dumps(params, ensure_ascii=False)})
        deps = Dependencies(project, settings=ctx._config_snapshot,
                            overrides=ctx._project_step_params_snapshot)
        ctx._completed_input_snapshot = capture(deps, 'tts')
    shortened = [row for row in ctx.task_data.get('lines', []) if row.get('timing_factors')]
    ctx.log(f"✅ 批量配音完成: 常规新合成 {done} / 初始复用 {skipped} / "
            f"缩时处理 {len(shortened)} 句（{sum(len(row['timing_factors']) for row in shortened)} 次） / 失败 {failed}")
