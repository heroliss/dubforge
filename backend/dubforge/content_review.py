"""用于字幕整句隐藏和评论排除的本地文本规则，不修改原稿或调用模型。"""
from collections.abc import Mapping
import copy
import math
import re
import unicodedata

from dubforge.errors import DubForgeError

# 规则变化必须使已生成的审查报告和成片失效；发布前会重新核对当前规则。
REVIEW_VERSION = 'local-context-v2'
TEXT_ONLY_LIMITATION = '本次仅检查字幕和评论文字，无法判断画面或所有语义风险，也不能保证平台审核通过。'
MAX_ADJACENT_GAP_SECONDS = 0.6
MAX_CONTEXT_CHARACTERS = 80
MAX_CUSTOM_TERMS = 500
MAX_CUSTOM_TERM_CHARACTERS = 200
_CJK = r'\u3400-\u9fff'
_ENGLISH_CONNECTORS = {
    'square', 'the', 'a', 'an', 'of', 'at', 'in', 'on', 'during', '1989',
    'was', 'is', 'were', 'site', 'scene', 'incident', 'event', 'events',
    'that', 'happened', 'occurred',
}
_CHINESE_CONNECTORS = re.compile(
    r'(?:广场|的|那场|这场|发生在|发生|事件|历史|一九八九年|1989年|1989|当年|期间|曾|所|在|于){0,6}')
_PLACE_EN = re.compile(r'(?<![a-z0-9])tian[\s\'’-]*an[\s\'’-]*men(?![a-z0-9])')
_EVENT_EN = re.compile(r'(?<![a-z0-9])(?:tragedy|massacre)(?![a-z0-9])')
_PLACE_ZH = re.compile(r'天\s*安\s*门')
_EVENT_ZH = re.compile(r'悲\s*剧|屠\s*杀')
_CLAUSE_END = re.compile(r'[.!?;。！？；]')

# 真实退回区间与评论叠加重合：同条评论同时描述高敏感地点和暴恐事件。
# 这种组合先保留给人工复核；单独地名、安检或新闻学习中的暴恐词不能推断
# 为不适合上屏。规则仅作用评论，不扩大普通字幕的屏蔽范围。
_COMMENT_SENSITIVE_LOCATION = re.compile(
    r'(?:全国|中国|本国|该国)?\s*最敏感\s*(?:的\s*)?(?:地方|地区|区域|场所)'
    r'|(?<![a-z0-9])(?:the\s+)?most\s+sensitive\s+(?:place|area|location)(?![a-z0-9])')
_COMMENT_TERROR_EVENT = re.compile(
    r'恐怖\s*(?:袭击|攻击|主义|分子)|暴\s*恐'
    r'|(?<![a-z0-9])(?:terrorism|terrorist\s+attack)(?![a-z0-9])')
MAX_COMMENT_RISK_DISTANCE = 600


class ContentReviewError(DubForgeError):
    code = 'CONTENT_REVIEW_INVALID'
    suggestion = '请检查字幕时间轴和内容屏蔽设置后重试，原稿不会被修改。'


def normalize_text(text: str) -> str:
    """统一兼容字符、大小写及空白；中文内部空白不改变词语身份。"""
    if text is None:
        return ''
    if not isinstance(text, str):
        raise ContentReviewError('待检查的文字必须是字符串', code='CONTENT_REVIEW_INPUT')
    value = ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
    return re.sub(rf'(?<=[{_CJK}])\s+(?=[{_CJK}])', '', value)


