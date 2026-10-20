"""成片差异的评论输入投影；只比较真正显示的文字、时段和有效样式。"""
import copy
import hashlib
import json
import math


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def comment_inputs(overlay, duration):
    return _comment_inputs(overlay, duration)


def _historical_lines(item, settings):
    """已渲染合同没有选择来源；只还原保存文字，不猜测人工来源或重选候选。"""
    from dubforge.comment_studio import _display_parts
    parts = [text for text, _, _ in _display_parts(item, settings)]
    if any(not isinstance(text, str) or not text.strip() for text in parts):
        raise ValueError('历史上屏评论缺少有效的显示文字')
    return [text.strip() for text in parts]


def _comment_inputs(overlay, duration, *, historical=False):
    from dubforge.comment_studio import display_lines, render_fingerprint_plan, validate_settings, RENDER_VERSION
    from dubforge.pipeline.comment_overlay import _plain
    if not overlay:
        return dict(known=True, events=[], render_version=RENDER_VERSION)
    normalized = render_fingerprint_plan(overlay)
    settings = validate_settings(overlay.get('settings', {}))
    style = {key: value for key, value in normalized['settings'].items()
             if key not in {'start', 'interval', 'duration', 'show_author'}}
    if settings['language_mode'] != 'bilingual':
        style.pop('source_first', None)
    signature = _digest(dict(settings=style, render_version=normalized['render_version']))
    events = []
    for index, item in enumerate(overlay.get('items', [])):
        start = settings['start'] + index * settings['interval']
        end = min(duration, start + settings['duration'])
        if end <= start:
            continue
        parts = [_plain(text) for text in (_historical_lines(item, settings) if historical
                                         else display_lines(item, settings))]
        if settings['show_author']:
            parts[0] = f"{_plain(item.get('author') or '匿名观众')[:48]}：{parts[0]}"
        events.append(dict(id=str(item['id']), start=round(start, 6), end=round(end, 6),
                           parts=parts, style_signature=signature))
    return dict(known=True, events=events, render_version=RENDER_VERSION)


def saved_comments(saved):
    """只读取实际保存的证据；旧记录没有渲染版本时不猜历史排版。"""
    inputs = saved.get('sentence_inputs') or {}
    comments = inputs.get('comment_inputs')
    if isinstance(comments, dict) and isinstance(comments.get('events'), list):
        return copy.deepcopy(comments)
    historical = inputs.get('global_inputs', {}).get('comments')
    if isinstance(historical, dict) and historical.get('render_version'):
        from dubforge.comment_studio import CommentError
        duration = inputs.get('duration')
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            return dict(known=False, events=[])
        try:
            result = _comment_inputs(historical, duration, historical=True)
        except (CommentError, KeyError, TypeError, ValueError):
            # 损坏或缺失的旧输入只能标为未知，不能虚构文字或从摘要推断人工保留。
            return dict(known=False, events=[])
        if historical['render_version'] != result['render_version']:
            result['known'] = False
        return result
    if saved.get('include_comments') is False:
        return dict(known=True, events=[])
    return dict(known=False, events=[])


def merged_comments(saved, current, ranges, *, full=False):
    from dubforge.pipeline.assembly_sentence_changes import _clip, _normalize
    previous = saved_comments(saved)
    return dict(known=bool(full or previous.get('known')),
        events=_normalize(_clip(previous['events'], ranges, inside=False)
                          + _clip(current['events'], ranges, inside=True)),
        render_version=current.get('render_version'))


def compare_comments(previous, current):
    from dubforge.pipeline.assembly_sentence_changes import _normalize
    if not previous.get('known'):
        return [], False
    old_ids = {row['id'] for row in previous['events']}
    ids = old_ids | {row['id'] for row in current['events']}
    result = []
    for ident in sorted(ids):
        before = _normalize([event for event in previous['events'] if event['id'] == ident])
        after = _normalize([event for event in current['events'] if event['id'] == ident])
        if before == after:
            continue
        def span(events):
            return dict(start=min(e['start'] for e in events), end=max(e['end'] for e in events)) if events else None
        result.append(dict(id='comment:' + ident, comment_id=ident,
            reason='上屏评论文字、显示时段或样式已变化', before=before, current=after,
            baseline_range=span(before), current_range=span(after)))
    return result, True


def global_scope(data):
    """评论有逐事件证据，不再用整份方案变化阻止可明确定位的局部更新。"""
    result = copy.deepcopy(data)
    result.pop('comments', None)
    result.get('dimensions', {}).pop('include_comments', None)
    return result


def group(kind, label, changes, *, known=True, reason=''):
    return dict(kind=kind, label=label, status='changed' if changes else 'unchanged' if known else 'unknown',
                count=len(changes), changes=changes, reason=reason)
