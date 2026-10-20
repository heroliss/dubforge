"""受控修订已有成片：重建画面字幕，保留已混音声音，不改正规流水线状态。"""
import copy
import hashlib
import json
import math
import queue
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dubforge import comment_studio, configuration, config, video_info
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.errors import DubForgeError
from dubforge.pipeline.render_resources import bounded_ffmpeg_command, ffmpeg_failure, preflight_render
from dubforge.pipeline import assembly_versions as versions, content_redaction, dubbing_session
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.video_source import resolve_video
from dubforge.utils.fs import atomic_replace, atomic_write_bytes

RENDER_VERSION = 'review-render-v2'
_DEFINITION_KEYS = ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')
_REVIEW_KEYS = ('content_review.enabled', 'content_review.mute_audio', 'content_review.audio_mode',
                'content_review.blocked_terms', 'content_review.blocked_ranges')


class ReviewRenderError(DubForgeError):
    code = 'REVIEW_RENDER_INVALID'
    suggestion = '请检查原视频、已生成成片及审查设置后重试；旧成片和已发布稿件保持保留。'


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _media(path):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
            '-of', 'json', str(path)], capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=60, check=False)
        doc = json.loads(result.stdout)
        duration = float(doc['format']['duration'])
        if result.returncode or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return dict(duration=duration, video=any(s['codec_type'] == 'video' for s in doc['streams']),
                    audio=any(s['codec_type'] == 'audio' for s in doc['streams']))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        raise ReviewRenderError('无法读取修订输入的视频时长或音轨', code='REVIEW_RENDER_MEDIA') from exc


def _path(deps, art):
    path = Path(art.rel_path)
    if not path.is_absolute():
        path = (deps.root / path).resolve()
        if not path.is_relative_to(deps.root):
            raise ReviewRenderError('产物路径越出项目目录', code='REVIEW_RENDER_SOURCE')
    return path.resolve()


def _file(deps, art, *, verify_hash):
    path = _path(deps, art)
    try:
        if not path.is_file() or path.stat().st_size != art.size or verify_hash and _sha(path) != art.sha256:
            raise ReviewRenderError('修订输入与登记哈希不一致，请先核对文件', code='REVIEW_RENDER_SOURCE_CHANGED')
    except OSError as exc:
        raise ReviewRenderError('修订输入文件无法读取', code='REVIEW_RENDER_SOURCE_CHANGED') from exc
    return path


def _snapshot(record):
    try:
        raw = json.loads(record.snapshot)
        if not isinstance(raw, dict):
            raise ValueError()
        if raw.get('partial'):
            raise ReviewRenderError('该成片包含局部修订，请在“合成成片”基于它继续局部修订，或完整生成新的版本。',
                                    code='REVIEW_RENDER_PARTIAL_UNSUPPORTED')
        if raw.get('output_cuts'):
            raise ReviewRenderError('所选成片已应用片段剪辑，不能复用旧混音生成审查修订版；请在“合成成片”按当前剪辑方案重新生成',
                                    code='REVIEW_RENDER_CUTS_UNSUPPORTED')
        definition = versions.validate({k: raw[k] for k in _DEFINITION_KEYS})
        if definition['id'] != record.version_id or not isinstance(raw['values'], dict):
            raise ValueError()
        # 只有成功版本快照能够说明旧成片的呈现参数，禁止从文件名或当前全局值猜测。
        allowed = {f['path'] for f in versions.fields()}
        values = {k: copy.deepcopy(v) for k, v in raw['values'].items() if k in allowed}
        required = {'assemble.crf'}
        if definition['burn_subtitles']:
            from dubforge.pipeline.subtitle_style import STYLE_DEFAULTS
            required |= {*STYLE_DEFAULTS, 'assemble.subtitle_track',
                         'subtitles.min_duration', 'subtitles.source_first'}
        if not required <= values.keys():
            raise ValueError()
        versions.validate({**definition, 'overrides': values})
        if definition['include_comments']:
            settings = raw['comment_overlay']['settings']
            if not isinstance(settings, dict) or not comment_studio.DEFAULT_SETTINGS.keys() <= settings.keys():
                raise ValueError()
            comment_studio.validate_settings(settings)
        if raw.get('review_render'):
            raise ReviewRenderError('该文件已经是审查修订版，请选择保留的原成片进行下一次修订',
                                    code='REVIEW_RENDER_SOURCE')
        return {**definition, 'values': values, 'comment_overlay': copy.deepcopy(raw.get('comment_overlay'))}
    except (ValueError, TypeError, KeyError, DubForgeError) as exc:
        if isinstance(exc, ReviewRenderError):
            raise
        raise ReviewRenderError('成片缺少可识别的成功版本快照，无法安全还原字幕或评论参数',
                                code='REVIEW_RENDER_SNAPSHOT') from exc