def normalize_terms(custom_terms=()) -> list[str]:
    """配置可用换行文本或词语列表，保留可读标签并去掉重复/空词。"""
    if custom_terms is None:
        return []
    if isinstance(custom_terms, str):
        values = custom_terms.splitlines()
    elif isinstance(custom_terms, (list, tuple)):
        values = custom_terms
    else:
        raise ContentReviewError('自定义屏蔽词必须是换行文字或字符串列表',
                                 code='CONTENT_REVIEW_SETTINGS')
    result, seen = [], set()
    for item in values:
        if not isinstance(item, str):
            raise ContentReviewError('每个自定义屏蔽词都必须是字符串',
                                     code='CONTENT_REVIEW_SETTINGS')
        label = item.strip()
        if len(label) > MAX_CUSTOM_TERM_CHARACTERS:
            raise ContentReviewError(f'单个自定义屏蔽词不能超过 {MAX_CUSTOM_TERM_CHARACTERS} 个字符',
                                     code='CONTENT_REVIEW_SETTINGS')
        normalized = normalize_text(label)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(label)
        if len(result) > MAX_CUSTOM_TERMS:
            raise ContentReviewError(f'自定义屏蔽词不能超过 {MAX_CUSTOM_TERMS} 条',
                                     code='CONTENT_REVIEW_SETTINGS')
    return result


def settings_from_cfg(cfg=None) -> tuple[bool, list[str]]:
    """优先接受运行快照的 ctx.cfg，也兼容配置字典；不改动设置。"""
    if cfg is None:
        from dubforge import config
        getter = config.get
    elif callable(cfg):
        getter = cfg
    elif isinstance(cfg, Mapping):
        def getter(key, default=None):
            if key in cfg:
                return cfg[key]
            section, _, name = key.partition('.')
            value = cfg.get(section, {})
            return value.get(name, default) if isinstance(value, Mapping) else default
    else:
        raise ContentReviewError('内容检查配置读取方式无效', code='CONTENT_REVIEW_SETTINGS')
    enabled = getter('content_review.enabled', True)
    if type(enabled) is not bool:
        raise ContentReviewError('内容检查开关必须为 true 或 false', code='CONTENT_REVIEW_SETTINGS')
    return enabled, normalize_terms(getter('content_review.blocked_terms', ''))


def _custom_patterns(terms):
    result = []
    for label in terms:
        term = normalize_text(label)
        pieces = []
        for i, char in enumerate(term):
            if i and re.fullmatch(rf'[{_CJK}]', char) and re.fullmatch(rf'[{_CJK}]', term[i - 1]):
                pieces.append(r'\s*')
            pieces.append(r'\s+' if char.isspace() else re.escape(char))
        # 英文自定义词按词边界检查，避免 ass 意外匹配 passage 等词。
        before = r'(?<![a-z0-9_])' if re.fullmatch(r'[a-z0-9_]', term[0]) else ''
        after = r'(?![a-z0-9_])' if re.fullmatch(r'[a-z0-9_]', term[-1]) else ''
        result.append((label, re.compile(before + ''.join(pieces) + after)))
    return result


def _context_matches(text, place_pattern, event_pattern, language):
    result = []
    for place in place_pattern.finditer(text):
        lo = max(0, place.start() - MAX_CONTEXT_CHARACTERS)
        hi = min(len(text), place.end() + MAX_CONTEXT_CHARACTERS)
        for local_event in event_pattern.finditer(text[lo:hi]):
            event_start, event_end = lo + local_event.start(), lo + local_event.end()
            if event_end <= place.start():
                between = text[event_end:place.start()]
            elif event_start >= place.end():
                between = text[place.end():event_start]
            else:
                continue
            if len(between) > MAX_CONTEXT_CHARACTERS or _CLAUSE_END.search(between):
                continue
            connector = re.sub(r'[\s,，:：\'"‘’“”（）()\-–—]', '', between)
            if language == 'zh':
                related = bool(_CHINESE_CONNECTORS.fullmatch(connector))
            else:
                words = re.findall(r'[a-z0-9]+', between)
                remainder = re.sub(r'[a-z0-9\s,，:：\'"‘’“”（）()\-–—]', '', between)
                related = not remainder and len(words) <= 6 and all(
                    word in _ENGLISH_CONNECTORS for word in words)
            if related:
                result.append({
                    'start': min(place.start(), event_start),
                    'end': max(place.end(), event_end),
                    'rule': 'tiananmen-event-context',
                    'reason': '命中天安门与悲剧/屠杀的紧邻语境',
                })
    return result


