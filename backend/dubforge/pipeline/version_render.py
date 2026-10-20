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
        # 文字属于当前版本；画幅仅依赖源文件，可与本轮其他版本共用。
        self._prepared_subtitle_lines = None
        cache = getattr(parent, '_render_canvas_cache', None)
        if cache is None:
            cache = parent._render_canvas_cache = {}
        self._render_canvas_cache = cache
        # 报告属于本次实际编码，不能经 __getattr__ 继承其他版本的结果。
        # 旧编码器或只测编排的替身可以没有报告；真实测量失败仍由编码器抛出。
        self._audio_report = None
        self._video_encoding_report = None

    def __getattr__(self, name):
        return getattr(self.parent, name)

    def cfg(self, key, default=None):
        from dubforge.pipeline.video_encoding import ENCODER_KEY, CQ_KEY
        if key in {ENCODER_KEY, CQ_KEY}:
            return self.version['values'].get(key, 'software' if key == ENCODER_KEY else 18)
        return self.version['values'].get(key, self.parent.cfg(key, default))

    def upstream(self, step, name):
        if step == 'subtitles' and self.subtitle_path:
            return self.subtitle_path
        manifest = self.version.get('render_manifest')
        if manifest and step == 'preprocess' and name == 'background.wav':
            from dubforge.pipeline.assembly_manifest import media_path
            deps = self.dependencies() if hasattr(self.parent, 'dependencies') else self.parent
            return media_path(deps, manifest['media'].get('background'))
        return self.parent.upstream(step, name)


def subtitle_lines(ctx, version):
    from dubforge.pipeline.step_subtitles import build_tracks
    if not version['burn_subtitles']: return []
    if version.get('render_manifest'):
        from dubforge.pipeline.assembly_manifest import subtitle_lines as manifest_lines
        lines = manifest_lines(version['render_manifest'])
        ctx._prepared_subtitle_lines = dict(track=version['values']['assemble.subtitle_track'], lines=lines)
        return lines
    track = version['values']['assemble.subtitle_track']
    # 原声成片跟随源时钟；配音版三种字幕统一跟随配音排布。
    ctx = ctx if getattr(ctx, 'version', None) else VersionContext(ctx, version)
    tracks, _ = build_tracks(ctx, mode={'source': 'source', 'trans': 'target', 'bilingual': 'bilingual'}[track])
    lines = tracks[track + '.srt']
    ctx._prepared_subtitle_lines = dict(track=track, lines=lines)
    return lines


def frame(uid, body):
    from dubforge import comment_studio
    from dubforge.pipeline.subtitle_style import _run, prepare_subtitle_lines, render_canvas
    from dubforge.pipeline.comment_overlay import prepare_filter
    seconds = body.get('seconds', 0)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise versions.VersionError('预览位置必须是有效的非负秒数')
    project = crud.get_project(uid)
    if not project: raise versions.VersionError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    spec = versions.validate(body.get('version'))
    if spec.get('partial') or spec.get('update') or spec.get('timeline'):
        raise versions.VersionError('局部替换版本请生成成片后检查替换位置；静帧预览暂不支持混合基片与当前内容。',
                                    code='ASSEMBLY_PARTIAL_PREVIEW_UNSUPPORTED')
    overlay = comment_studio.for_task(uid, spec['include_comments'], body.get('overlay_revision'))
    version = versions.resolve(deps, spec, comment_plan=overlay)
    ctx = VersionContext(deps, version)
    video = resolve_video(deps.project, deps.root)
    if not video or not video.is_file(): raise versions.VersionError('原视频尚未就绪，暂不能预览')
    with tempfile.TemporaryDirectory(prefix='dubforge-version-frame-') as folder:
        filters = [f'setpts=PTS+{seconds:.6f}/TB']
        canvas = render_canvas(ctx, video) if spec['burn_subtitles'] or version['comment_overlay'] else None
        if spec['burn_subtitles']:
            filters.append(prepare_subtitle_lines(subtitle_lines(ctx, version), ctx.cfg, canvas, Path(folder)))
        graph, _ = prepare_filter(version['comment_overlay'], video, Path(folder), canvas=canvas)
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
    # 按需的时间轴入口复用实际审查规则；历史轮询不重新筛查整份字幕。
    # 原声无字幕版本也可能因为审查静音需要编码，不能在前端误判成原片直用。
    audio_redaction_required, audio_redaction_error = False, ''
    if deps.cfg('content_review.enabled', True) and deps.cfg('content_review.mute_audio', True):
        from dubforge.pipeline.content_redaction import plan as review_plan
        try:
            audio_redaction_required = bool(review_plan(deps)['ranges'])
        except versions.DubForgeError as exc:
            audio_redaction_error = exc.message
    return dict(duration=duration, subtitles=[dict(start=l['start'], end=l['end']) for l in lines],
                audio_redaction_required=audio_redaction_required, audio_redaction_error=audio_redaction_error,
                comments=[dict(start=overlay['settings']['start'] + i * overlay['settings']['interval'],
                               end=overlay['settings']['start'] + i * overlay['settings']['interval'] + overlay['settings']['duration'])
                          for i, _ in enumerate(overlay.get('items', []))])


