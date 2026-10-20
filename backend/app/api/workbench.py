"""配音试配 API：与批量配音共用输入、项目参数和句级缓存
- state: 逐句列表 (缓存/满意标记/语速) + 视频/背景音可用性
- synthesize: 所选/单句合成提交到同一后台步骤任务
- confirm: 满意标记 (artifacts/tts/workbench_confirm.json; 批量合成自动复用已试配句)
- 音视频流: 支持 Range (浏览器拖动)
"""
import datetime
import json
from functools import lru_cache
from pathlib import Path

from dubforge import config, tts_service
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.step_tts import line_cache_valid, line_fingerprint
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.video_source import resolve_video
from dubforge.pipeline import line_settings, dubbing_session, dubbing_edit, subtitle_document
from dubforge.utils.fs import atomic_replace
from fastapi import APIRouter, Query
from fastapi.responses import Response
from app.media import MediaFileResponse, MediaSnapshotResponse
from dubforge.runner import runner
from dubforge.pipeline import dubbing_task
from dubforge.pipeline.dubbing_placement import build_plan

router = APIRouter(prefix="/api/projects/{uid}/workbench", tags=["workbench"])


@router.post('/subtitle-preview')
def subtitle_preview(uid: str, body: dict):
    """只预览当前草稿样式，不保存参数，也不要求生成配音或先导出字幕。"""
    from dubforge.errors import ConfigError
    from dubforge.pipeline.step_params import validate_numeric_updates
    from dubforge.pipeline.step_subtitles import build_tracks
    from dubforge.pipeline.subtitle_style import PREVIEW_KEYS, STYLE_DEFAULTS, preview_frame, validate_style
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    settings = body.get('settings', {})
    seconds = body.get('seconds', 0)
    if not isinstance(settings, dict) or set(settings) - PREVIEW_KEYS or type(seconds) not in (int, float):
        raise ConfigError('字幕预览参数无效')
    validate_numeric_updates(settings)
    deps = Dependencies(project)
    deps.overrides = {**deps.overrides, **settings}
    validate_style({key: deps.cfg(key, default) for key, default in STYLE_DEFAULTS.items()})
    track = deps.cfg('assemble.subtitle_track', 'trans')
    if track not in {'source', 'trans', 'bilingual'}:
        raise ConfigError('请选择有效的烧录字幕类型')
    # 复用字幕导出的依赖规则；选择原文时不要求译文。
    deps.overrides['subtitles.mode'] = 'target' if track == 'trans' else track
    deps.require('subtitles')
    video = resolve_video(project, deps.root)
    if video is None:
        raise DubForgeError('没有可用的源视频', code='VIDEO_MISSING')
    tracks, _ = build_tracks(deps)
    lines = tracks[track + '.srt']
    data = preview_frame(video, lines, deps.cfg, seconds)
    return Response(data, media_type='image/png', headers={
        'Cache-Control': 'no-store',
        'X-Subtitle-Active': str(sum(line.start <= seconds < line.end for line in lines)),
    })


def _confirm_file(pdir) -> Path:
    return pdir / "artifacts" / "tts" / "workbench_confirm.json"


def _legacy_confirm_file(pdir) -> Path:
    """兼容早期版本写在项目根目录的工作台状态。"""
    return pdir / "workbench_confirm.json"


