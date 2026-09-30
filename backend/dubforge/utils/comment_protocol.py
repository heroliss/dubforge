"""评论的编号逐行协议：仅传正文与短编号，不让模型复制平台长 ID。"""
import re

_LINE = re.compile(r'^\s*(\d+)\s*[.、．]\s*(.*?)\s*$')


def escape_text(text: str) -> str:
    """先转义反斜杠，确保原有字面量 \\n 与真正换行可以无损区分。"""
    normalized = re.sub(r'\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]', '\n', str(text))
    return normalized.replace('\\', '\\\\').replace('\n', '\\n')


def unescape_text(text: str) -> str:
    # 只还原约定的两种转义，不使用会损坏中文和路径的 unicode_escape。
    return re.sub(r'\\([\\n])', lambda m: '\n' if m[1] == 'n' else '\\', text)


def instructions(action: str) -> str:
    shared = ('输入、输出均为编号逐行文本，不使用 JSON、表格、代码块或解释。'
              '每条评论占一个物理行，保留请求中的编号，不重新编号。'
              '正文内部换行统一写为字面量 \\n，正文原有反斜杠写为 \\\\。')
    if action == 'translate':
        return shared + '输出格式：编号. 完整中文译文。示例：1. 好喜欢！\\n感谢分享。'
    return shared + ('输出格式：编号. 分数 | 是或否 | 简短中文理由。'
                     '是=符合筛选要求，否=不符合；分数为 0–100。示例：1. 85 | 是 | 真诚分享观看感受')


def request_text(rows: list[dict], numbers: dict[str, int], payload: dict) -> str:
    parts = []
    if 'criteria' in payload:
        parts.extend(['【评分要求】', escape_text(payload['criteria'])])
    if payload.get('video_context'):
        parts.append('【视频背景，仅作理解参考】')
        context = payload['video_context']
        for key, label in [('title', '标题'), ('summary', '概要'), ('description', '简介')]:
            if context.get(key):
                parts.append(f'{label}：{escape_text(context[key])}')
    parts.append('【待处理评论】')
    parts.extend(f'{numbers[row["id"]]}. {escape_text(row["text"])}' for row in rows)
    return '\n'.join(parts)


def parse_response(raw: str, ids: dict[int, str], action: str) -> dict:
    """逐行解析保留重复项；坏行不吞掉其他有效结果，交由领域层定向补齐。"""
    key = 'translations' if action == 'translate' else 'assessments'
    rows, errors = [], []
    previous = None
    for line in raw.lstrip('\ufeff').splitlines():
        if not line.strip() or line.strip().startswith('```'):
            continue
        match = _LINE.fullmatch(line)
        if not match:
            errors.append('存在无编号行；评论内部换行必须写为 \\n')
            if previous is not None:
                previous['_line_error'] = '正文出现裸换行，无法确认完整性，请用 \\n 表示换行'
            continue
        number, text = int(match[1]), match[2]
        previous = None
        if number not in ids:
            errors.append(f'编号 {number} 不属于本次请求')
            continue
        row = {'id': ids[number]}
        if action == 'translate':
            row['text_zh'] = unescape_text(text)
        else:
            fields = [field.strip() for field in text.split('|', 2)]
            if len(fields) != 3:
                # 仍保留该编号，防止“同号一好一坏”被当作唯一有效项。
                row.update(score=None, eligible=None, reason='')
            else:
                score, eligible, reason = fields
                try:
                    score = float(score)
                except ValueError:
                    pass
                row.update(score=score, eligible={'是': True, '否': False,
                           'true': True, 'false': False}.get(eligible.lower(), eligible),
                           reason=unescape_text(reason))
        rows.append(row)
        previous = row
    return {key: rows, '_protocol_errors': errors}
