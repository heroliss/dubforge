"""按需生成短小的原声核对片段；不改成片、字幕或制作状态。"""
import hashlib
import json
import math
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from dubforge import content_review_service
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.content_redaction import audio_plan, apply_audio_review, overlaps, plan
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.subtitle_document import read
from dubforge.pipeline.subtitle_style import PREVIEW_KEYS, prepare_subtitle_lines, video_canvas
from dubforge.pipeline.video_source import resolve_video
from dubforge.utils.fs import atomic_replace, atomic_write_bytes
from dubforge.utils.srt import SubLine

_ENCODING = threading.BoundedSemaphore(1)
_VERSION = 'source-comparison-v2'
_MAX_SECONDS = 20.0


class ReviewPreviewError(DubForgeError):
    code = 'CONTENT_REVIEW_PREVIEW_FAILED'
    suggestion = '请确认原视频可用，刷新审查结果后重试。'


def _evidence(uid, body):
    if not isinstance(body, dict):
        raise ReviewPreviewError('片段核对参数无效')
    current = content_review_service.report(uid)
    found = next((row for row in current['findings'] + current['original_findings']
                  if row.get('key') == body.get('key')), None)
    if (not found or any(body.get(key) != current.get(key) for key in
                        ('decision_revision', 'revision', 'configuration_fingerprint'))
            or body.get('evidence_fingerprint') != found.get('evidence_fingerprint')):
        raise ReviewPreviewError('文字、设置或复核记录已变化，请刷新后重新核对片段',
                                 code='CONTENT_REVIEW_PREVIEW_CONFLICT')
    return current, found


def _clip(finding, total):
    starts = [finding.get('start'), finding.get('source_audio_start')]
    ends = [finding.get('end'), finding.get('source_audio_end')]
    starts = [float(x) for x in starts if type(x) in (float, int) and math.isfinite(x)]
    ends = [float(x) for x in ends if type(x) in (float, int) and math.isfinite(x)]
    if not starts or not ends or min(starts) >= total:
        raise ReviewPreviewError('命中时段不在原视频范围内，暂不能核对这个片段')
    focus_start, focus_end = min(starts), max(ends)
    start = max(0.0, focus_start - 2.0)
    end = min(total, focus_end + 2.0, start + _MAX_SECONDS)
    if end <= start:
        raise ReviewPreviewError('没有可核对的视频区间')
    return dict(start=start, end=end, focus_start=focus_start, focus_end=focus_end,
                truncated=end < min(total, focus_end + 2.0))


def _prepare(uid, body):
    current, finding = _evidence(uid, body)
    project = crud.get_project(uid)
    deps = Dependencies(project)
    video = resolve_video(project, deps.root)
    if not video:
        raise ReviewPreviewError('原视频尚未就绪，暂不能核对声音和画面', code='VIDEO_MISSING')
    from dubforge.pipeline.step_assemble import _ffprobe_duration
    total = _ffprobe_duration(video)
    if not math.isfinite(total) or total <= 0:
        raise ReviewPreviewError('无法读取原视频时长，请检查源文件')
    clip = _clip(finding, total)
    current_plan = plan(deps)
    try:
        rows = read(deps, translation=False, strict=False)
    except DubForgeError as exc:
        if exc.code not in {'DEPS_NOT_MET', 'ARTIFACT_MISSING'}:
            raise
        rows = []
    stat = video.stat()
    settings = {key: deps.cfg(key) for key in PREVIEW_KEYS - {'assemble.burn_subtitles'}}
    audio = audio_plan(deps, start=clip['start'], duration=clip['end'] - clip['start'])
    audio_processing = {key: audio[key] for key in (
        'requested_mode', 'effective_mode', 'background_identity', 'fallback_reason', 'bgm_volume')}
    snapshot = dict(version=_VERSION, source=str(video.resolve()), size=stat.st_size,
                    modified=stat.st_mtime_ns, clip=clip, rows=rows, settings=settings,
                    review=current_plan.get('signature'), ranges=current_plan['ranges'],
                    audio=dict(processing=audio_processing, filter=audio['filter']))
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return deps, video, rows, current_plan, dict(key=finding['key'], **clip,
        fingerprint=fingerprint, muted_ranges=audio['ranges'], audio_processing=audio_processing,
        scope='source_audio'), audio if audio['filter'] else ''