def _matches(text, patterns):
    matches = (_context_matches(text, _PLACE_ZH, _EVENT_ZH, 'zh')
               + _context_matches(text, _PLACE_EN, _EVENT_EN, 'en'))
    for label, pattern in patterns:
        matches.extend({'start': match.start(), 'end': match.end(),
                        'rule': 'custom-term', 'reason': f'命中自定义屏蔽词：{label}'}
                       for match in pattern.finditer(text))
    return matches


def _timing(start, end, line_id):
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end <= start):
        raise ContentReviewError(f'第 {line_id} 句时间无效，无法确定屏蔽区间',
                                 code='CONTENT_REVIEW_TIMELINE')
    return float(start), float(end)


def _audio_timing(row):
    voice_timing = row.get('voice_timing')
    base = row.get('base') or {}
    if voice_timing is not None:
        if not isinstance(voice_timing, (list, tuple)) or len(voice_timing) != 2:
            raise ContentReviewError(f"第 {row['id']} 句原声时间无效", code='CONTENT_REVIEW_TIMELINE')
        return _timing(*voice_timing, row['id'])
    if isinstance(base, Mapping) and ('start' in base or 'end' in base):
        return _timing(base.get('start'), base.get('end'), row['id'])
    return _timing(row.get('start'), row.get('end'), row['id'])


def _tracks(row):
    base = row.get('base') or {}
    result = {}
    if 'source_text' in row:
        result['source_text'] = normalize_text(row['source_text'])
        result['text'] = normalize_text(row.get('text', ''))
    else:
        # 仅原文产物的 text 同样可直接检查。
        result['text'] = normalize_text(row.get('text', ''))
    if isinstance(base, Mapping) and 'source_text' in base:
        result['source_audio'] = normalize_text(base['source_text'])
    return result


def _validated_rows(lines):
    if not isinstance(lines, (list, tuple)):
        raise ContentReviewError('字幕检查输入必须是句子列表', code='CONTENT_REVIEW_INPUT')
    seen, previous_start = set(), None
    for row in lines:
        if not isinstance(row, Mapping) or type(row.get('id')) is not int or row['id'] <= 0:
            raise ContentReviewError('字幕必须有有效的正整数编号', code='CONTENT_REVIEW_INPUT')
        if row['id'] in seen:
            raise ContentReviewError('字幕编号重复，无法确定屏蔽的句子', code='CONTENT_REVIEW_INPUT')
        seen.add(row['id'])
        if row.get('base') is not None and not isinstance(row['base'], Mapping):
            raise ContentReviewError('字幕原始底稿格式无效', code='CONTENT_REVIEW_INPUT')
        start, _ = _timing(row.get('start'), row.get('end'), row['id'])
        if previous_start is not None and start < previous_start:
            raise ContentReviewError('字幕时间顺序无效，请先修正时间轴', code='CONTENT_REVIEW_TIMELINE')
        previous_start = start
        _audio_timing(row)
        _tracks(row)
    return lines


