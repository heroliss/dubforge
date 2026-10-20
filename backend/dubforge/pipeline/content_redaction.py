"""把可核查的文字筛查结果应用到字幕和最终混音，不修改原始资料。"""
import copy
import hashlib
import json
import math
import re
from pathlib import Path
from functools import lru_cache

from dubforge.errors import DubForgeError


class ContentRedactionError(DubForgeError):
    code = 'CONTENT_REVIEW_INVALID'
    suggestion = '请检查内容审查的屏蔽词、时段与字幕时间轴后重试。'


def parse_ranges(value):
    if not isinstance(value, str) or len(value) > 20000:
        raise ContentRedactionError('人工屏蔽时段需为每行「开始秒-结束秒:原因」的文字')
    result = []
    for number, raw in enumerate(value.splitlines(), 1):
        if not raw.strip():
            continue
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(?:[:：]\s*(.*))?', raw)
        if not match:
            raise ContentRedactionError(f'人工屏蔽时段第{number}行格式无效，请使用「开始秒-结束秒:原因」')
        start, end = float(match[1]), float(match[2])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start or end > 36000:
            raise ContentRedactionError(f'人工屏蔽时段第{number}行需满足 0 ≤ 开始 < 结束 ≤ 36000')
        result.append(dict(start=start, end=end, reason=match[3] or '人工核定的屏蔽时段'))
    if len(result) > 200:
        raise ContentRedactionError('最多设置200个屏蔽时段')
    return result


def validate_settings(values):
    from dubforge.content_review import normalize_terms
    for key in ('content_review.enabled', 'content_review.mute_audio'):
        if key in values and type(values[key]) is not bool:
            raise ContentRedactionError('内容筛查与静音开关必须明确选择开启或关闭')
    if 'content_review.audio_mode' in values and values['content_review.audio_mode'] not in ('voice_only', 'all_audio'):
        raise ContentRedactionError('请选择有效的声音屏蔽方式')
    if 'content_review.blocked_terms' in values:
        normalize_terms(values['content_review.blocked_terms'])
    if 'content_review.blocked_ranges' in values:
        parse_ranges(values['content_review.blocked_ranges'])
    if values.get('content_review.audio_mode', 'voice_only') not in {'voice_only', 'all_audio'}:
        raise ContentRedactionError('音频屏蔽方式请选择仅屏蔽语音或全部声音静音')


def merge_ranges(ranges, *, start=0.0, duration=None, padding=0.0):
    stop = start + duration if duration is not None else math.inf
    result = []
    for row in sorted(ranges, key=lambda r: r['start']):
        left = max(start, float(row['start']) - padding)
        right = min(stop, float(row['end']) + padding)
        if right <= left:
            continue
        left, right = left - start, right - start
        if result and left <= result[-1]['end']:
            result[-1]['end'] = max(result[-1]['end'], right)
        else:
            result.append(dict(start=left, end=right))
    return result


def overlaps(start, end, ranges):
    return any(start < r['end'] and end > r['start'] for r in ranges)