def _cues(deps, rows, ranges, start, end, *, processed):
    """沿用字幕的轨道、双语顺序、最短显示时间与屏蔽边界。"""
    from dubforge.pipeline.step_subtitles import _extend_short_cues
    track = deps.cfg('assemble.subtitle_track', 'trans')
    source_first = deps.cfg('subtitles.source_first', False)
    cues, chosen = [], []
    for row in rows:
        if processed and overlaps(row['start'], row['end'], ranges):
            continue
        source, target = row.get('source_text', ''), row.get('text', '')
        if track == 'source':
            parts = (('source', source),)
        elif track == 'trans':
            parts = (('trans', target),)
        else:
            parts = (('source', source), ('trans', target)) if source_first else (
                ('trans', target), ('source', source))
        text = '\n'.join(value for _, value in parts if value)
        if text:
            chosen.append(row)
            cues.append(SubLine(row['id'], row['start'], row['end'], text, parts))
    _extend_short_cues(cues, deps.cfg('subtitles.min_duration', 1.5),
                       fixed_ids={row['id'] for row in chosen if 'voice_timing' in row},
                       barriers=ranges if processed else ())
    return [SubLine(c.index, max(0.0, c.start - start), min(end, c.end) - start, c.text, c.parts)
            for c in cues if c.start < end and c.end > start]


def _encode(video, target, deps, cues, clip, audio):
    with tempfile.TemporaryDirectory(prefix='.review-preview-', dir=target.parent) as folder:
        # 缩小画面并限制线程；短片核对不会重新编码整部视频。
        filters = ["scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", 'setsar=1']
        if cues:
            filters.append(prepare_subtitle_lines(cues, deps.cfg, video_canvas(video), Path(folder)))
        command = ['ffmpeg', '-nostdin', '-y', '-hide_banner', '-loglevel', 'error',
                   '-ss', f"{clip['start']:.6f}", '-i', str(video.resolve())]
        if audio and audio['background'] is not None:
            command += ['-ss', f"{clip['start']:.6f}", '-i', str(audio['background'])]
        command += ['-t', f"{clip['end'] - clip['start']:.6f}", '-map', '0:v:0']
        if audio and audio['background'] is not None:
            command += ['-filter_complex', apply_audio_review(audio, '0:a:0', background_label='1:a:0'),
                        '-map', '[reviewed_audio]']
        else:
            command += ['-map', '0:a:0?']
            if audio:
                command += ['-af', audio['filter']]
        command += [
                   '-vf', ','.join(filters), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '25',
                   '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k']
        command += ['-movflags', '+faststart', '-f', 'mp4', str(target.resolve())]
        try:
            result = subprocess.run(command, cwd=folder, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReviewPreviewError('核对片段生成失败，请检查 FFmpeg 或稍后重试', detail=str(exc)) from exc
        if result.returncode or not target.is_file() or target.stat().st_size == 0:
            raise ReviewPreviewError('核对片段生成失败，请检查视频、字幕样式及 FFmpeg',
                                     detail=result.stderr.decode('utf-8', 'replace')[-2000:])


def _folder(project):
    return crud.project_dir_by_project(project) / 'artifacts' / 'content_review_preview'


def media(uid, fingerprint, mode):
    """只允许读取本项目生成的核对片段，不接受客户端文件路径。"""
    project = crud.get_project(uid)
    if not project:
        raise ReviewPreviewError('项目不存在', code='PROJECT_NOT_FOUND')
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint) or mode not in {'original', 'processed'}:
        raise ReviewPreviewError('核对片段地址无效')
    path = _folder(project) / f'{fingerprint}.{mode}.mp4'
    if not path.is_file() or not path.with_name(f'{fingerprint}.json').is_file():
        raise ReviewPreviewError('核对片段已清理，请重新生成', code='ARTIFACT_MISSING')
    return path