def review_lines(lines, custom_terms=(), *, enabled=True) -> list[dict]:
    """检查当前源/译文及原声底稿；每个命中句返回一个 finding，不修改输入。"""
    rows = _validated_rows(lines)
    patterns = _custom_patterns(normalize_terms(custom_terms))
    if not enabled:
        return []
    tracks = [_tracks(row) for row in rows]
    found: dict[int, dict] = {}

    def add(index, match, field, context_ids):
        row = rows[index]
        if row['id'] not in found:
            audio_start, audio_end = _audio_timing(row)
            found[row['id']] = {
                'id': row['id'], 'ids': [row['id']],
                'start': float(row['start']), 'end': float(row['end']),
                'source_text': row.get('source_text', row.get('text', '')),
                'text': row.get('text', '') if 'source_text' in row else '',
                'source_audio_text': (row.get('base') or {}).get('source_text',
                    row.get('source_text', row.get('text', ''))),
                'source_audio_start': audio_start, 'source_audio_end': audio_end,
                'reason': '', 'rule': match['rule'], 'matched_rules': [],
                'fields': [], 'context_ids': [], 'reasons': [],
            }
        finding = found[row['id']]
        for key, values in [('matched_rules', [match['rule']]), ('fields', [field]),
                            ('context_ids', context_ids), ('reasons', [match['reason']])]:
            for value in values:
                if value not in finding[key]:
                    finding[key].append(value)
        finding['reason'] = '；'.join(finding['reasons'])

    for index, row_tracks in enumerate(tracks):
        for field, text in row_tracks.items():
            for match in _matches(text, patterns):
                add(index, match, field, [rows[index]['id']])
    for index in range(1, len(rows)):
        left, right = rows[index - 1], rows[index]
        # 不能隔过原稿中未传入的一句，把两个不同场景的关键词连接起来。
        if right['id'] != left['id'] + 1:
            continue
        for field in sorted(tracks[index - 1].keys() & tracks[index].keys()):
            if field == 'source_audio':
                left_start, left_end = _audio_timing(left)
                right_start, right_end = _audio_timing(right)
            else:
                left_start, left_end = left['start'], left['end']
                right_start, right_end = right['start'], right['end']
            if (right_start < left_start or right_start - left_end > MAX_ADJACENT_GAP_SECONDS + 1e-6
                    or right_end <= left_start):
                continue
            before, after = tracks[index - 1][field], tracks[index][field]
            if not before or not after:
                continue
            combined = before + ' ' + after
            for match in _matches(combined, patterns):
                if match['start'] < len(before) and match['end'] > len(before) + 1:
                    ids = [left['id'], right['id']]
                    add(index - 1, match, field, ids)
                    add(index, match, field, ids)
    return [found[row['id']] for row in rows if row['id'] in found]


def blocked_comment(original, translation='', custom_terms=(), *, enabled=True) -> list[str]:
    """评论原文/译文分别检查，命中时整条不选用，原文仍可供工作区阅读。"""
    patterns = _custom_patterns(normalize_terms(custom_terms))
    reasons = []
    for value in (original, translation):
        text = normalize_text(value)
        if enabled:
            for match in _matches(text, patterns):
                if match['reason'] not in reasons:
                    reasons.append(match['reason'])
            locations = list(_COMMENT_SENSITIVE_LOCATION.finditer(text))
            events = list(_COMMENT_TERROR_EVENT.finditer(text))
            if any(min(abs(location.end() - event.start()), abs(event.end() - location.start()))
                   <= MAX_COMMENT_RISK_DISTANCE for location in locations for event in events):
                reason = '评论同时含高敏感地点与暴恐事件表述，需人工复核后再上屏'
                if reason not in reasons:
                    reasons.append(reason)
    return reasons


def redact_lines(lines, findings=None, custom_terms=(), *, enabled=True) -> list[dict]:
    """只返回展示/导出用副本，整句省略命中行；原稿及其时间轴不写回。"""
    _validated_rows(lines)
    findings = review_lines(lines, custom_terms, enabled=enabled) if findings is None else findings
    blocked_ids = {finding['id'] for finding in findings} if enabled else set()
    return [copy.deepcopy(row) for row in lines if row['id'] not in blocked_ids]


def review_report(lines, custom_terms=(), *, enabled=True) -> dict:
    """构建供项目 API 合并的文本审查报告；未命中不等于平台审查通过。"""
    terms = normalize_terms(custom_terms)
    findings = review_lines(lines, terms, enabled=enabled)
    blocked_ids = [finding['id'] for finding in findings]
    warnings = [TEXT_ONLY_LIMITATION]
    if not enabled:
        warnings.append('文本屏蔽已关闭，未执行规则检查。')
    elif not lines or not any(any(_tracks(row).values()) for row in lines):
        warnings.append('没有可检查的字幕文字，不能据此认为内容已通过检查。')
    return {
        'version': REVIEW_VERSION, 'enabled': enabled, 'scope': 'text_only',
        'checked_lines': len(lines), 'blocked_ids': blocked_ids, 'findings': findings,
        'summary': {'lines': len(lines), 'blocked_lines': len(blocked_ids), 'blocked_comments': 0},
        'limitation': TEXT_ONLY_LIMITATION, 'warnings': warnings,
        'mute_audio': True, 'blocked_terms': terms,
    }