class _Context:
    def __init__(self, deps):
        self.deps, self.project, self.pdir = deps, deps.project, deps.root

    def cfg(self, key, default=None):
        return self.deps.cfg(key, default)

    def upstream(self, step, name):
        return self.deps.upstream(step, name)

    def run_subprocess(self, command):
        """声音测量与处理也接受同一取消信号，不遗留 FFmpeg 子进程。"""
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace')
        try:
            while True:
                _cancel(getattr(self, '_audio_cancel', None))
                try:
                    out, err = process.communicate(timeout=.25)
                    return process.returncode, out, err
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                from dubforge.runner import StepContext
                StepContext._kill_proc(process)
                process.wait(timeout=10)


def _prepare(deps, art, record, *, verify_hash, encoder='libx264'):
    snapshot = _snapshot(record)
    from dubforge.pipeline.output_cuts import snapshot as cuts_snapshot
    if cuts_snapshot(deps):
        raise ReviewRenderError('当前已启用片段剪辑，不能使用旧审查修订流程；请在“合成成片”按当前剪辑方案重新生成',
                                code='REVIEW_RENDER_CUTS_UNSUPPORTED')
    if art.project_id != deps.project.id or art.step_key != 'assemble' or art.kind != 'video' or art.sha256 != record.artifact_hash:
        raise ReviewRenderError('只能修订本项目登记的正式成片', code='REVIEW_RENDER_SOURCE')
    old = _file(deps, art, verify_hash=verify_hash)
    if old.parent != (deps.root / 'output').resolve() or not old.name.startswith('final_') or old.suffix.lower() != '.mp4':
        raise ReviewRenderError('试片、临时文件或原视频不能作为待修订成片', code='REVIEW_RENDER_SOURCE')
    video = resolve_video(deps.project, deps.root)
    if video is None:
        raise ReviewRenderError('缺少项目原视频，无法移除已烧录字幕', code='VIDEO_MISSING')
    video = video.resolve()
    source = next((a for a in deps.arts.values() if a.step_key == 'download' and _path(deps, a) == video), None)
    if not source or source.id == art.id or video == old:
        raise ReviewRenderError('原视频没有本项目的独立下载或导入登记，不能用成片画面代替', code='REVIEW_RENDER_SOURCE')
    _file(deps, source, verify_hash=verify_hash)
    values = {**snapshot['values'], **{k: deps.cfg(k, default) for k, default in zip(
        _REVIEW_KEYS, (True, True, 'voice_only', '', ''), strict=True)}}
    from dubforge.pipeline.audio_quality import DEFAULTS as AUDIO_DEFAULTS
    values = {**AUDIO_DEFAULTS, **values}
    frozen = {**snapshot, 'values': values}
    ctx = _Context(deps.for_version(frozen))
    if not ctx.cfg('content_review.enabled', True):
        raise ReviewRenderError('请开启内容审查后再生成审查修订版', code='REVIEW_RENDER_DISABLED')
    review = content_redaction.plan(ctx)
    if review['ranges'] and not ctx.cfg('content_review.mute_audio', True):
        raise ReviewRenderError('需处理的时段尚未开启音频静音，请核定声音处理后再修订', code='REVIEW_RENDER_AUDIO')
    current_overlay = comment_studio.confirmed(deps.project.uid) if snapshot['include_comments'] else None
    overlay = copy.deepcopy(current_overlay)
    if overlay:
        overlay['settings'] = copy.deepcopy(snapshot['comment_overlay']['settings'])
        overlay['settings'].update({k.removeprefix('comments.'): v for k, v in values.items() if k.startswith('comments.')})
        overlay = content_redaction.safe_overlay(ctx, overlay)
        for item in overlay['items']:
            comment_studio.display_lines(item, overlay['settings'])
    files = [source, art, *(a for key, a in deps.arts.items() if key in {
        'split/split.txt', 'translate/translate.txt', 'transcribe/src.srt'})]
    audio = content_redaction.audio_plan(ctx)
    background_art = ctx.deps.arts.get('preprocess/background.wav') if audio['background'] is not None else None
    if background_art:
        files.append(background_art)
    guards = [dict(id=a.id, step=a.step_key, name=a.name, sha256=a.sha256,
                   size=a.size, rel_path=a.rel_path) for a in files]
    for item in files:
        _file(deps, item, verify_hash=verify_hash)
    mute = audio['filter']
    recipe = dict(schema=RENDER_VERSION, source_artifact_id=art.id, video_artifact_id=source.id,
        inputs=guards, definition={k: snapshot[k] for k in ('dubbing', 'burn_subtitles', 'include_comments')},
        values=values, source_comment_settings=(snapshot.get('comment_overlay') or {}).get('settings'),
        current_comment_plan=current_overlay, comment_overlay=overlay,
        review_receipt=content_redaction.receipt(ctx), audio_filter=mute,
        audio_processing=content_redaction.audio_fingerprint(ctx),
        encoder=dict(name=encoder, preset='p4' if encoder == 'h264_nvenc' else 'medium',
            rate_control='vbr' if encoder == 'h264_nvenc' else 'crf',
            quality_parameter='cq' if encoder == 'h264_nvenc' else 'crf', quality_value=values['assemble.crf'],
            note='NVENC CQ沿用冻结CRF数值作为本次选择，不代表与软件编码同等质量。' if encoder == 'h264_nvenc' else '软件CRF编码'))
    if overlay:
        # 原始 0 值无法区分旧自动窄版与当前可用全宽；成功配方保存实际规则。
        recipe['comment_width_rule'] = 'available-area-v1'
    return dict(ctx=ctx, snapshot=frozen, art=art, video=video, old=old, overlay=overlay, audio=audio,
                recipe=recipe, fingerprint=versions.digest(recipe))


