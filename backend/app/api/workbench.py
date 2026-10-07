"""配音试配 API：与批量配音共用输入、项目参数和句级缓存
- state: 逐句列表 (缓存/满意标记/语速) + 视频/背景音可用性
- synthesize: 所选/单句合成提交到同一后台步骤任务
- confirm: 满意标记 (artifacts/tts/workbench_confirm.json; 批量合成自动复用已试配句)
- 音视频流: 支持 Range (浏览器拖动)
"""
import datetime
import json
from pathlib import Path

from dubforge import config, tts_service
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.step_tts import line_cache_valid, line_fingerprint
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.video_source import resolve_video
from dubforge.pipeline import line_settings, dubbing_session, dubbing_edit, subtitle_document
from dubforge.utils.fs import atomic_replace
from fastapi import APIRouter
from fastapi.responses import Response
from app.media import MediaFileResponse
from dubforge.runner import runner
from dubforge.pipeline import dubbing_task

router = APIRouter(prefix="/api/projects/{uid}/workbench", tags=["workbench"])


@router.post('/subtitle-preview')
def subtitle_preview(uid: str, body: dict):
    """只预览当前草稿样式，不保存参数，也不要求生成配音或先导出字幕。"""
    from dubforge.errors import ConfigError
    from dubforge.pipeline.step_params import validate_numeric_updates
    from dubforge.pipeline.step_subtitles import build_tracks
    from dubforge.pipeline.subtitle_style import PREVIEW_KEYS, preview_frame
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
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA
    labels = {f.path: f.label for f in STEP_PARAM_SCHEMA['tts']}
    from dubforge.pipeline.step_dubtask import build_task
    try:
        lines = build_task(deps, editing=True)['lines']
    except (DubForgeError, OSError, ValueError):
        lines = []
    result = []
    for line in lines:
        blockers = deps.unready('workbench', line=line)
        chosen = dubbing_edit.selection(project, line, pdir, dependencies=deps)
        versions = []
        for take in dubbing_edit.take_ids(pdir, line['id']):
            wav = dubbing_edit.take_path(pdir, line['id'], take)
            valid = deps.current('split') and deps.current('translate') and dubbing_edit.playable(project, line, pdir, take, dependencies=deps)
            data = dubbing_edit.read_take(pdir, line, take)
            saved = confirms.get(dubbing_edit.confirm_key(line['id'], take), {})
            fingerprint = data.get('fingerprint') or line_fingerprint(project, line, dependencies=deps)
            versions.append({'id': take, 'exists': valid, 'stale': not valid,
                'duration_retry': data.get('duration_retry'),
                'settings_changed': valid and fingerprint != line_fingerprint(project, line, dependencies=deps),
                'url': _versioned_url(f"/api/projects/{project.uid}/workbench/audio/line/{line['id']}?take={take}", wav),
                'seconds': _wav_dur(wav) if valid else None,
                'confirmed': bool(valid and saved.get('fingerprint') == fingerprint and saved.get('version') == _file_version(wav))})
        current = next((v for v in versions if v['id'] == chosen['take']), None)
        own = line_settings.overrides(project, line)
        result.append({**line,
            'selection': chosen, 'versions': versions,
            'unready_inputs': blockers, 'customized': bool(own),
            'custom_params': [{'label': labels.get(k, k), 'value': v} for k, v in own.items()],
            'wav': current or {'exists': False, 'stale': chosen['take'] is not None, 'url': None, 'seconds': None},
            'confirmed': bool(current and current['confirmed'])})
    return result


@router.get("")
def state(uid: str, probe: bool = True):
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
    task_error = None
    if not lines and not blockers:
        try:
            deps.dubbing_lines(required=True)
        except DubForgeError as exc:
            task_error = str(exc)

    # 服务探测可从页面关键路径中拆出; 工作台先展示内容, 再异步更新在线状态。

    video = resolve_video(project, pdir)
    def audio_track(name):
        path = pdir / 'audio' / name
        found = deps.current('preprocess') and path.is_file()
        return {'found': found, 'url': _versioned_url(
            f'/api/projects/{uid}/workbench/audio/{name.removesuffix(".wav")}', path) if found else None}
    return {
        "lines": lines,
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
        import wave
        with wave.open(str(p), "rb") as w:
            return round(w.getnframes() / w.getframerate(), 2)
    except Exception:
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
    from dubforge.pipeline.step_params import schema_view
    from dubforge import configuration
    project, deps, line = _line_context(uid, line_id)
    own = line_settings.overrides(project, line)
    defaults = {k: deps.cfg(k) for k in line_settings.LINE_PATHS}
    values = {**defaults, **own, 'tts.backend': deps.cfg('tts.backend')}
    schema = schema_view(values)['tts']
    schema['fields'] = [f for f in schema['fields'] if f['path'] in line_settings.LINE_PATHS]
    resolved = configuration.resolve(uid)
    parent_sources = {k: resolved['sources'][k] for k in defaults}
    return {'steps': {'tts': schema}, 'defaults': defaults,
            'context': {'tts.backend': deps.cfg('tts.backend')},
            'parent_sources': parent_sources,
            'sources': {**parent_sources, **{k: {'kind': 'line'} for k in own}},
            'overridden': {k: k in own for k in defaults}}


@router.put('/lines/{line_id}/params')
def save_line_params(uid: str, line_id: int, body: dict):
    project, deps, line = _line_context(uid, line_id)
    if any(s.status in {'running', 'queued'} for s in deps.steps.values()):
        raise DubForgeError('项目正在运行，请结束后再修改句子参数', code='PIPELINE_BUSY')
    if deps.cfg('tts.backend') != 'indextts25':
        raise DubForgeError('当前配音服务的声音参数由服务端管理', code='CONFIG_INVALID')
    with dubbing_session.edit(project):
        project = crud.get_project(uid)
        values = line_settings.save(project, line, body.get('updates', {}), body.get('reset', []), deps.cfg)
    return {'ok': True, 'overridden': list(values)}


@router.post('/synthesize')
def synthesize_selected(uid: str, body: dict):
    return runner.start_dubbing(uid, body.get('line_ids'), takes=body.get('takes'),
                                force=bool(body.get('force', True)))


@router.post("/lines/{line_id}/synthesize")
def synthesize(uid: str, line_id: int, body: dict | None = None):
    body = body or {}
    takes = {str(line_id): body['take']} if 'take' in body else None
    result = runner.start_dubbing(uid, [line_id], takes=takes, force=bool(body.get('force', False)))
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
def line_audio(uid: str, line_id: int, take: int = 1):
    if not crud.get_project(uid):
        return Response("no project", status_code=404)
    p = dubbing_edit.take_path(crud.project_dir(uid), line_id, take)
    if not p.is_file():
        return Response("not found", status_code=404)
    # 单句音频先读完并关闭文件；慢速播放请求不能在 Windows 上锁住正在重合成的版本。
    # 浏览器拿到完整短音频后可本地定位，长视频/长音轨使用可取消的分段读取。
    return Response(p.read_bytes(), media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


@router.api_route("/audio/bg", methods=['GET', 'HEAD'])
@router.api_route('/audio/{track}', methods=['GET', 'HEAD'])
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


@router.api_route("/video", methods=['GET', 'HEAD'])
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
