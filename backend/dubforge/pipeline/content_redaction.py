"""把可核查的文字筛查结果应用到字幕和最终混音，不修改原始资料。"""
import copy
import hashlib
import json
import math
import re

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
    if 'content_review.blocked_terms' in values:
        normalize_terms(values['content_review.blocked_terms'])
    if 'content_review.blocked_ranges' in values:
        parse_ranges(values['content_review.blocked_ranges'])


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
    from dubforge.content_review import REVIEW_VERSION, review_lines, settings_from_cfg
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
    findings = review_lines(rows, terms)
    ranges = manual + [dict(start=f['start'], end=f['end']) for f in findings]
    for f in findings:
        if f.get('source_audio_start') is not None:
            ranges.append(dict(start=f['source_audio_start'], end=f['source_audio_end']))
    # 原始识别稿也参与筛查，避免整理/校对时遗漏原话后误认为原声已经改变。
    try:
        from dubforge.utils.srt import parse_srt
        raw_path = ctx.upstream('transcribe', 'src.srt')
        original = [dict(id=l.index, start=l.start, end=l.end, source_text=l.text, text='')
                    for l in parse_srt(raw_path)]
    except (FileNotFoundError, OSError, DubForgeError, KeyError, AttributeError):
        original = []
    raw_findings = review_lines(original, terms)
    ranges += [dict(start=f['start'], end=f['end']) for f in raw_findings]
    blocked = {l['id'] for l in rows if overlaps(l['start'], l['end'], ranges)}
    result = dict(enabled=True, findings=findings, original_findings=raw_findings,
                  ranges=merge_ranges(ranges), blocked_ids=sorted(blocked), checked_lines=len(rows))
    result['original_checked_lines'] = len(original)
    result['warnings'] = [] if original else ['未找到可核对的原始识别稿；当前仅检查整理文字，请另行听验原声。']
    signature = dict(version=REVIEW_VERSION, lines=rows, original=original, terms=terms,
                     manual=manual, mute_audio=ctx.cfg('content_review.mute_audio', True))
    result['signature'] = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # VersionContext的父上下文可能同轮渲染多个版本，缓存只放在当前实例。
    ctx._content_redaction_plan = result
    return result


def receipt(ctx):
    from dubforge.content_review import REVIEW_VERSION
    current = plan(ctx)
    return dict(version=REVIEW_VERSION, signature=current.get('signature'),
                ranges=current['ranges'], mute_audio=ctx.cfg('content_review.mute_audio', True))


def redact_document(ctx, rows):
    current = plan(ctx)
    if not current['enabled']:
        return rows
    return [l for l in rows if l['id'] not in current['blocked_ids']
            and not overlaps(l['start'], l['end'], current['ranges'])]


def filter_cues(ctx, cues):
    current = plan(ctx)
    return [l for l in cues if not overlaps(l.start, l.end, current['ranges'])]


def audio_filter(ctx, *, start=0.0, duration=None):
    if not ctx.cfg('content_review.mute_audio', True):
        return ''
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
        except (FileNotFoundError, OSError, DubForgeError):
            # 原声版不要求具备可用配音；字幕与原声时段仍会处理。
            pass
    visible = merge_ranges(ranges, start=start, duration=duration, padding=0.15)
    if not visible:
        return ''
    expression = '+'.join(f'between(t,{r["start"]:.6f},{r["end"]:.6f})' for r in visible)
    return f"volume=0:enable='{expression}'"


def safe_overlay(ctx, overlay):
    if not overlay:
        return overlay
    from dubforge.content_review import blocked_comment, settings_from_cfg
    enabled, terms = settings_from_cfg(ctx.cfg)
    if not enabled:
        return overlay
    result = copy.deepcopy(overlay)
    result['items'] = [item for item in result['items'] if not any(blocked_comment(
        item.get('text', ''), item.get(field, ''), terms) for field in ('source_text', 'display_text', 'text_zh'))]
    if not result['items']:
        raise ContentRedactionError('评论方案的全部内容命中屏蔽规则，请重新选择可上屏评论', code='COMMENTS_REVIEW_EMPTY')
    return result
