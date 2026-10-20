"""局部成片按实际新旧内容范围核对，不能借局部凭证声明全片已重审。"""
import math


def _intersect(left, right):
    return [dict(start=max(a['start'], b['start']), end=min(a['end'], b['end']))
            for a in left for b in right if max(a['start'], b['start']) < min(a['end'], b['end'])]


def _outside(rows, removed):
    result = list(rows)
    for cut in removed:
        kept = []
        for row in result:
            if row['end'] <= cut['start'] or row['start'] >= cut['end']:
                kept.append(row)
            else:
                if row['start'] < cut['start']:
                    kept.append(dict(start=row['start'], end=cut['start']))
                if row['end'] > cut['end']:
                    kept.append(dict(start=cut['end'], end=row['end']))
        result = kept
    return result


def _coverage(receipt, current, *, depth=0):
    if not isinstance(receipt, dict) or depth > 16:
        return [], []
    if receipt.get('scope') == 'partial':
        ranges = receipt.get('ranges', [])
        old_visual, old_audio = _coverage(receipt.get('inherited_baseline'), current, depth=depth + 1)
        local = receipt.get('current_local') or {}
        # 当前凭证的签名只证明本次采用范围，不能替范围外继承内容背书。
        local_ok = (local.get('version') == current.get('version')
                    and (depth > 0 or local.get('signature') == current.get('signature')))
        visual = _outside(old_visual, ranges)
        if local_ok:
            visual += _intersect(local.get('ranges', []), ranges)
        if not receipt.get('replace_audio'):
            return visual, old_audio
        audio = _outside(old_audio, ranges)
        if local_ok and local.get('mute_audio') and local.get('audio_processing') == current.get('audio_processing'):
            audio += _intersect(local.get('ranges', []), ranges)
        return visual, audio
    if receipt.get('version') != current.get('version'):
        return [], []
    ranges = receipt.get('ranges', [])
    audio = ranges if receipt.get('mute_audio') and receipt.get('audio_processing') == current.get('audio_processing') else []
    return ranges, audio


def covers_current_ranges(receipt, current):
    visual, audio = _coverage(receipt, current)
    required = current.get('ranges', [])
    return not _outside(required, visual) and (not current.get('mute_audio') or not _outside(required, audio))


def comment_issue(artifact, snapshot, terms, store, check, *, allowed=None, depth=0):
    """分别检查旧片保留区的旧评论及替换区采用的新评论，支持连续局部修订。"""
    if depth > 16 or not isinstance(snapshot, dict):
        return '局部成片缺少可核对的基片生成记录，请重新选择有效基片。'
    allowed = allowed if allowed is not None else [dict(start=0., end=math.inf)]
    partial = snapshot.get('partial_plan') if snapshot.get('partial') else None
    if snapshot.get('partial') and (not isinstance(partial, dict)
            or not isinstance(partial.get('ranges'), list) or not isinstance(partial.get('base_version'), dict)):
        return '局部成片缺少可核对的基片生成记录，请重新选择有效基片。'
    if partial:
        ranges = partial['ranges']
        old = _outside(allowed, ranges)
        if old:
            issue = comment_issue(artifact, partial.get('base_version'), terms, store, check,
                                  allowed=old, depth=depth+1)
            if issue:
                return issue
        allowed = _intersect(allowed, ranges)
    if not allowed:
        return ''
    overlay = snapshot.get('comment_overlay')
    scoped = snapshot
    if isinstance(overlay, dict) and isinstance(overlay.get('items'), list):
        settings = overlay.get('settings') or {}
        selected = []
        for index, item in enumerate(overlay['items']):
            start = settings.get('start', 0.) + index * settings.get('interval', 0.)
            end = start + settings.get('duration', 0.)
            if _intersect([dict(start=start, end=end)], allowed):
                selected.append(item)
        scoped = {**snapshot, 'comment_overlay': {**overlay, 'items': selected}}
    return check(artifact, scoped, terms, store=store)