def _prune(folder, keep):
    # 每个项目最多保留近期16组；正在播放而暂时被占用的文件留待下次清理。
    records = sorted(folder.glob('*.json'), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for record in records[16:]:
        if record.stem == keep or not re.fullmatch(r'[0-9a-f]{64}', record.stem):
            continue
        for suffix in ('.original.mp4', '.processed.mp4', '.json'):
            try:
                record.with_name(record.stem + suffix).unlink(missing_ok=True)
            except OSError:
                pass


def generate(uid, body):
    if not _ENCODING.acquire(blocking=False):
        raise ReviewPreviewError('已有核对片段正在生成，请稍后重试', code='CONTENT_REVIEW_PREVIEW_BUSY')
    pending = []
    try:
        deps, video, rows, current, result, audio = _prepare(uid, body)
        folder = _folder(deps.project)
        folder.mkdir(parents=True, exist_ok=True)
        digest = result['fingerprint']
        record = folder / f'{digest}.json'
        if record.is_file() and all((folder / f'{digest}.{mode}.mp4').is_file() for mode in ('original', 'processed')):
            _evidence(uid, body)
            return {**json.loads(record.read_text(encoding='utf-8')), 'key': result['key']}
        # 通用审查面板没有指定成片版本；短片始终展示当前字幕轨以便核对规则。
        # 成片是否烧录仍由各版本决定，不能用旧的项目烧录开关猜版本效果。
        cue_sets = {mode: _cues(deps, rows, current['ranges'], result['start'], result['end'],
                              processed=mode == 'processed') for mode in ('original', 'processed')}
        result['subtitles_available'] = bool(cue_sets['original'])
        shown_ids = {cue.index for cue in cue_sets['processed']}
        result['hidden_ids'] = [cue.index for cue in cue_sets['original'] if cue.index not in shown_ids]
        result['warnings'] = ['原声片段核对仅演示当前字幕与静音规则，未混入中文配音或评论；请另行核对最终成片。']
        result['warnings'].append('核对短片展示当前字幕轨；成片是否烧录字幕以其版本方案为准。')
        if audio and audio['fallback_reason']:
            result['warnings'].append(audio['fallback_reason'])
        elif audio and audio['effective_mode'] == 'voice_only':
            result['warnings'].append('命中时段已用分离背景音补齐，请试听确认背景中没有可辨识的原话。')
        if result['truncated']:
            result['warnings'].append('该时段较长，本次仅核对前20秒，请继续在原视频中检查后续内容。')
        if deps.cfg('assemble.subtitle_track', 'trans') != 'source' and any(not row.get('text', '').strip() for row in rows):
            result['warnings'].append('部分句子尚无译文，片段只展示现有文字，不能代表字幕已完成。')
        for mode in ('original', 'processed'):
            target = folder / f'{digest}.{mode}.mp4'
            temp = target.with_suffix('.mp4.tmp')
            pending.append((temp, target))
            _encode(video, temp, deps, cue_sets[mode], result, audio if mode == 'processed' else '')
            result[mode + '_url'] = f'/api/projects/{uid}/content-review/preview/{digest}/{mode}'
        # 编码期间如果文字、选用配音尾音、源文件或规则变化，不发布旧效果。
        fresh = _prepare(uid, body)[4]
        if fresh['fingerprint'] != digest:
            raise ReviewPreviewError('生成期间项目资料已变化，请刷新后重新核对', code='CONTENT_REVIEW_PREVIEW_CONFLICT')
        for temp, target in pending:
            atomic_replace(temp, target)
        atomic_write_bytes(record, json.dumps(result, ensure_ascii=False).encode('utf-8'))
        _prune(folder, digest)
        return result
    finally:
        for temp, _ in pending:
            temp.unlink(missing_ok=True)
        _ENCODING.release()