def _load_confirm(pdir) -> dict:
    try:
        target = _confirm_file(pdir)
        if not target.is_file():
            target = _legacy_confirm_file(pdir)
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_confirm(pdir, d: dict) -> None:
    target = _confirm_file(pdir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        atomic_replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _sample_lines(project, pdir, deps):
    """试听与成片共同读取已选择的声源和配音版本。"""
    confirms = _load_confirm(pdir)
    params = crud.project_params(project)
    takes_index = dubbing_edit.take_index(pdir)
    from dubforge.pipeline.audio_adjustments import view as adjustment_view
    # 每次请求只列一次索引；多数句子没有缩时历史，避免逐槽试读缺失文件。
    adjustment_indexes = {path.stem for path in (pdir / 'artifacts' / 'tts' / 'adjustments').glob('*.json')}
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA
    from dubforge.pipeline import pronunciation
    labels = {f.path: f.label for f in STEP_PARAM_SCHEMA['tts']}
    labels[pronunciation.PATH] = pronunciation.FIELD.label
    from dubforge.pipeline.step_dubtask import build_task
    try:
        lines = build_task(deps, editing=True)['lines']
    except (DubForgeError, OSError, ValueError):
        lines = []
    result = []
    from dubforge.pipeline import live_dubbing
    config_execution = live_dubbing.view(project)
    revisions = params.get(live_dubbing.REVISIONS, {})
    for line in lines:
        blockers = deps.unready('workbench', line=line)
        chosen = dubbing_edit.selection(project, line, pdir, dependencies=deps, params=params)
        current_fingerprint = line_fingerprint(project, line, dependencies=deps)
        versions = []
        take_versions = []
        for take in takes_index.get(line['id'], []):
            wav = dubbing_edit.take_path(pdir, line['id'], take)
            version = _file_version(wav)
            take_versions.append((take, version))
            data = dubbing_edit.read_take(pdir, line, take)
            # 带 identity/version 快照的工作台音频已经自证对应当前句子和文件，
            # 不应被 split/translate 步骤因无关设置变 stale 的状态挡成“预览无声”。
            # 没有快照的旧音频仍沿用依赖门禁，避免误试听无法核验来源的历史产物。
            has_current_snapshot = dubbing_edit.snapshot_current(pdir, line, take, snapshot=data, version=version,
                                                                project=project, dependencies=deps)
            valid = (has_current_snapshot or (deps.current('split') and deps.current('translate'))) \
                and dubbing_edit.playable(project, line, pdir, take, dependencies=deps, snapshot=data, version=version)
            saved = confirms.get(dubbing_edit.confirm_key(line['id'], take), {})
            fingerprint = data.get('fingerprint') or current_fingerprint
            from dubforge.pipeline.dubbing_changes import view as pending_changes
            changes = pending_changes(project, line, pdir, take, dependencies=deps, snapshot=data,
                                      version=version, valid=valid, current_fingerprint=current_fingerprint)
            versions.append({'id': take, 'exists': valid, 'stale': not valid,
                'adjustment_history': adjustment_view(pdir, line, take)
                    if valid and f"{line['id']}_v{take}" in adjustment_indexes else None,
                'line_settings_mode': data.get('line_settings_mode', 'preserve'),
                'duration_retry': data.get('duration_retry'),
                **changes,
                'url': _versioned_url(f"/api/projects/{project.uid}/workbench/audio/line/{line['id']}?take={take}", wav),
                'seconds': _wav_dur(wav) if valid else None,
                'confirmed': bool(valid and saved.get('fingerprint') == fingerprint and saved.get('version') == version)})
        current = next((v for v in versions if v['id'] == chosen['take']), None)
        own = line_settings.overrides(project, line)
        result.append({**line,
            'synthesis_revision': dubbing_task.confirmation_revision(project, line, pdir, dependencies=deps,
                selected=chosen, versions=take_versions, confirms=confirms, fingerprint=current_fingerprint),
            'selection': chosen, 'versions': versions,
            'unready_inputs': blockers, 'customized': bool(own),
            'custom_params': [{'path': k, 'label': labels.get(k, k), 'value': v} for k, v in own.items()],
            'wav': current or {'exists': False, 'stale': chosen['take'] is not None, 'url': None, 'seconds': None},
            'confirmed': bool(current and current['confirmed'])})
        result[-1]['config_execution'] = live_dubbing.line_view(config_execution, line['id'], revisions.get(str(line['id']), 0))
    return result


@router.get('/reference-lines')
def reference_lines(uid: str):
    """参考句选择只读源句，避免加载全量配音版本或要求译文先就绪。"""
    from dubforge.pipeline.text_source import source_lines
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    path = deps.path('split', 'split.txt')
    if path is None or not path.is_file():
        return {'lines': []}
    return {'lines': source_lines(deps)}


@router.get('/reference-audio')
def reference_audio(uid: str, line_id: int = Query(ge=1),
                    max_seconds: float = Query(default=15., ge=0.5, le=15.)):
    """试听分离原声并带少量上下文；实际模型参考区间保持不变。"""
    from dubforge.pipeline.text_source import source_lines
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    lines = source_lines(deps)
    line = next((row for row in lines if row['id'] == line_id), None)
    if line is None:
        raise DubForgeError(f'参考原声第 {line_id} 句已不存在，请重新选择参考句',
                            code='LINE_NOT_FOUND', suggestion='请重新选择音色或情感参考句。')
    vocal = crud.project_dir(uid) / 'audio' / 'vocal.wav'
    if not vocal.is_file():
        raise DubForgeError('尚无可试听的分离人声，请先完成「音轨分离」',
                            code='REF_MISSING', suggestion='完成音轨分离后，即可试听参考原声。')
    if not deps.current('preprocess'):
        raise DubForgeError('分离人声已过期或正在更新，请先完成「音轨分离」',
                            code='REFERENCE_AUDIO_STALE', suggestion='请更新音轨分离结果后再试听。')
    duration = _wav_dur(vocal)
    if duration is None:
        raise DubForgeError('无法读取参考原声时长，请检查音轨分离结果后重试',
                            code='REFERENCE_AUDIO_INVALID')
    reference_start = float(line['start'])
    reference_end = min(float(line['end']), reference_start + max_seconds, duration)
    if reference_end <= reference_start:
        raise DubForgeError('该句超出参考原声音轨范围，请检查原文时间和音轨分离结果',
                            code='REFERENCE_AUDIO_RANGE')
    # 语义句界可能落在发音尾部，试听可跨入少量相邻原声；不能把缓冲用于实际合成参考。
    start = max(0., reference_start - .15)
    end = min(duration, reference_end + .25)
    return {'url': _versioned_url(f'/api/projects/{uid}/workbench/audio/vocal', vocal),
            'start': round(start, 6), 'end': round(end, 6),
            'reference_start': reference_start, 'reference_end': reference_end,
            'line_id': line_id}


@router.get("")
def state(uid: str, probe: bool = True):
    from dubforge.pipeline import live_dubbing
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    pdir = crud.project_dir(uid)
    overrides = crud.project_step_params(project)
    def cfg_value(key: str, default=None):
        return overrides.get(key, config.get(key, default))

    deps = Dependencies(project)
    blockers = deps.unready("workbench")
    lines = _sample_lines(project, pdir, deps)
    from dubforge.pipeline import audio_post
    audio_gains = audio_post.line_view(project, lines, pdir, dependencies=deps)
    lines = [{**line, 'audio_gain_db': audio_gains[str(line['id'])]['effective_gain_db'],
              'audio_gain_state': audio_gains[str(line['id'])]} for line in lines]
    task_error = None
    if not lines and not blockers:
        try:
            deps.dubbing_lines(required=True)
        except DubForgeError as exc:
            task_error = str(exc)

    # 服务探测可从页面关键路径中拆出; 工作台先展示内容, 再异步更新在线状态。

    video = resolve_video(project, pdir)
    from dubforge.pipeline import voice_timing
    layout = voice_timing.plan(project, pdir, [{**line,
                                'source': line['selection']['source'] if line['selection']['source'] == 'original'
                                else 'dub' if line['wav']['exists'] and line['selection']['take'] is not None else 'silent',
                                'take': line['selection']['take'], 'seconds': line['wav']['seconds'],
                                'media_key': line['wav']['url']} for line in lines], deps.cfg,
                              video_duration=_video_duration(video))
    playback = {row['id']: row for row in layout['inputs']}
    timing_rows = {row['id']: row for row in layout['timing']['lines']}
    lines = [{**line, 'playback_start': playback[line['id']]['start'],
              'playback_end': playback[line['id']]['end'],
              'timing': timing_rows[line['id']]} for line in lines]
    def audio_track(name):
        path = pdir / 'audio' / name
        found = deps.current('preprocess') and path.is_file()
        return {'found': found, 'url': _versioned_url(
            f'/api/projects/{uid}/workbench/audio/{name.removesuffix(".wav")}', path) if found else None}
    reserved_takes = live_dubbing.reserved_takes_by_line(project)
    return {
        "lines": lines,
        "synthesis_slots": sorted({take for line in lines
                                   for take in [line['selection']['take'], *[v['id'] for v in line['versions']]]
                                   if type(take) is int and 1 <= take <= 10000}
                                  | {take for values in reserved_takes.values() for take in values}),
        "synthesis_reserved_takes": {str(ident): sorted(values) for ident, values in reserved_takes.items()},
        "placement": layout['placement'],
        "timing": layout['timing'],
        "edit_blockers": deps.unready('translate'),
        "orphaned_edits": sorted(set(subtitle_document.records(project)) - {str(l['id']) for l in lines}) if lines else [],
        "subtitles": {
            'status': deps.status('subtitles'), 'fresh': deps.fresh('subtitles'),
            'error': deps.steps['subtitles'].error_message or None,
            'unready_inputs': deps.unready('subtitles'),
            'files': [{'name': a.name, 'path': str(deps.path('subtitles', a.name)),
                       'url': _versioned_url(f'/api/projects/{uid}/steps/subtitles/artifacts/{a.name}/raw', deps.path('subtitles', a.name))}
                      for a in deps.arts.values() if a.step_key == 'subtitles' and a.name.endswith('.srt')
                      and deps.path('subtitles', a.name) and deps.path('subtitles', a.name).is_file()],
        },
        **dubbing_session.status(uid),
        "synthesis_task": dubbing_task.view(project.id),
        "line_config_execution": live_dubbing.view(project),
        "unready_inputs": blockers,
        "task_error": task_error,
        "video": {"found": video is not None,
                  "url": _versioned_url(
                      f"/api/projects/{uid}/workbench/video", video)
                  if video else None},
        "bg": audio_track('background.wav'),
        "vocal": audio_track('vocal.wav'),
        "original": audio_track('raw.wav'),
        "line_settings_supported": cfg_value('tts.backend') == 'indextts25',
        "service": tts_service.status(cfg_value, probe=probe),
        "completed": deps.current('tts'),
        "mix": {'voice': cfg_value('assemble.voice_volume', 1.0), 'background': cfg_value('assemble.bgm_volume', 1.0)},
        "counts": {
            "total": len(lines),
            "synthesized": sum(1 for l in lines if l["wav"]["exists"]),
            "confirmed": sum(1 for l in lines if l["confirmed"]),
        },
    }


def _service_config(uid: str):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    overrides = crud.project_step_params(project)

    def cfg_value(key: str, default=None):
        return overrides.get(key, config.get(key, default))

    return cfg_value


@router.get("/service")
def service(uid: str):
    """单独探测服务，避免离线服务阻塞配音页面内容。"""
    return tts_service.status(_service_config(uid))


@router.post("/service/start")
def start_service(uid: str):
    return tts_service.start(_service_config(uid))


@router.post("/service/stop")
def stop_service(uid: str):
    return tts_service.stop(_service_config(uid))


@router.get("/service/log")
def service_log(uid: str):
    return tts_service.read_log(_service_config(uid))


def _wav_dur(p):
    try:
        from dubforge.pipeline.tts_duration import audio_seconds
        return audio_seconds(p)
    except (DubForgeError, OSError):
        return None


@lru_cache(maxsize=64)
def _cached_video_duration(path, version):
    # 同一视频轮询只探测一次；同名文件替换后重新读取，不按字幕结尾猜视频时长。
    from dubforge.pipeline.step_assemble import _ffprobe_duration
    return _ffprobe_duration(Path(path)) or None


def _video_duration(path):
    if path is None:
        return None
    try:
        return _cached_video_duration(str(path), _file_version(path))
    except OSError:
        return None


def _file_version(p: Path) -> str:
    """为同名媒体生成轻量版本号，避免工作台内存音频缓存复用旧文件。"""
    try:
        st = p.stat()
        return f"{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        return "0"


def _versioned_url(url: str, p: Path) -> str:
    return f"{url}{'&' if '?' in url else '?'}v={_file_version(p)}"


def _line_context(uid, line_id):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f'项目不存在: {uid}', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    line = next((l for l in deps.dubbing_lines(required=True) if l['id'] == line_id), None)
    if line is None:
        raise DubForgeError(f'句号不存在: {line_id}', code='LINE_NOT_FOUND')
    return project, deps, line


@router.put('/lines/{line_id}/text')
def edit_text(uid: str, line_id: int, body: dict):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    with dubbing_session.edit(project):
        project = crud.get_project(uid)
        subtitle_document.save(project, Dependencies(project), line_id, body)
    return {'ok': True}


def _stale_playback_outputs(project):
    """声音文件与句级身份不变，仅字幕和使用配音的成片需要重做。"""
    for key in ('subtitles', 'assemble'):
        step = crud.get_step(project.id, key)
        if step and step.status == 'done':
            step.status = 'stale'
            crud.save_step(step)


@router.put('/lines/{line_id}/timing')
def edit_playback_timing(uid: str, line_id: int, body: dict):
    from dubforge.pipeline import playback_timing, voice_timing
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    with dubbing_session.edit(project, paths={'playback_timing_edits'}):
        project = crud.get_project(uid)
        deps = Dependencies(project)
        lines = dubbing_edit.production_lines(project, deps.dubbing_lines(required=True), deps.root,
                                              dependencies=deps)
        line = next((row for row in lines if row['id'] == line_id), None)
        if line is None:
            raise DubForgeError('句子已不存在，请刷新列表', code='LINE_NOT_FOUND')
        path = dubbing_edit.take_path(deps.root, line_id, line['take']) if line.get('take') else None
        valid = line['source'] == 'dub' and path and dubbing_edit.playable(
            project, line, deps.root, line['take'], dependencies=deps)
        if not body.get('reset') and not valid:
            raise DubForgeError('请先选用有效配音，再调整本句播放时间', code='PLAYBACK_TIMING_INVALID')
        from dubforge.pipeline.tts_duration import audio_seconds
        layout = voice_timing.plan(project, deps.root, [
            {**row, 'source': row['source'] if row['source'] == 'original'
             else 'dub' if row.get('take') is not None else 'silent'} for row in lines], deps.cfg)
        previous = json.dumps(playback_timing.records(project), sort_keys=True)
        playback_timing.save(project, line, body, seconds=audio_seconds(path) if valid else None,
                             video_duration=layout['placement']['video_duration'],
                             lines=layout['inputs'],
                             audio_version=dubbing_edit.file_version(path) if path else None)
        if json.dumps(playback_timing.records(project), sort_keys=True) != previous:
            _stale_playback_outputs(project)
    return {'ok': True}


@router.put('/lines/{line_id}/audio-adjustment')
def restore_audio_adjustment(uid: str, line_id: int, body: dict):
    from dubforge.pipeline import audio_adjustments
    project, _, _ = _line_context(uid, line_id)
    with dubbing_session.edit(project):
        project, deps, line = _line_context(uid, line_id)
        chosen = dubbing_edit.selection(project, line, deps.root, dependencies=deps)
        if chosen['source'] != 'dub' or not chosen['take'] or not dubbing_edit.playable(
                project, line, deps.root, chosen['take'], dependencies=deps):
            raise DubForgeError('请先选用有效配音，再恢复自动缩时记录', code='AUDIO_ADJUSTMENT_INVALID')
        if audio_adjustments.restore(project, line, deps.root, chosen['take'], body):
            _stale_playback_outputs(project)
    return {'ok': True}


@router.post('/discard_orphaned_edits')
def discard_orphaned_edits(uid: str):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    with dubbing_session.edit(project):
        project = crud.get_project(uid)
        deps = Dependencies(project)
        deps.require('translate')
        ids = {str(l['id']) for l in subtitle_document.read(deps, translation=False, strict=False)}
        params = crud.project_params(project)
        params['subtitle_edits'] = {id: value for id, value in params.get('subtitle_edits', {}).items() if id in ids}
        project.params = json.dumps(params, ensure_ascii=False)
        crud.save_project(project)
        crud.write_manifest(uid)
    return {'ok': True}


@router.get('/lines/{line_id}/params')
def line_params(uid: str, line_id: int):
    from dataclasses import asdict
    from dubforge.pipeline import pronunciation
    from dubforge.pipeline.step_params import schema_view
    from dubforge import configuration
    project, deps, line = _line_context(uid, line_id)
    own = line_settings.overrides(project, line)
    defaults = {k: deps.cfg(k) for k in line_settings.LINE_PATHS}
    defaults[pronunciation.PATH] = ''
    values = {**defaults, **own, 'tts.backend': deps.cfg('tts.backend')}
    schema = schema_view(values)['tts']
    schema['fields'] = [f for f in schema['fields'] if f['path'] in line_settings.LINE_PATHS]
    schema['fields'].insert(0, asdict(pronunciation.FIELD))
    schema['values'][pronunciation.PATH] = values[pronunciation.PATH]
    resolved = configuration.resolve(uid)
    parent_sources = {k: resolved['sources'][k] for k in defaults if k != pronunciation.PATH}
    parent_sources[pronunciation.PATH] = {'kind': 'line_default', 'label': '跟随本句译文'}
    from dubforge.pipeline import live_dubbing
    execution = live_dubbing.view(project, line_id)
    return {'steps': {'tts': schema}, 'defaults': defaults, 'line_text': line['text'], 'revision': execution['revision'],
            'execution': execution,
            'context': {'tts.backend': deps.cfg('tts.backend')},
            'parent_sources': parent_sources,
            'sources': {**parent_sources, **{k: {'kind': 'line'} for k in own}},
            'overridden': {k: k in own for k in defaults}}


@router.put('/lines/{line_id}/params')
def save_line_params(uid: str, line_id: int, body: dict):
    project, deps, line = _line_context(uid, line_id)
    if deps.cfg('tts.backend') != 'indextts25':
        raise DubForgeError('当前配音服务的声音参数由服务端管理', code='CONFIG_INVALID')
    from dubforge.pipeline import live_dubbing
    return live_dubbing.save(uid, line, body.get('updates', {}), body.get('reset', []),
                            execution=body.get('execution', 'auto'), revision=body.get('revision'))


@router.post('/synthesize')
def synthesize_selected(uid: str, body: dict):
    return runner.start_dubbing(uid, body.get('line_ids'), takes=body.get('takes'),
                                force=bool(body.get('force', True)), expected=body.get('expected'),
                                append_to_job_id=body.get('append_to_job_id'))


@router.post("/lines/{line_id}/synthesize")
def synthesize(uid: str, line_id: int, body: dict | None = None):
    body = body or {}
    takes = {str(line_id): body['take']} if 'take' in body else None
    result = runner.start_dubbing(uid, [line_id], takes=takes, force=bool(body.get('force', False)),
                                 append_to_job_id=body.get('append_to_job_id'))
    return {**result, 'take': result['lines'][0]['take']}


@router.put('/lines/{line_id}/selection')
def set_selection(uid: str, line_id: int, body: dict):
    project, _, line = _line_context(uid, line_id)
    with dubbing_session.edit(project):
        project = crud.get_project(uid)
        dubbing_edit.save_selection(project, line, body.get('source'), body.get('take'))
    return {'ok': True}


def _confirm_many(uid, ids, value):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    if not isinstance(ids, list) or any(type(i) is not int for i in ids):
        raise DubForgeError('句子选择格式无效', code='LINE_NOT_FOUND')
    with dubbing_session.edit(project):
        project = crud.get_project(uid)
        deps = Dependencies(project)
        lines = {l['id']: l for l in deps.dubbing_lines(required=True)}
        if any(i not in lines for i in ids):
            raise DubForgeError('选择的句子已变化，请刷新列表', code='LINE_NOT_FOUND')
        root = crud.project_dir(uid)
        saved = _load_confirm(root)
        updated = 0
        for id in dict.fromkeys(ids):
            line = lines[id]
            take = dubbing_edit.selection(project, line, root)['take']
            if take is None or not dubbing_edit.playable(project, line, root, take, dependencies=deps):
                continue
            key = dubbing_edit.confirm_key(id, take)
            if value:
                data = dubbing_edit.read_take(root, line, take)
                saved[key] = {'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                    'fingerprint': data.get('fingerprint') or line_fingerprint(project, line, dependencies=deps),
                    'version': _file_version(dubbing_edit.take_path(root, id, take))}
            else:
                saved.pop(key, None)
            updated += 1
        _save_confirm(root, saved)
        return {'ok': True, 'updated': updated, 'skipped': len(set(ids)) - updated,
                'confirmed': value, 'count': len(saved)}


@router.post('/confirm')
def confirm_many(uid: str, body: dict):
    return _confirm_many(uid, body.get('line_ids'), bool(body.get('confirmed', True)))


@router.post('/lines/{line_id}/confirm')
def confirm(uid: str, line_id: int, body: dict):
    result = _confirm_many(uid, [line_id], bool(body.get('confirmed', True)))
    if result['skipped']:
        raise DubForgeError('请先生成或选择本句配音，再标记满意', code='WORKBENCH_SAMPLE_MISSING')
    return result


@router.get("/audio/line/{line_id}")
@router.head("/audio/line/{line_id}", include_in_schema=False)
def line_audio(uid: str, line_id: int, take: int = 1):
    if not crud.get_project(uid):
        return Response("no project", status_code=404)
    p = dubbing_edit.take_path(crud.project_dir(uid), line_id, take)
    if not p.is_file():
        return Response("not found", status_code=404)
    # 单句音频释放文件句柄后仍须支持 Range，浏览器暂停恢复时会请求目标时间段。
    try:
        return MediaSnapshotResponse(p, media_type="audio/wav",
                                     headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return Response("not found", status_code=404)


@router.get("/audio/bg")
@router.head("/audio/bg", include_in_schema=False)
@router.get('/audio/{track}')
@router.head('/audio/{track}', include_in_schema=False)
def bg_audio(uid: str, track: str = 'bg'):
    if not crud.get_project(uid):
        return Response("no project", status_code=404)
    names = {'bg': 'background.wav', 'background': 'background.wav', 'vocal': 'vocal.wav', 'raw': 'raw.wav'}
    if track not in names:
        return Response('not found', status_code=404)
    p = crud.project_dir(uid) / "audio" / names[track]
    if not p.is_file():
        return Response("not found", status_code=404)
    return MediaFileResponse(p, media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


@router.get("/video")
@router.head("/video", include_in_schema=False)
def video(uid: str):
    project = crud.get_project(uid)
    if not project:
        return Response("no project", status_code=404)
    v = resolve_video(project, crud.project_dir(uid))
    if v is None or not v.is_file():
        return Response("no video", status_code=404)
    return MediaFileResponse(v, media_type="video/mp4",
                        headers={"Cache-Control": "no-store",
                                 "Accept-Ranges": "bytes"})
