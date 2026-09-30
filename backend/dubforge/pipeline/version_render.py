"""版本渲染：同一解析结果供单帧、短片与正式成片使用。"""
import math
import tempfile
import uuid
from pathlib import Path

from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as versions
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.video_source import resolve_video
from dubforge.utils.fs import atomic_replace
from dubforge.utils.srt import write_srt


class VersionContext:
    """只覆盖版本允许的参数，不修改共享项目、任务或服务凭据。"""
    def __init__(self, parent, version):
        self.parent, self.version = parent, version
        self.subtitle_path = None

    def __getattr__(self, name):
        return getattr(self.parent, name)

    def cfg(self, key, default=None):
        return self.version['values'].get(key, self.parent.cfg(key, default))

    def upstream(self, step, name):
        if step == 'subtitles' and self.subtitle_path:
            return self.subtitle_path
        return self.parent.upstream(step, name)


def subtitle_lines(ctx, version):
    from dubforge.pipeline.step_subtitles import build_tracks
    if not version['burn_subtitles']: return []
    track = version['values']['assemble.subtitle_track']
    tracks, _ = build_tracks(ctx, mode={'source': 'source', 'trans': 'target', 'bilingual': 'bilingual'}[track])
    return tracks[track + '.srt']


def frame(uid, body):
    from dubforge import comment_studio
    from dubforge.pipeline.subtitle_style import _run, prepare_subtitle_lines, video_canvas
    from dubforge.pipeline.comment_overlay import prepare_filter
    seconds = body.get('seconds', 0)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise versions.VersionError('预览位置必须是有效的非负秒数')
    project = crud.get_project(uid)
    if not project: raise versions.VersionError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    spec = versions.validate(body.get('version'))
    overlay = comment_studio.for_task(uid, spec['include_comments'], body.get('overlay_revision'))
    version = versions.resolve(deps, spec, comment_plan=overlay)
    ctx = VersionContext(deps, version)
    video = resolve_video(deps.project, deps.root)
    if not video or not video.is_file(): raise versions.VersionError('原视频尚未就绪，暂不能预览')
    with tempfile.TemporaryDirectory(prefix='dubforge-version-frame-') as folder:
        filters = [f'setpts=PTS+{seconds:.6f}/TB']
        if spec['burn_subtitles']:
            filters.append(prepare_subtitle_lines(subtitle_lines(ctx, version), ctx.cfg, video_canvas(video), Path(folder)))
        graph, _ = prepare_filter(version['comment_overlay'], video, Path(folder))
        if graph: filters.append(graph)
        data = _run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', f'{seconds:.6f}', '-i', str(video.resolve()),
                     '-vf', ','.join(filters), '-frames:v', '1', '-an', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'], cwd=folder)
        if not data: raise versions.VersionError('当前位置没有画面，请选择视频结束前的时间')
        return data


def preview_context(uid):
    """按需读取时间轴，不在步骤轮询中探测媒体。"""
    from dubforge.pipeline.step_assemble import _ffprobe_duration
    from dubforge.pipeline.subtitle_document import read
    from dubforge import comment_studio
    project = crud.get_project(uid)
    if not project: raise versions.VersionError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    video = resolve_video(project, deps.root)
    duration = _ffprobe_duration(video) if video and video.is_file() else 0
    try:
        lines = read(deps, translation=False, strict=False)
    except (versions.DubForgeError, KeyError, FileNotFoundError):
        lines = []
    overlay = comment_studio.effective_overlay(project.uid, comment_studio._read(project).get('overlay')) or {}
    return dict(duration=duration, subtitles=[dict(start=l['start'], end=l['end']) for l in lines],
                comments=[dict(start=overlay['settings']['start'] + i * overlay['settings']['interval'],
                               end=overlay['settings']['start'] + i * overlay['settings']['interval'] + overlay['settings']['duration'])
                          for i, _ in enumerate(overlay.get('items', []))])