def _state_signature(uid):
    """仅用于本次执行的前后核对；凭据只参与摘要，不保存或输出配置内容。"""
    project = crud.get_project(uid)
    if not project:
        raise ReviewRenderError('项目已被移除', code='PROJECT_NOT_FOUND')
    return versions.digest([project.source_kind, project.source_url, crud.project_params(project),
                            configuration.resolve(uid)['fingerprint'], config.load_config()])


def _revision_reason(code, message, **extra):
    return dict(code=code, message=message, **extra)


def _range_label(rows):
    """只展示核实的数值时段，不返回用户备注或正文。"""
    if not rows:
        return '无屏蔽时段'
    parts = [f'{float(r["start"]):g}–{float(r["end"]):g} 秒' for r in rows[:6]]
    return '；'.join(parts) + (f'（共 {len(rows)} 段）' if len(rows) > 6 else '')


def _manual_range_label(value):
    try:
        return _range_label(content_redaction.merge_ranges(content_redaction.parse_ranges(value or '')))
    except (DubForgeError, TypeError, ValueError):
        return '历史时段无法核对'


def _revision_audio(recipe):
    """旧修订只做全静音；没有实际处理时不因新默认策略而失效。"""
    if not recipe.get('audio_filter'):
        return None
    audio = recipe.get('audio_processing') or recipe.get('review_receipt', {}).get('audio_processing')
    if audio is None:
        return dict(effective_mode='all_audio')
    if not isinstance(audio, dict) or audio.get('effective_mode') not in {'voice_only', 'all_audio'}:
        raise ValueError('修订声音记录格式无效')
    result = dict(effective_mode=audio['effective_mode'])
    if audio['effective_mode'] == 'voice_only':
        result['background_identity'] = audio.get('background_identity')
        result['bgm_volume'] = audio.get('bgm_volume', recipe['values'].get('assemble.bgm_volume', 1))
    return result


def _revision_overlay(overlay):
    if not overlay:
        return None
    settings = comment_studio.render_fingerprint_plan(overlay)['settings']
    return dict(settings=settings, items=[dict(lines=comment_studio.display_lines(item, overlay['settings']),
        author=(item.get('author') or '匿名观众')[:48] if settings['show_author'] else None)
        for item in overlay['items']])


def _revision_recipe(recipe):
    """摘要先核验原始记录，再比较实际使用的配方；不重写旧记录。"""
    required = {'schema', 'source_artifact_id', 'video_artifact_id', 'inputs', 'definition', 'values',
                'comment_overlay', 'review_receipt', 'audio_filter', 'encoder'}
    if not isinstance(recipe, dict) or not required <= recipe.keys():
        raise ValueError('修订配方缺少历史')
    if recipe['schema'] not in {'review-render-v1', 'review-render-v2'}:
        raise ValueError('修订配方版本无法核对')
    if not isinstance(recipe['inputs'], list) or not recipe['inputs']:
        raise ValueError('修订输入历史无效')
    result = copy.deepcopy(recipe)
    result['schema'] = 'review-render-effects-v1'
    # 当前计划的样式随后被冻结样式覆盖；实际消费的内容与时序由 overlay 核验。
    result.pop('current_comment_plan', None)
    result.pop('source_comment_settings', None)
    if recipe['comment_overlay']:
        # 旧显式宽度没有改变；仅缺少规则记录的旧自动宽度需要重新制作。
        result['comment_width_rule'] = recipe.get('comment_width_rule',
            'legacy-auto' if not recipe['comment_overlay']['settings'].get('width_percent')
            else 'available-area-v1')
        if 'comments.width_percent' in result['values']:
            result['values']['comments.width_percent'] = result['values']['comments.width_percent'] or 100
    result['comment_overlay'] = _revision_overlay(recipe['comment_overlay'])
    result['audio_processing'] = _revision_audio(recipe)
    result['values'].pop('content_review.audio_mode', None)
    if result['values'].get('assemble.final_loudness_mode', 'off') == 'off':
        from dubforge.pipeline.audio_quality import FINAL_KEYS
        for key in FINAL_KEYS:
            result['values'].pop(key, None)
    result['review_receipt'].pop('audio_processing', None)
    return result