def capture_prepared_inputs(ctx, deps, specs):
    """记录编码实际使用的版本方案；准备评论后仍使用本轮冻结的配置。"""
    if ctx.task_data.get('scope') == 'preview':
        return
    from dubforge.pipeline.stale_explanation import capture
    actual = deps.with_versions(specs)
    stamp = 'versions:' + versions.digest([versions.fingerprint(actual, v) for v in specs])
    ctx._completed_input_snapshot = capture(actual, 'assemble', fingerprint=stamp, scope='versions')


def uses_source_directly(ctx, version, *, review=None):
    """原片直用的统一条件；需要静音的原片仍须生成独立成品。"""
    if version.get('partial') or version.get('update') or version.get('timeline'):
        return False
    if version.get('output_cuts') or any(version[key] for key in ('dubbing', 'burn_subtitles', 'include_comments')):
        return False
    if ctx.cfg('assemble.final_loudness_mode', 'off') != 'off':
        return False
    if review is None:
        from dubforge.pipeline.content_redaction import plan
        review = plan(ctx)
    return not (review['ranges'] and ctx.cfg('content_review.mute_audio', True))


def run(ctx):
    from dubforge.execution_progress import report_stage
    report_stage(ctx, 'preparing_assembly_versions', '核对成片版本与冻结设置')
    from dubforge.pipeline.step_assemble import _ffprobe_duration, _task_range, _mix_timeline, _mux, _mux_original_sub
    from dubforge.publishing.service import output_replacement
    deps = ctx.dependencies()
    specs = ctx.task_data['version_specs']
    if ctx.task_data.get('comments_auto') and not all(v.get('update') or v.get('timeline') for v in specs):
        # 评论准备=auto 且无已确认方案: 合成前自动采集/评分/翻译并保存, 再带方案重解析。
        from dubforge import comments_auto
        specs = comments_auto.ensure_render_specs(ctx, deps, specs)
        ctx.task_data['version_specs'] = specs
    elif any(v.get('comment_overlay') for v in specs if v.get('include_comments')):
        # 手工确认方案只做同路径预检；没有实际媒体的领域测试/预览不探测画幅。
        from dubforge.pipeline.assembly_manifest import media_path
        first_manifest = next((v.get('render_manifest') for v in specs if v.get('update')), None)
        video = (media_path(deps, first_manifest['media']['source']) if first_manifest and all(v.get('update') for v in specs)
                 else resolve_video(ctx.project, ctx.pdir))
        if video and video.is_file():
            from dubforge.pipeline import comment_overlay
            from dubforge.pipeline.subtitle_style import render_canvas
            canvas = render_canvas(ctx, video)
            for version in specs:
                if version.get('comment_overlay') and version.get('include_comments'):
                    comment_overlay.ass_document(version['comment_overlay'], canvas)
    from dubforge.pipeline.content_redaction import safe_overlay
    # 先确定实际烧录的评论，再记录生成输入；不能把自动准备前的空方案
    # 或筛查前会被排除的评论记作成片实际内容。
    specs = [{**v, 'comment_overlay': safe_overlay(VersionContext(ctx, v), v['comment_overlay'])}
             if v['comment_overlay'] and not v.get('update') and not v.get('timeline') else v for v in specs]
    capture_prepared_inputs(ctx, deps, specs)
    if any(versions.requires_dubbing(version) for version in specs):
        # 整批开始前检查，避免先编码原声版，再因后面的配音版不完整失败。
        from dubforge.pipeline.step_assemble import _require_dubbing
        _require_dubbing(ctx)
    from dubforge.pipeline.assembly_manifest import media_path
    immutable_only = all(v.get('update') or v.get('timeline') for v in specs)
    first = specs[0]
    video = ((Path(first['timeline_plan']['media'][0]['path']) if first.get('timeline') else
              media_path(deps, first['render_manifest']['media']['source'])) if immutable_only
             else resolve_video(ctx.project, ctx.pdir))
    if not video: raise versions.VersionError('项目没有可用原视频')
    total = ((float(first['timeline_plan']['source_duration']) if first.get('timeline') else
              float(first['render_manifest']['duration'])) if immutable_only else _ffprobe_duration(video))
    preview = ctx.task_data.get('scope') == 'preview'
    force_rebuild = not preview and ctx.task_data.get('force_rebuild', False)
    if force_rebuild:
        ctx.log('本次将完整重新生成选定版本，并重新建立成片输入对照。')
    if preview and any(version.get('partial') or version.get('update') or version.get('timeline') for version in specs):
        raise versions.VersionError('局部替换版本请先生成完整成片，再播放检查替换位置；暂不支持单独生成试片。',
                                    code='ASSEMBLY_PARTIAL_PREVIEW_UNSUPPORTED')
    start, end = _task_range(ctx, total) if preview else (None, None)
    duration = end - start if preview else total
    from dubforge.pipeline.output_cuts import keep_segments
    for version in specs:
        if version.get('output_cuts'):
            version_total = (float(version['timeline_plan']['source_duration']) if version.get('timeline') else
                             float(version['render_manifest']['duration']) if version.get('update') else total)
            segments = keep_segments(version['output_cuts'], start=start or 0, end=end if preview else version_total)
            if not segments:
                raise versions.VersionError('选定的试片范围已被全部删除，请选择包含保留画面的区间。')
            removed = (end - start if preview else version_total) - sum(row['end'] - row['start'] for row in segments)
            ctx.log(f'成片剪辑「{version["name"]}」：本次删除 {removed:.2f} 秒，画面、声音、字幕和评论同步裁剪')
    out_dir = ctx.pdir / 'output'
    out_dir.mkdir(parents=True, exist_ok=True)
    voices = {}
    voice_reports = {}
    try:
        for index, version in enumerate(specs):
            ctx.check_cancel(); ctx.check_pause()
            current_video = video
            if version.get('update'):
                from dubforge.pipeline.assembly_updates import load_plan as load_update
                from dubforge.pipeline.assembly_manifest import media_path
                load_update(deps, version, verify=True)
                current_video = media_path(deps, version['render_manifest']['media']['source'])
            if version.get('timeline'):
                from dubforge.pipeline.assembly_timeline import load_plan as load_timeline
                load_timeline(deps, version, verify=True)
            render_total = (float(version['timeline_plan']['source_duration']) if version.get('timeline') else
                            float(version['render_manifest']['duration']) if version.get('update') else total)
            render_duration = end - start if preview else render_total
            report_stage(ctx, 'checking_version', f'核对成片版本 {index + 1}/{len(specs)}：{version["name"]}',
                         completed=index, total=len(specs), unit='版本', batch_index=index + 1, batch_total=len(specs))
            from dubforge.pipeline.content_redaction import plan as review_plan, receipt as review_receipt
            scoped = VersionContext(ctx, version)
            review = (dict(ranges=[], blocked_ids=[]) if version.get('timeline') else review_plan(scoped))
            if not force_rebuild and uses_source_directly(scoped, version, review=review):
                report_stage(ctx, 'reuse_original', f'直接使用原片：{version["name"]}', reused=True)
                ctx.log(f'「{version["name"]}」直接使用原片，不创建重复文件')
                continue
            if not preview and not force_rebuild and versions.is_current(deps, version, verify_hash=True):
                report_stage(ctx, 'reuse_version', f'复用已完成版本：{version["name"]}', reused=True)
                ctx.log(f'「{version["name"]}」输入与生效设置未变，复用已有成品')
                continue
            overlay = version['comment_overlay']
            if version['include_comments'] and not overlay and not version.get('timeline'):
                raise versions.VersionError('此版本需要已确认的评论方案')
            if overlay and not version.get('update') and overlay['settings']['start'] >= total:
                raise versions.VersionError('评论首次出现时间超过视频时长')
            if review['ranges']:
                ctx.log(f'内容审查：隐藏 {len(review["blocked_ids"])} 句字幕，处理 {len(review["ranges"])} 个时段；需另行核对画面')
            input_fingerprint = versions.fingerprint(deps, version)
            previous = versions.result_artifact(deps, version)
            out = out_dir / (f'preview_version_{version["id"]}_{uuid.uuid4().hex}.mp4' if preview else
                             previous.name if previous else f'final_version_{version["id"]}.mp4')
            pending = out.with_name('.tmp_pending_' + out.name)
            with output_replacement(ctx.project.uid): pass
            try:
                # 临时字幕按版本生成；不改共享导出文件，也不改项目字幕默认。
                with tempfile.TemporaryDirectory(prefix='.tmp_version_', dir=out_dir) as folder:
                    if version['burn_subtitles'] and not version.get('timeline'):
                        scoped.subtitle_path = Path(folder) / 'version.srt'
                        write_srt(subtitle_lines(scoped, version), scoped.subtitle_path)
                    lo, hi = index * 99 / len(specs), (index + 1) * 99 / len(specs)
                    if version.get('timeline'):
                        from dubforge.pipeline.assembly_timeline import render as render_timeline
                        render_timeline(scoped, pending)
                    elif version.get('update') and version['update_plan']['method'] == 'partial':
                        from dubforge.pipeline.assembly_updates import render as render_update
                        render_update(scoped, current_video, pending, render_total)
                    elif version.get('update') and not version['update_plan']['replace_audio']:
                        from dubforge.pipeline.assembly_updates import render_visual_full
                        render_visual_full(scoped, current_video, pending, render_total)
                    elif version.get('partial'):
                        from dubforge.pipeline.partial_render import run as render_partial
                        render_partial(scoped, video, pending, total)
                    elif version['dubbing']:
                        voice_key = (version['id'] if version.get('update') else '', version['values']['assemble.loudnorm'],
                                     version['values'].get('assemble.sentence_leveling', False),
                                     version['values'].get('assemble.sentence_leveling_max_db', 3.))
                        if voice_key not in voices or preview:
                            if voice_key in voices:
                                voices[voice_key].unlink(missing_ok=True)
                            voices[voice_key] = _mix_timeline(scoped, ctx.pdir, render_total, start=start, end=end,
                                output_name=f'.tmp_voice_{uuid.uuid4().hex}.wav', register=False)
                            voice_reports[voice_key] = getattr(scoped, '_audio_line_report', [])
                        else:
                            scoped._audio_line_report = voice_reports[voice_key]
                        _mux(scoped, ctx.pdir, current_video, voices[voice_key], pending, render_duration, start=start,
                             burn=version['burn_subtitles'], comment_plan=overlay, progress_range=(lo, hi))
                    else:
                        _mux_original_sub(scoped, current_video, pending, render_duration, (lo, hi), start=start,
                                          burn=version['burn_subtitles'], comment_plan=overlay)
                if scoped._video_encoding_report is not None:
                    version['video_encoding'] = scoped._video_encoding_report
                if not preview:
                    from dubforge.pipeline.assembly_sentence_changes import completed as sentence_snapshot
                    if version.get('timeline'):
                        from dubforge.pipeline.assembly_timeline import completed_snapshot
                        version['sentence_inputs'] = completed_snapshot(version)
                    elif version.get('update'):
                        import copy
                        version['sentence_inputs'] = copy.deepcopy(version['render_manifest']['inputs'])
                    else:
                        version['sentence_inputs'] = sentence_snapshot(deps, version)
                        if not version.get('partial'):
                            from dubforge.pipeline.assembly_manifest import capture as capture_manifest, materialize
                            version['render_manifest'] = materialize(deps, capture_manifest(deps, version))
                with output_replacement(ctx.project.uid):
                    ctx.check_cancel()
                    report_stage(ctx, 'saving_version', f'保存成片版本：{version["name"]}', batch_index=index + 1, batch_total=len(specs))
                    atomic_replace(pending, out)
                    from dubforge.pipeline.audio_quality import write_report
                    if scoped._audio_report is not None:
                        write_report(out, scoped._audio_report)
                    if preview:
                        from dubforge.pipeline.assembly_preview import record
                        record(ctx, version, out, input_fingerprint, start=start, end=end)
                    ctx.register_file(out.name, out, 'video', replace_existing=not preview)
                    if not preview:
                        received = (dict(schema=1, scope='timeline', whole_output_reviewed=False,
                            inherited_baseline=version['timeline_plan']['base_version'].get('content_review_receipt'),
                            note='只对已生成媒体剪辑；字幕、评论和声音沿用各来源，未重新审查全片。')
                            if version.get('timeline') else review_receipt(scoped))
                        if version.get('partial'):
                            from dubforge.pipeline.partial_render import review_receipt as partial_review_receipt
                            received = partial_review_receipt(scoped, received)
                        version['content_review_receipt'] = received
                        versions.record_result(ctx, version, out.name, input_fingerprint)
                ctx.log(f'「{version["name"]}」{"试片" if preview else "成品"}已生成')
            finally:
                # 取消或预检失败可能发生在完整人声已返回、mux尚未消费之前。
                # 这里只回收本轮临时副本，不能删除未来共用的完整人声缓存。
                full_voice = scoped.__dict__.pop('_preview_full_voice', None)
                if full_voice is not None and Path(full_voice).name.startswith('.tmp_full_'):
                    Path(full_voice).unlink(missing_ok=True)
                pending.unlink(missing_ok=True)
                from dubforge.pipeline.audio_quality import report_path
                report_path(pending).unlink(missing_ok=True)
    finally:
        for voice in voices.values(): voice.unlink(missing_ok=True)
        full_voice = ctx.__dict__.pop('_preview_full_voice', None)
        if full_voice is not None and Path(full_voice).name.startswith('.tmp_full_'):
            Path(full_voice).unlink(missing_ok=True)