def run(ctx):
    from dubforge.pipeline.step_assemble import _ffprobe_duration, _task_range, _mix_timeline, _mux, _mux_original_sub
    from dubforge.publishing.service import output_replacement
    deps = Dependencies(ctx.project)
    specs = ctx.task_data['version_specs']
    if ctx.task_data.get('comments_auto'):
        # 评论准备=auto 且无已确认方案: 合成前自动采集/评分/翻译并保存, 再带方案重解析。
        from dubforge import comments_auto
        specs = comments_auto.ensure_render_specs(ctx, deps, specs)
        ctx.task_data['version_specs'] = specs
    if any(version['dubbing'] for version in specs):
        # 整批开始前检查，避免先编码原声版，再因后面的配音版不完整失败。
        from dubforge.pipeline.step_assemble import _require_dubbing
        _require_dubbing(ctx)
    video = resolve_video(ctx.project, ctx.pdir)
    if not video: raise versions.VersionError('项目没有可用原视频')
    total = _ffprobe_duration(video)
    preview = ctx.task_data.get('scope') == 'preview'
    start, end = _task_range(ctx, total) if preview else (None, None)
    duration = end - start if preview else total
    out_dir = ctx.pdir / 'output'
    out_dir.mkdir(parents=True, exist_ok=True)
    voices = {}
    try:
        for index, version in enumerate(specs):
            ctx.check_cancel(); ctx.check_pause()
            if not any(version[k] for k in ('dubbing', 'burn_subtitles', 'include_comments')):
                ctx.log(f'「{version["name"]}」直接使用原片，不创建重复文件')
                continue
            if not preview and versions.is_current(deps, version, verify_hash=True):
                ctx.log(f'「{version["name"]}」输入与生效设置未变，复用已有成品')
                continue
            overlay = version['comment_overlay']
            if version['include_comments'] and not overlay:
                raise versions.VersionError('此版本需要已确认的评论方案')
            if overlay and overlay['settings']['start'] >= total:
                raise versions.VersionError('评论首次出现时间超过视频时长')
            scoped = VersionContext(ctx, version)
            input_fingerprint = versions.fingerprint(deps, version)
            previous = versions.result_artifact(deps, version)
            out = out_dir / (f'preview_version_{version["id"]}_{uuid.uuid4().hex}.mp4' if preview else
                             previous.name if previous else f'final_version_{version["id"]}.mp4')
            pending = out.with_name('.tmp_pending_' + out.name)
            with output_replacement(ctx.project.uid): pass
            try:
                # 临时字幕按版本生成；不改共享导出文件，也不改项目字幕默认。
                with tempfile.TemporaryDirectory(prefix='.tmp_version_', dir=out_dir) as folder:
                    if version['burn_subtitles']:
                        scoped.subtitle_path = Path(folder) / 'version.srt'
                        write_srt(subtitle_lines(scoped, version), scoped.subtitle_path)
                    lo, hi = index * 99 / len(specs), (index + 1) * 99 / len(specs)
                    if version['dubbing']:
                        voice_key = version['values']['assemble.loudnorm']
                        if voice_key not in voices:
                            voices[voice_key] = _mix_timeline(scoped, ctx.pdir, total, start=start, end=end,
                                output_name=f'.tmp_voice_{uuid.uuid4().hex}.wav', register=False)
                        _mux(scoped, ctx.pdir, video, voices[voice_key], pending, duration, start=start,
                             burn=version['burn_subtitles'], comment_plan=overlay, progress_range=(lo, hi))
                    else:
                        _mux_original_sub(scoped, video, pending, duration, (lo, hi), start=start,
                                          burn=version['burn_subtitles'], comment_plan=overlay)
                with output_replacement(ctx.project.uid):
                    ctx.check_cancel()
                    atomic_replace(pending, out)
                    ctx.register_file(out.name, out, 'video', replace_existing=not preview)
                    if not preview: versions.record_result(ctx, version, out.name, input_fingerprint)
                ctx.log(f'「{version["name"]}」{"试片" if preview else "成品"}已生成')
            finally:
                pending.unlink(missing_ok=True)
    finally:
        for voice in voices.values(): voice.unlink(missing_ok=True)