def _revision_values(version, audio):
    active = versions.active_values(version)
    # 旧混音已冻结，这三个开关不会在独立修订中再次应用。
    for key in ('assemble.voice_volume', 'assemble.ducking', 'assemble.loudnorm'):
        active.pop(key, None)
    if not audio or audio['effective_mode'] != 'voice_only':
        active.pop('assemble.bgm_volume', None)
    active = {k: v for k, v in active.items() if k in {f['path'] for f in versions.fields()}}
    if version['include_comments']:
        settings = {**comment_studio.DEFAULT_SETTINGS,
            **{k.removeprefix('comments.'): v for k, v in version['values'].items() if k.startswith('comments.')}}
        normalized = comment_studio.render_fingerprint_plan(dict(settings=settings, items=[]))['settings']
        active.update({'comments.' + k: v for k, v in normalized.items() if 'comments.' + k in version['values']})
    return active


def _recipe_reasons(old, new):
    reasons = []
    old_audio, new_audio = _revision_audio(old), _revision_audio(new)
    mode = lambda a: a['effective_mode'] if a else 'unchanged'
    if mode(old_audio) != mode(new_audio):
        labels = dict(all_audio='全部声音静音', voice_only='仅屏蔽人声、保留分离背景', unchanged='不处理声音')
        reasons.append(_revision_reason('REVIEW_AUDIO_MODE_CHANGED', '声音处理方式已更新', changes=[
            dict(label='实际声音处理', before=labels[mode(old_audio)], after=labels[mode(new_audio)])]))
    elif old_audio != new_audio:
        reasons.append(_revision_reason('REVIEW_BACKGROUND_CHANGED', '修订实际使用的分离背景或背景音量已变化'))
    old_values, new_values = old['values'], new['values']
    if old_values.get('content_review.blocked_ranges', '') != new_values.get('content_review.blocked_ranges', ''):
        reasons.append(_revision_reason('REVIEW_MANUAL_RANGES_CHANGED', '人工屏蔽时段已修改或清空', changes=[
            dict(label='人工屏蔽时段', before=_manual_range_label(old_values.get('content_review.blocked_ranges')),
                 after=_manual_range_label(new_values.get('content_review.blocked_ranges')))]))
    if old_values.get('content_review.blocked_terms', '') != new_values.get('content_review.blocked_terms', ''):
        reasons.append(_revision_reason('REVIEW_BLOCKED_TERMS_CHANGED', '屏蔽词设置已修改'))
    old_review, new_review = old['review_receipt'], new['review_receipt']
    if old_review.get('ranges') != new_review.get('ranges') or old['audio_filter'] != new['audio_filter']:
        reasons.append(_revision_reason('REVIEW_EFFECT_CHANGED', '实际屏蔽的字幕或声音时段已变化', changes=[
            dict(label='文字屏蔽时段', before=_range_label(old_review.get('ranges', [])),
                 after=_range_label(new_review.get('ranges', [])))]))
    guards = lambda recipe: {(r['step'], r['name']): r for r in recipe['inputs']}
    old_inputs, new_inputs = guards(old), guards(new)
    changed = {key[0] for key in old_inputs.keys() | new_inputs.keys() if old_inputs.get(key) != new_inputs.get(key)}
    for steps, code, message in [({'download'}, 'REVIEW_SOURCE_VIDEO_CHANGED', '登记的原视频已变化'),
        ({'assemble'}, 'REVIEW_ORIGINAL_OUTPUT_CHANGED', '修订所复用的旧成片或混音已变化'),
        ({'transcribe', 'split', 'translate'}, 'REVIEW_TEXT_CHANGED', '实际用于字幕或审查的文字资料已变化')]:
        if changed & steps:
            reasons.append(_revision_reason(code, message))
    if _revision_overlay(old['comment_overlay']) != _revision_overlay(new['comment_overlay']):
        reasons.append(_revision_reason('REVIEW_COMMENTS_CHANGED', '实际烧录的评论内容、时序或样式已变化'))
    if old_review.get('signature') != new_review.get('signature') and not changed & {'transcribe', 'split', 'translate'}:
        if not any(r['code'] in {'REVIEW_MANUAL_RANGES_CHANGED', 'REVIEW_BLOCKED_TERMS_CHANGED', 'REVIEW_EFFECT_CHANGED'} for r in reasons):
            reasons.append(_revision_reason('REVIEW_TEXT_CHANGED', '字幕校对文字或审查依据已变化'))
    if _revision_recipe(old) != _revision_recipe(new) and not reasons:
        reasons.append(_revision_reason('REVIEW_RECIPE_CHANGED', '修订实际使用的制作设置或审查处理已变化'))
    return reasons