def plan(ctx):
    """同一次冻结的运行上下文只检查一次；没有文字资料不等于已经通过。"""
    manifest = (getattr(ctx, 'version', None) or {}).get('render_manifest')
    if manifest is not None:
        return copy.deepcopy(manifest['review']['plan'])
    from dubforge.content_review import REVIEW_VERSION, review_lines, settings_from_cfg
    from dubforge import content_review_decisions as decisions
    from dubforge.pipeline.subtitle_document import read
    enabled, terms = settings_from_cfg(ctx.cfg)
    if not enabled:
        return dict(enabled=False, findings=[], ranges=[], blocked_ids=[], checked_lines=0)
    cached = getattr(ctx, '_content_redaction_plan', None)
    if cached is not None:
        return cached
    manual = parse_ranges(ctx.cfg('content_review.blocked_ranges', '') or '')
    try:
        rows = read(ctx, translation=False, strict=False)
    except (FileNotFoundError, OSError):
        rows = []
    except DubForgeError as exc:
        if exc.code not in {'DEPS_NOT_MET', 'ARTIFACT_MISSING'}:
            raise
        rows = []
    store = getattr(ctx, '_content_review_decision_store', None) or decisions.load(ctx.project)
    findings = decisions.bind_lines(review_lines(rows, terms), rows, 'subtitle', store)
    ranges = manual + [dict(start=f['start'], end=f['end']) for f in findings if f['active']]
    for f in findings:
        if f['active'] and f.get('source_audio_start') is not None:
            ranges.append(dict(start=f['source_audio_start'], end=f['source_audio_end']))
    # 原始识别稿也参与筛查，避免整理/校对时遗漏原话后误认为原声已经改变。
    try:
        from dubforge.utils.srt import parse_srt
        raw_path = ctx.upstream('transcribe', 'src.srt')
        original = [dict(id=l.index, start=l.start, end=l.end, source_text=l.text, text='')
                    for l in parse_srt(raw_path)]
    except (FileNotFoundError, OSError, DubForgeError, KeyError, AttributeError):
        original = []
    raw_findings = decisions.bind_lines(review_lines(original, terms), original, 'original', store)
    ranges += [dict(start=f['start'], end=f['end']) for f in raw_findings if f['active']]
    blocked = {l['id'] for l in rows if overlaps(l['start'], l['end'], ranges)}
    result = dict(enabled=True, findings=findings, original_findings=raw_findings,
                  ranges=merge_ranges(ranges), blocked_ids=sorted(blocked), checked_lines=len(rows))
    result['original_checked_lines'] = len(original)
    result['original_checked_text_lines'] = sum(bool(row.get('source_text', '').strip()) for row in original)
    result['decision_revision'] = str(store['revision'])
    result['source_checked_lines'] = sum(bool(row.get('source_text', '').strip()) for row in rows)
    result['translated_checked_lines'] = sum(bool(row.get('text', '').strip()) for row in rows)
    result['warnings'] = [] if original else ['未找到可核对的原始识别稿；当前仅检查整理文字，请另行听验原声。']
    signature = dict(version=REVIEW_VERSION, lines=rows, original=original, terms=terms,
                     manual=manual, mute_audio=ctx.cfg('content_review.mute_audio', True))
    automatic = manual + [dict(start=f['start'], end=f['end']) for f in findings + raw_findings]
    automatic += [dict(start=f['source_audio_start'], end=f['source_audio_end']) for f in findings
                  if f.get('source_audio_start') is not None]
    # 只有改变实际字幕/音轨时段的误报允许进入制作指纹；确认和备注不改变产物。
    if merge_ranges(automatic) != result['ranges']:
        result['decision_effect'] = result['ranges']
        signature['effective_ranges'] = result['ranges']
    result['signature'] = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # VersionContext的父上下文可能同轮渲染多个版本，缓存只放在当前实例。
    ctx._content_redaction_plan = result
    return result


def receipt(ctx):
    from dubforge.content_review import REVIEW_VERSION
    current = plan(ctx)
    audio = audio_plan(ctx)
    return dict(version=REVIEW_VERSION, signature=current.get('signature'),
                ranges=current['ranges'], mute_audio=ctx.cfg('content_review.mute_audio', True),
                audio_processing={k: audio[k] for k in ('requested_mode', 'effective_mode',
                    'background_identity', 'fallback_reason')})


def fingerprint_effect(ctx):
    """没有时段误报允许时沿用旧指纹，普通状态读取无需额外解析文字产物。"""
    from dubforge import content_review_decisions as decisions
    store = getattr(ctx, '_content_review_decision_store', None) or decisions.load(ctx.project)
    if not any(row['decision'] == 'allow' and not key.startswith('comment:') for key, row in store['entries'].items()):
        return None
    return plan(ctx).get('decision_effect')


def redact_document(ctx, rows):
    current = plan(ctx)
    if not current['enabled']:
        return rows
    return [l for l in rows if l['id'] not in current['blocked_ids']
            and not overlaps(l['start'], l['end'], current['ranges'])]


def filter_cues(ctx, cues):
    current = plan(ctx)
    return [l for l in cues if not overlaps(l.start, l.end, current['ranges'])]