def revision_state(deps, version, record=None, verify_hash=False):
    """独立修订的只读诊断；只返回安全标签、数值，不输出路径或正文。"""
    reasons = []
    failed = lambda code, message: dict(current=False, reasons=[_revision_reason(code, message)])
    try:
        record = record or versions.results(deps.project.uid).get(version['id'])
        snapshot = json.loads(record.snapshot) if record else {}
        info = snapshot.get('review_render')
        if not isinstance(info, dict) or not isinstance(info.get('recipe'), dict):
            return failed('REVIEW_HISTORY_MISSING', '修订缺少可核验的生成配方，请重新生成成片')
        recipe = info['recipe']
        if (versions.digest(recipe) != record.fingerprint or info.get('fingerprint') != record.fingerprint
                or info.get('schema') != recipe.get('schema') or info.get('source_artifact_id') != recipe.get('source_artifact_id')
                or info.get('encoder') != recipe.get('encoder')):
            return failed('REVIEW_HISTORY_INVALID', '修订生成记录与保存的摘要不一致，请重新生成成片')
        _revision_recipe(recipe)
        art = next((a for a in deps.arts.values() if a.id == record.artifact_id), None)
        if not art or art.sha256 != record.artifact_hash:
            return failed('REVIEW_OUTPUT_CHANGED', '修订文件的登记已丢失或文件身份已变化')
        _file(deps, art, verify_hash=verify_hash)
        original = next((a for a in deps.arts.values() if a.id == info['source_artifact_id']), None)
        original_record = next((r for r in versions.results(deps.project.uid).values()
                                if original and r.artifact_id == original.id), None)
        if not original or not original_record:
            return failed('REVIEW_ORIGINAL_OUTPUT_MISSING', '缺少本次修订所复用的旧成片或成功记录')
        prepared = _prepare(deps, original, original_record, verify_hash=verify_hash,
                            encoder=info.get('encoder', {}).get('name', 'libx264'))
        reasons.extend(_recipe_reasons(recipe, prepared['recipe']))
        # 摘要中的无关工作区样式变化不代表画面变化；其余消费差异仍必须阻断。
        if _revision_recipe(recipe) != _revision_recipe(prepared['recipe']) and not reasons:
            reasons.append(_revision_reason('REVIEW_RECIPE_CHANGED', '修订的实际制作配方已变化'))
        if any(version[k] != snapshot[k] for k in ('dubbing', 'burn_subtitles', 'include_comments')):
            reasons.append(_revision_reason('REVIEW_VERSION_CHANGED', '成片版本的声音、字幕或评论选择已变化'))
        # 两边按当前实际消费范围比较值；新增声音策略自身由上面的模式差异解释。
        audio = _revision_audio(prepared['recipe'])
        old_values = _revision_values(snapshot, audio)
        new_values = _revision_values(version, audio)
        if old_values != new_values:
            labels = {f['path']: f['label'] for f in versions.fields()}
            changed = [labels[k] for k in sorted(old_values.keys() | new_values.keys()) if old_values.get(k) != new_values.get(k)]
            reasons.append(_revision_reason('REVIEW_VERSION_SETTINGS_CHANGED', '本版本实际使用的设置已变化：' + '、'.join(changed)))
        if verify_hash:
            media, original_media = _media(_path(deps, art)), _media(prepared['video'])
            if not media['video'] or not media['audio'] or abs(media['duration'] - original_media['duration']) > .25:
                reasons.append(_revision_reason('REVIEW_MEDIA_INVALID', '修订文件的时长、画面或音轨无法与原视频对应'))
        return dict(current=not reasons, reasons=reasons)
    except (DubForgeError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, ReviewRenderError) and exc.code == 'REVIEW_RENDER_CUTS_UNSUPPORTED':
            return failed('REVIEW_RENDER_CUTS_UNSUPPORTED', exc.message)
        if isinstance(exc, ReviewRenderError) and exc.code == 'REVIEW_RENDER_SOURCE_CHANGED':
            return failed('REVIEW_FILE_MISSING_OR_CHANGED', '原视频、旧成片、修订文件或文字文件缺失，或内容与登记不一致')
        if isinstance(exc, DubForgeError) and exc.code in {'COMMENTS_NOT_CONFIRMED', 'COMMENTS_REVIEW_EMPTY', 'COMMENTS_DISPLAY_TEXT'}:
            return failed('REVIEW_COMMENTS_UNAVAILABLE', '当前没有可用的已确认评论方案，请先核对上屏评论')
        if isinstance(exc, ReviewRenderError) and exc.code == 'REVIEW_RENDER_DISABLED':
            return failed('REVIEW_DISABLED', '当前内容审查已关闭，请核对设置后重新生成成片')
        return failed('REVIEW_HISTORY_INVALID', '修订生成记录或当前输入无法核验，请重新生成成片')


def is_current(deps, version, record=None, verify_hash=False):
    """保留原布尔接口；独立修订不能用于正规生产步骤的缓存跳过。"""
    return revision_state(deps, version, record, verify_hash)['current']


def _idle(project, marker=None):
    from dubforge.runner import runner
    from dubforge.workflows import check_idle
    check_idle(project.uid)
    if dubbing_session._active.get(project.uid) is not marker:
        dubbing_session.check_idle(project.uid)
    if video_info._active.get(project.uid) != ('review_render' if marker else None):
        video_info.check_idle(project.uid)
    if any(s.status in {'running', 'queued'} or runner.current_job_id(project.id, s.step_key) is not None
           for s in crud.get_steps(project.id)):
        raise ReviewRenderError('项目正在运行或收尾，请结束后再修订成片', code='PIPELINE_BUSY')
    if marker is None:
        from dubforge import runtime_edit
        runtime_edit.check_idle(project.uid)