def audio_ranges(ctx, *, start=0.0, duration=None):
    """导出实际静音时段，供原视频试听与正式混音共用。"""
    if not ctx.cfg('content_review.mute_audio', True):
        return []
    current = plan(ctx)
    ranges = list(current['ranges'])
    if ranges and getattr(ctx, 'project', None) is not None:
        # 选用配音可能长于字幕；静音覆盖整段实际选用语音，防止尾音泄漏。
        try:
            from dubforge.pipeline.dependencies import Dependencies
            from dubforge.pipeline.dubbing_edit import production_lines, take_name
            import soundfile as sf
            deps = Dependencies(ctx.project)
            lines = production_lines(ctx.project, deps.dubbing_lines(), deps.root, dependencies=deps)
            for l in lines:
                if l['source'] != 'dub' or l['take'] is None or not overlaps(l['start'], l['end'], ranges):
                    continue
                path = deps.path('tts', take_name(l['id'], l['take']))
                if path and path.is_file():
                    info = sf.info(str(path))
                    ranges.append(dict(start=l['start'], end=max(l['end'], l['start'] + info.duration)))

            # 命中判定始终使用源稿身份/范围；实际屏蔽同时覆盖配音最终
            # 播放位置。否则自动或人工滑出源范围的完整声音会绕过静音。
            version = getattr(ctx, 'version', None) or getattr(getattr(ctx, 'deps', None), 'version', None)
            if not version or version.get('dubbing', True):
                layout = getattr(ctx, '_voice_layout', None) or getattr(ctx, '_content_redaction_voice_layout', None)
                if layout is None and version and version.get('dubbing'):
                    # 多版本渲染复用同一混音文件时，当前 VersionContext 未
                    # 执行 _mix_timeline；仍按该版本冻结配置派生同一时钟。
                    from dubforge.pipeline import voice_timing
                    inputs = [{**row, 'source': row['source'] if row['source'] == 'original'
                               else 'dub' if row.get('take') is not None else 'silent'} for row in lines]
                    layout = voice_timing.plan(ctx.project, deps.root, inputs, ctx.cfg)
                    ctx._content_redaction_voice_layout = layout
                blocked = set(current['blocked_ids'])
                for row in (layout or {}).get('timing', {}).get('lines', []):
                    if (row['source'] != 'dub' or row.get('audio_end') is None
                            or (row['id'] not in blocked and not overlaps(
                                row['source_start'], row['source_end'], current['ranges']))):
                        continue
                    ranges.append(dict(start=row['applied_start'],
                                       end=max(row['audio_end'], row['applied_end'])))
        except (FileNotFoundError, OSError, RuntimeError, DubForgeError):
            # 原声版不要求具备可用配音；字幕与原声时段仍会处理。
            pass
    return merge_ranges(ranges, start=start, duration=duration, padding=0.15)


def audio_filter(ctx, *, start=0.0, duration=None):
    visible = audio_ranges(ctx, start=start, duration=duration)
    if not visible:
        return ''
    expression = '+'.join(f'between(t,{r["start"]:.6f},{r["end"]:.6f})' for r in visible)
    return f"volume=0:enable='{expression}'"


@lru_cache(maxsize=128)
def _video_duration(path, size, mtime_ns, inode):
    # 文件版本参与缓存键，原视频替换后重新读取；仅读取时长，不消费原声音轨。
    from dubforge.pipeline.step_assemble import _ffprobe_duration
    return _ffprobe_duration(Path(path))