@contextmanager
def _editing(uid, marker=None):
    """与发布、步骤启动和资料编辑沿用一致锁序；编码期间不持这些全局锁。"""
    from dubforge.runner import runner
    from dubforge.publishing.service import output_replacement
    with output_replacement(uid), runner._start_lock, dubbing_session.gate, video_info.gate, configuration.gate:
        project = crud.get_project(uid)
        if not project:
            raise ReviewRenderError('项目不存在', code='PROJECT_NOT_FOUND')
        _idle(project, marker)
        yield project


def _cancel(check):
    if check is not None and (check.is_set() if hasattr(check, 'is_set') else check()):
        raise ReviewRenderError('成片修订已取消，旧文件保留', code='REVIEW_RENDER_CANCELLED')


def _render(prepared, folder, total, *, progress, cancel, index, count, version_id):
    from dubforge.pipeline.comment_overlay import prepare_filter
    from dubforge.pipeline.subtitle_style import prepare_subtitle_lines, video_canvas
    from dubforge.pipeline.version_render import subtitle_lines
    ctx = prepared['ctx']
    preflight_render(ctx, folder)
    ctx._audio_cancel = cancel
    filters = []
    if prepared['snapshot']['burn_subtitles']:
        lines = subtitle_lines(ctx, prepared['snapshot'])
        filters.append(prepare_subtitle_lines(lines, ctx.cfg, video_canvas(prepared['video']), folder, name='review'))
    graph, _ = prepare_filter(prepared['overlay'], prepared['video'], folder, duration=total)
    if graph:
        filters.append(graph)
    out = folder / 'video.mp4.tmp'
    cmd = ['ffmpeg', '-y', '-hide_banner', '-progress', 'pipe:1', '-nostats',
           '-i', str(prepared['video']), '-i', str(prepared['old'])]
    background_label = None
    if prepared['audio']['background'] is not None:
        cmd += ['-i', str(prepared['audio']['background'])]
        background_label = '2:a:0'
    reference, treatment = None, None
    master = None
    if ctx.cfg('assemble.final_loudness_mode', 'off') != 'off':
        from dubforge.pipeline.audio_quality import measure_mix, final_gain
        reference_graph = (content_redaction.apply_audio_review(prepared['audio'], '1:a:0', background_label=background_label)
                           if prepared['recipe']['audio_filter'] else '[1:a:0]anull[reviewed_audio]')
        if ctx.cfg('assemble.final_loudness_mode', 'off') == 'uniform':
            from dubforge.pipeline.audio_master import prepare_master
            master = prepare_master(ctx, cmd, [reference_graph], '[reviewed_audio]', duration=total)
            reference, treatment = master['reference'], master['processing']
            cmd += ['-i', str(master['path'])]
            master_input = cmd.count('-i') - 1
        else:
            reference = measure_mix(ctx, cmd, [reference_graph], '[reviewed_audio]', duration=total)
            treatment = final_gain(ctx, reference)
    cmd += ['-map', '0:v:0', '-map_metadata', '-1', '-map_chapters', '-1']
    if filters:
        encoder = prepared['recipe']['encoder']
        cmd += ['-vf', ','.join(filters), '-c:v', encoder['name'], '-preset', encoder['preset']]
        cmd += (['-rc', 'vbr', '-cq', str(encoder['quality_value']), '-b:v', '0']
                if encoder['name'] == 'h264_nvenc' else ['-crf', str(encoder['quality_value'])])
    else:
        cmd += ['-c:v', 'copy']
    if master is not None:
        cmd += ['-map', f'{master_input}:a:0', '-c:a', 'aac', '-b:a', '192k']
    elif prepared['recipe']['audio_filter'] or treatment is not None:
        graph = (content_redaction.apply_audio_review(prepared['audio'], '1:a:0', background_label=background_label)
                 if prepared['recipe']['audio_filter'] else '[1:a:0]anull[reviewed_audio]')
        audio_output = '[reviewed_audio]'
        if treatment is not None:
            from dubforge.pipeline.audio_quality import gain_filter
            addition, audio_output = gain_filter(audio_output, treatment)
            graph += ';' + addition
        cmd += ['-filter_complex', graph, '-map', audio_output, '-c:a', 'aac', '-b:a', '192k']
    else:
        cmd += ['-map', '1:a:0', '-c:a', 'copy']
    cmd += ['-t', f'{total:.6f}', '-movflags', '+faststart', '-f', 'mp4', str(out)]
    lines = queue.Queue()
    with tempfile.TemporaryFile(dir=folder) as errors:
        try:
            proc = subprocess.Popen(bounded_ffmpeg_command(cmd), cwd=folder, stdout=subprocess.PIPE, stderr=errors,
                                    text=True, encoding='utf-8', errors='replace')
        except OSError as exc:
            raise ffmpeg_failure(exc, output_dir=folder, fallback_code='REVIEW_RENDER_FFMPEG',
                                 message='审查修订编码失败，请检查 FFmpeg 日志') from exc
        def pump():
            try:
                for line in proc.stdout:
                    lines.put(line.strip())
            finally:
                proc.stdout.close()
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while proc.poll() is None or not lines.empty():
                _cancel(cancel)
                try:
                    line = lines.get(timeout=.2)
                except queue.Empty:
                    continue
                if progress and line.startswith('out_time_us='):
                    try:
                        seconds = max(0.0, min(total, float(line.partition('=')[2]) / 1_000_000))
                    except ValueError:
                        continue
                    progress(dict(phase='rendering', artifact_id=prepared['art'].id, version_id=version_id,
                                  index=index + 1, count=count, seconds=seconds, duration=total,
                                  percent=100 * (index + seconds / total) / count))
            if proc.wait() != 0:
                errors.seek(0)
                raise ffmpeg_failure(errors.read()[-4000:], output_dir=folder,
                                     fallback_code='REVIEW_RENDER_FFMPEG',
                                     message='审查修订编码失败，请检查 FFmpeg 日志')
        finally:
            if proc.poll() is None:
                from dubforge.runner import StepContext
                StepContext._kill_proc(proc)
                proc.wait(timeout=10)
            reader.join(timeout=5)
    from dubforge.pipeline.audio_quality import finalize
    prepared['audio_report'] = finalize(ctx, out, reference=reference, applied=treatment)
    from dubforge.pipeline.audio_comparison import augment_report
    prepared['audio_report'] = augment_report(ctx, prepared['audio_report'], prepared['video'],
                                              duration=total, full_duration=total)
    return out