def audio_plan(ctx, *, start=0.0, duration=None):
    """统一声音处理策略；背景只能来自分离产物，无法使用时安全降级为全静音。"""
    manifest = (getattr(ctx, 'version', None) or {}).get('render_manifest')
    if manifest is not None:
        from dubforge.pipeline.assembly_manifest import media_path
        result = copy.deepcopy(manifest['review']['audio_plan'])
        result['ranges'] = merge_ranges(result['ranges'], start=start, duration=duration)
        expression = '+'.join(f'between(t,{r["start"]:.6f},{r["end"]:.6f})' for r in result['ranges'])
        result['filter'] = f"volume=0:enable='{expression}'" if expression else ''
        deps = ctx.dependencies() if callable(getattr(ctx, 'dependencies', None)) else ctx
        result['background'] = (media_path(deps, manifest['media']['background'])
                                if result.get('background') and manifest['media'].get('background') else None)
        return result
    import soundfile as sf
    mode = ctx.cfg('content_review.audio_mode', 'voice_only')
    if mode not in ('voice_only', 'all_audio'):
        raise ContentRedactionError('音频屏蔽方式无效，请重新选择')
    ranges = audio_ranges(ctx, start=start, duration=duration)
    # 没有屏蔽时段时不探测整片媒体，避免普通状态轮询引入额外子进程。
    if ranges and duration is None and getattr(ctx, 'project', None) is not None:
        from dubforge.pipeline.video_source import resolve_video
        root = getattr(ctx, 'pdir', None) or getattr(ctx, 'root', None)
        try:
            video = resolve_video(ctx.project, root) if root is not None else None
            if video is not None:
                version = video.stat()
                total = _video_duration(str(video.resolve()), version.st_size, version.st_mtime_ns, version.st_ino)
                if total > 0:
                    duration = max(0.0, total - start)
        except (OSError, ValueError, RuntimeError, DubForgeError):
            pass
        if duration is not None:
            # 时段已经相对 start 偏移并加过保护边界，此处仅裁剪视频尾部。
            ranges = merge_ranges(ranges, duration=duration)
    expression = '+'.join(f'between(t,{r["start"]:.6f},{r["end"]:.6f})' for r in ranges)
    result = dict(requested_mode=mode, effective_mode=mode if ranges else 'unchanged',
                  ranges=ranges, filter=f"volume=0:enable='{expression}'" if ranges else '',
                  background=None, background_identity=None, fallback_reason='',
                  bgm_volume=float(ctx.cfg('assemble.bgm_volume', 1.0)))
    if not ranges or mode == 'all_audio':
        return result
    reason = ''
    if not ctx.cfg('separate.enabled', True):
        reason = '人声与背景分离已关闭，命中时段改为全部声音静音。'
    elif result['bgm_volume'] <= 0:
        reason = '背景混入音量为 0，命中时段保持全部声音静音。'
    else:
        try:
            # 不从视频音轨或 audio.wav 回退；这两者仍可能包含待屏蔽的原声。
            background = Path(ctx.upstream('preprocess', 'background.wav'))
            info, version = sf.info(str(background)), background.stat()
            if info.frames <= 0 or info.duration <= 0:
                raise OSError('分离背景没有音频帧')
            deps = getattr(ctx, 'deps', None) or (ctx if hasattr(ctx, 'arts') else None)
            if deps is None and callable(getattr(ctx, 'dependencies', None)):
                deps = ctx.dependencies()
            art = deps.arts.get('preprocess/background.wav') if deps is not None else None
            registered = Path(art.rel_path) if art is not None else None
            if registered is not None and not registered.is_absolute():
                registered = deps.root / registered
            if registered is not None and (background.resolve() != registered.resolve() or version.st_size != art.size):
                reason = '分离背景与登记产物不一致，命中时段改为全部声音静音；请重新完成人声与背景分离。'
            elif info.duration + 1 / max(info.samplerate, 1) < start + max(row['end'] for row in ranges):
                reason = '分离背景长度不足以覆盖命中时段，命中时段改为全部声音静音。'
            elif art is not None and not deps.fresh('preprocess'):
                reason = '分离背景已过期，命中时段改为全部声音静音；请重新完成人声与背景分离。'
            else:
                result['background'] = background
                result['background_identity'] = dict(sha256=art.sha256 if art else None,
                    size=version.st_size, mtime_ns=version.st_mtime_ns, inode=version.st_ino)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, DubForgeError):
            reason = '没有可用的分离背景，命中时段改为全部声音静音；原视频音轨不会用作背景。'
    if reason:
        result.update(effective_mode='all_audio', fallback_reason=reason)
    return result


def apply_audio_review(audio, source_label, *, background_label=None, output_label='reviewed_audio'):
    """输入已有混音时，仅在屏蔽段补回独立背景；标签不含方括号。"""
    source, output = f'[{source_label}]', f'[{output_label}]'
    if not audio['filter']:
        return f'{source}anull{output}'
    if audio['effective_mode'] != 'voice_only' or background_label is None:
        return f"{source}{audio['filter']}{output}"
    expression = '+'.join(f'between(t,{r["start"]:.6f},{r["end"]:.6f})' for r in audio['ranges'])
    base, background = f'[{output_label}_base]', f'[{output_label}_background]'
    return (f"{source}{audio['filter']}{base};"
            f"[{background_label}]volume={audio['bgm_volume']},volume=0:enable='not({expression})'{background};"
            f'{base}{background}amix=inputs=2:duration=first:normalize=0{output}')


def audio_fingerprint(ctx):
    """未实际处理声音时不引入可选背景或模式，使无命中成品继续有效。"""
    audio = audio_plan(ctx)
    if not audio['ranges']:
        return None
    result = {k: audio[k] for k in ('requested_mode', 'effective_mode', 'background_identity')}
    if audio['background'] is not None:
        result['bgm_volume'] = audio['bgm_volume']
    return result


def safe_overlay(ctx, overlay):
    if not overlay:
        return overlay
    if ((getattr(ctx, 'version', None) or {}).get('render_manifest') is not None
            and overlay.get('internal_schema') == 'dubforge.comment-events.v1'):
        # 清单已经保存当时实际采用的审查结果，不能再次用未选工作稿筛选。
        return copy.deepcopy(overlay)
    from dubforge.content_review import settings_from_cfg
    from dubforge import content_review_decisions as decisions
    enabled, terms = settings_from_cfg(ctx.cfg)
    if not enabled:
        return overlay
    result = copy.deepcopy(overlay)
    store = decisions.load(ctx.project)
    result['items'] = [item for item in result['items']
                       if not (finding := decisions.comment_finding(item, (enabled, terms), store=store))
                       or not finding['active']]
    if not result['items']:
        raise ContentRedactionError('评论方案的全部内容命中屏蔽规则，请重新选择可上屏评论', code='COMMENTS_REVIEW_EMPTY')
    return result