def repair(uid, artifact_ids, *, progress=None, cancel=None, encoder='libx264'):
    """返回修订文件与版本；progress 接收阶段/实际编码秒数，cancel 可为回调或 Event。"""
    if (not isinstance(artifact_ids, list) or not 1 <= len(artifact_ids) <= 32
            or any(type(k) is not int or k <= 0 for k in artifact_ids) or len(set(artifact_ids)) != len(artifact_ids)):
        raise ReviewRenderError('请选择1–32个不重复的正式成片产物')
    if encoder not in ('libx264', 'h264_nvenc'):
        raise ReviewRenderError('修订编码器仅支持软件 libx264 或明确选择的 h264_nvenc')
    _cancel(cancel)
    marker = {'active_line': None, 'active_progress': None, 'operation': 'review_render'}
    with _editing(uid) as project:
        deps = Dependencies(project)
        saved = versions.results(uid)
        prepared = []
        for ident in artifact_ids:
            if progress:
                progress(dict(phase='validating', artifact_id=ident, count=len(artifact_ids)))
            art = next((a for a in deps.arts.values() if a.id == ident), None)
            record = next((r for r in saved.values() if r.artifact_id == ident), None)
            if not art or not record:
                raise ReviewRenderError('选中产物不属于本项目的已生成正式版本', code='REVIEW_RENDER_SOURCE')
            item = _prepare(deps, art, record, verify_hash=True, encoder=encoder)
            media, old_media = _media(item['video']), _media(item['old'])
            if not media['video'] or not old_media['video'] or not old_media['audio'] or abs(media['duration'] - old_media['duration']) > .25:
                raise ReviewRenderError('原视频与已有成片时长不一致或成片缺少音轨，不能复用已混音声音', code='REVIEW_RENDER_DURATION')
            item['duration'] = media['duration']
            item['version_id'] = f'review-{record.version_id[:40]}-{item["fingerprint"][:16]}'
            item['definition'] = versions.validate(dict(id=item['version_id'], name=item['snapshot']['name'][:73] + '（审查修订）',
                **{k: item['snapshot'][k] for k in ('dubbing', 'burn_subtitles', 'include_comments')},
                overrides={k: v for k, v in item['snapshot']['values'].items() if k in {f['path'] for f in versions.fields()}}))
            prepared.append(item)
        catalog = versions.catalog(uid)['versions']
        definitions = {v['id']: v for v in catalog}
        additions = [p['definition'] for p in prepared if p['version_id'] not in {v['id'] for v in catalog}]
        versions.validate_list(catalog + additions)
        state_signature = _state_signature(uid)
        dubbing_session._active[uid] = marker
        video_info._active[uid] = 'review_render'
    output = deps.root / 'output'
    changed_files = []
    try:
        output.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.tmp_review_', dir=output) as temp:
            repaired, pending = [], []
            for index, item in enumerate(prepared):
                _cancel(cancel)
                existing = saved.get(item['version_id'])
                current_version = versions.resolve(deps, definitions.get(item['version_id'], item['definition']))
                if existing and is_current(deps, current_version, existing, verify_hash=True):
                    art = next(a for a in deps.arts.values() if a.id == existing.artifact_id)
                    repaired.append(dict(source_artifact_id=item['art'].id, artifact_id=art.id,
                        version_id=item['version_id'], path=str(_path(deps, art)), sha256=art.sha256,
                        receipt=item['recipe']['review_receipt'], reused=True))
                    continue
                folder = Path(temp) / str(index)
                folder.mkdir()
                encoded = _render(item, folder, item['duration'], progress=progress, cancel=cancel,
                                  index=index, count=len(prepared), version_id=item['version_id'])
                media = _media(encoded)
                if not media['video'] or not media['audio'] or abs(media['duration'] - item['duration']) > .25:
                    raise ReviewRenderError('修订输出时长或音轨校验失败，结果未保存', code='REVIEW_RENDER_DURATION')
                filename = f'final_version_{item["version_id"]}.mp4'
                final = output / filename
                if final.exists():
                    raise ReviewRenderError('修订目标文件已存在但无法验证有效，请保留旧文件并另行核对', code='REVIEW_RENDER_CONFLICT')
                generated_at = datetime.now(timezone.utc).isoformat()
                snapshot = {**item['snapshot'], **item['definition'], 'comment_overlay': item['overlay'],
                    'generated_at': generated_at,
                    'content_review_receipt': item['recipe']['review_receipt'],
                    'review_render': dict(schema=RENDER_VERSION, source_artifact_id=item['art'].id,
                        fingerprint=item['fingerprint'], recipe=item['recipe'], encoder=item['recipe']['encoder'],
                        created_at=generated_at,
                        note='基于当前文字和已有混音的审查修订；命中段按当前模式处理并补回可用分离背景，未重新进行语义整理、翻译或语音合成。')}
                receipt = folder / 'receipt.json.tmp'
                atomic_write_bytes(receipt, json.dumps(snapshot['review_render'], ensure_ascii=False, indent=2).encode())
                pending.append((item, encoded, final, receipt, snapshot, _sha(encoded)))
            with _editing(uid, marker) as project:
                _cancel(cancel)
                if _state_signature(uid) != state_signature:
                    raise ReviewRenderError('修订期间项目资料或配置已变化，请重新核定后再生成', code='REVIEW_RENDER_CONFLICT')
                fresh = Dependencies(project)
                for item in prepared:
                    art = next((a for a in fresh.arts.values() if a.id == item['art'].id), None)
                    record = next((r for r in versions.results(uid).values() if art and r.artifact_id == art.id), None)
                    if not art or not record or _prepare(fresh, art, record, verify_hash=True,
                            encoder=encoder)['fingerprint'] != item['fingerprint']:
                        raise ReviewRenderError('修订输入已变化，结果未保存', code='REVIEW_RENDER_CONFLICT')
                if pending:
                    with crud.session() as session:
                        scope = configuration._row(session, 'project:' + uid)
                        current = configuration._resolve(session, scope.key)
                        patch = json.loads(scope.values)
                        old_catalog = current['values']['assembly.versions']
                        patch['assembly.versions'] = versions.validate_list(old_catalog + [p['definition'] for p, *_ in pending
                            if p['version_id'] not in {v['id'] for v in old_catalog}])
                        configuration._validate(patch, current['defaults'])
                        scope.values, scope.revision = json.dumps(patch, ensure_ascii=False), scope.revision + 1
                        session.add(scope)
                        for item, encoded, final, receipt, snapshot, sha in pending:
                            art = Artifact(project_id=project.id, step_key='assemble', kind='video', name=final.name,
                                rel_path=str(final.relative_to(fresh.root)), sha256=sha, size=encoded.stat().st_size)
                            session.add(art); session.flush()
                            session.add(AssemblyResult(key=f'{uid}:{item["version_id"]}', project_uid=uid,
                                version_id=item['version_id'], artifact_id=art.id, artifact_hash=sha,
                                fingerprint=item['fingerprint'], snapshot=json.dumps(snapshot, ensure_ascii=False)))
                            final_receipt = final.with_suffix('.review.json')
                            if final.exists() or final_receipt.exists():
                                raise ReviewRenderError('修订文件名已占用，结果未保存', code='REVIEW_RENDER_CONFLICT')
                            atomic_replace(encoded, final); changed_files.append(final)
                            from dubforge.pipeline.audio_quality import write_report, report_path
                            write_report(final, item['audio_report']); changed_files.append(report_path(final))
                            atomic_replace(receipt, final_receipt); changed_files.append(final_receipt)
                            repaired.append(dict(source_artifact_id=item['art'].id, artifact_id=art.id,
                                version_id=item['version_id'], path=str(final), sha256=sha,
                                receipt=item['recipe']['review_receipt'], reused=False))
                        session.commit()
                    changed_files.clear()
                    crud.write_manifest(uid)
            if progress:
                progress(dict(phase='complete', percent=100, count=len(repaired)))
            order = {ident: i for i, ident in enumerate(artifact_ids)}
            return dict(project_uid=uid, repaired=sorted(repaired, key=lambda r: order[r['source_artifact_id']]))
    except OSError as exc:
        raise ReviewRenderError('修订文件处理失败，旧成片保留；请检查磁盘与文件占用后重试',
                                code='REVIEW_RENDER_IO', detail=str(exc)) from exc
    finally:
        # 只清理由本次新建且尚未登记的修订文件，绝不删除旧成片或发布快照。
        for path in changed_files:
            path.unlink(missing_ok=True)
        with dubbing_session.gate, video_info.gate:
            if dubbing_session._active.get(uid) is marker:
                dubbing_session._active.pop(uid, None)
            if video_info._active.get(uid) == 'review_render':
                video_info._active.pop(uid, None)
