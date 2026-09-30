"""已确认评论的 ASS 图层；单帧预览和正式成片共享同一渲染路径。"""
import math
import re
import tempfile
import unicodedata
import uuid
from pathlib import Path

from dubforge.comment_studio import CommentError, display_lines, validate_settings
from dubforge.pipeline.subtitle_style import _run, video_canvas
from dubforge.pipeline.text_style import paint_style, language_style, ass_inline, validate_font
from dubforge.utils.fs import atomic_write_bytes


def _plain(text):
    # 平台文本不是 ASS 指令；全角替换保留可读含义，禁止覆盖样式、绘图或路径注入。
    text = str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝')
    text = ''.join(c for c in text if not ('\u202a' <= c <= '\u202e' or '\u2066' <= c <= '\u2069'))
    return re.sub(r'\s+', ' ', text).strip()


def _clusters(text):
    """换行不拆重音组合、肤色修饰、常见 ZWJ 表情与双字符国旗。"""
    result = []
    for char in text:
        attached = (unicodedata.category(char) in {'Mn', 'Mc', 'Me'} or char == '\u200d'
                    or '\U0001f3fb' <= char <= '\U0001f3ff')
        flag_pair = (result and len(result[-1]) == 1
                     and '\U0001f1e6' <= result[-1] <= '\U0001f1ff'
                     and '\U0001f1e6' <= char <= '\U0001f1ff')
        if result and (attached or result[-1].endswith('\u200d') or flag_pair):
            result[-1] += char
        else:
            result.append(char)
    return result


def _units(text):
    def measure(cluster):
        widths = [0 if unicodedata.category(c) in {'Mn', 'Mc', 'Me', 'Cf'} else
                  1 if unicodedata.east_asian_width(c) in 'WF' else .58 for c in cluster]
        return max(widths) if len(cluster) > 1 and ('\u200d' in cluster or ord(cluster[0]) >= 0x1f1e6) else sum(widths)
    return sum(measure(cluster) for cluster in _clusters(text))


def _tokens(text):
    tokens, word = [], ''
    for cluster in _clusters(text):
        if cluster.isspace() or unicodedata.east_asian_width(cluster[0]) in 'WF' or ord(cluster[0]) >= 0x1f1e6:
            if word:
                tokens.append(word); word = ''
            tokens.append(cluster)
        else:
            word += cluster
    if word:
        tokens.append(word)
    # 中日韩闭标点随前字，开标点随后字；英文标点已包含在单词中。
    merged = []
    for token in tokens:
        if merged and (token in '，。！？；：、）》」』】…' or merged[-1] in '（《「『【'):
            merged[-1] += token
        else:
            merged.append(token)
    return merged


def _wrap(text, units):
    """优先在单词之间换行；只有超过整行的长词/链接才按字符簇折行。"""
    lines, line, width, space = [], '', 0.0, ''
    for token in _tokens(text):
        if token.isspace():
            space = ' ' if line else ''
            continue
        size = _units(token)
        if line and width + _units(space) + size > units + 1e-9:
            lines.append(line)
            line, width, space = '', 0.0, ''
        if size <= units + 1e-9:
            line += space + token
            width += _units(space) + size
        else:
            for cluster in _clusters(token):
                size = _units(cluster)
                if line and width + size > units + 1e-9:
                    lines.append(line)
                    line, width = '', 0.0
                line += cluster
                width += size
        space = ''
    if line:
        lines.append(line)
    return lines


def _horizontal_margins(settings, canvas, alignment):
    width, height = canvas
    side = min(round(settings['margin']), round(width * .2))
    percent = settings['width_percent']
    if not percent and alignment in {2, 8}:
        return round(width * .08), round(width * .08)
    fraction = percent / 100 if percent else (.9 if width < height else .68)
    text_width = max(1, min(width - side * 2, round(width * fraction)))
    if alignment in {1, 7}:
        return side, width - side - text_width
    if alignment in {3, 9}:
        return width - side - text_width, side
    left = (width - text_width) // 2
    return left, width - text_width - left


def _clock(seconds):
    value = max(0, round(seconds * 100))
    return f'{value // 360000}:{value // 6000 % 60:02}:{value // 100 % 60:02}.{value % 100:02}'


def ass_document(plan, canvas, start=0.0, end=None):
    settings = validate_settings(plan.get('settings', {}))
    items = plan.get('items')
    if not isinstance(items, list) or not items or len(items) > 100:
        raise CommentError('没有有效的已确认评论', code='COMMENTS_NOT_CONFIRMED')
    width, height = canvas
    alignment = {'top_left': 7, 'top': 8, 'top_right': 9, 'bottom_left': 1, 'bottom': 2, 'bottom_right': 3}[settings['position']]
    left, right = _horizontal_margins(settings, canvas, alignment)
    available_width = width - left - right
    paint = paint_style(color=settings['color'], outline_color=settings['outline_color'], outline=settings['outline'],
                       background_color=settings['background_color'], background_opacity=settings['background_opacity'])
    header = (
        '[Script Info]\nScriptType: v4.00+\n'
        f'PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n'
        '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: Comment,{validate_font(settings["font"])},{settings["font_size"]},{paint["PrimaryColour"]},&H00FFFFFF,{paint["OutlineColour"]},{paint["BackColour"]},0,0,0,0,100,100,0,0,{paint["BorderStyle"]},{paint["Outline"]},{paint["Shadow"]},{alignment},{left},{right},{settings["margin"]},1\n'
        '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    events = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CommentError('已确认评论文字无效，请重新确认', code='COMMENTS_DISPLAY_TEXT')
        parts = [_plain(text) for text in display_lines(item, settings)]
        begin = settings['start'] + index * settings['interval']
        finish = begin + settings['duration']
        if finish <= start or (end is not None and begin >= end):
            continue
        if settings['show_author']:
            author = _plain(item.get('author') or '匿名观众')
            parts[0] = f'{author[:48]}：{parts[0]}'
        if settings['source_style_enabled'] and settings['language_mode'] != 'translated':
            flags = [True] if settings['language_mode'] == 'original' else ([True, False] if settings['source_first'] else [False, True])
            styles = [language_style(settings, source) for source in flags]
            scale, minimum = 1.0, 18 / min(style['font_size'] for style in styles)
            def separate_wrap():
                rendered, total_height, widest = [], 0, 0
                for part, style in zip(parts, styles, strict=True):
                    scaled = {**style, 'font_size': style['font_size'] * scale}
                    lines = _wrap(part, available_width / scaled['font_size'])
                    rendered.append(ass_inline(scaled) + '\\N'.join(lines))
                    total_height += len(lines) * scaled['font_size'] * 1.3
                    widest = max(widest, max((_units(line) * scaled['font_size'] for line in lines), default=0))
                return rendered, total_height, widest
            rendered, total_height, widest = separate_wrap()
            while (total_height > height * .26 or widest > available_width) and scale > minimum:
                scale = max(minimum, scale - .025)
                rendered, total_height, widest = separate_wrap()
            if total_height > height * .30 or widest > available_width + 1e-6:
                raise CommentError(f'第 {index + 1} 条评论在当前区域内放不下，请增大宽度或缩短上屏文字', code='COMMENTS_LAYOUT_OVERFLOW')
            stop = min(finish, end) if end is not None else finish
            body = '\\N'.join(rendered)
            events.append(f'Dialogue: 10,{_clock(max(begin, start)-start)},{_clock(stop-start)},Comment,,0,0,0,,{body}\n')
            continue
        size = float(settings['font_size'])
        def wrap():
            return [line for part in parts for line in _wrap(part, available_width / size)]
        def too_wide():
            return any(_units(line) * size > available_width + 1e-6 for line in lines)
        lines = wrap()
        while (len(lines) * size * 1.3 > height * .26 or too_wide()) and size > 18:
            size = max(18, size - 1)
            lines = wrap()
        if len(lines) * size * 1.3 > height * .30 or too_wide():
            raise CommentError(f'第 {index + 1} 条评论在当前区域内放不下，请增大宽度或缩短上屏文字', code='COMMENTS_LAYOUT_OVERFLOW')
        stop = min(finish, end) if end is not None else finish
        body = '\\N'.join(lines)
        events.append(f'Dialogue: 10,{_clock(max(begin, start) - start)},{_clock(stop - start)},Comment,,0,0,0,,{{\\fs{size:g}}}{body}\n')
    return header + ''.join(events)


def prepare_filter(plan, video, cwd, *, start=0.0, duration=None):
    """仅内部文件名进入滤镜；调用者负责 finally 删除临时 ASS。"""
    if not plan:
        return '', None
    path = Path(cwd) / f'.comments_{uuid.uuid4().hex}.ass'
    data = ass_document(plan, video_canvas(video), start, start + duration if duration is not None else None)
    atomic_write_bytes(path, data.encode('utf-8-sig'))
    return f'ass={path.name}', path


def preview_frame(video, plan, seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise CommentError('评论预览位置必须是有效的非负秒数')
    with tempfile.TemporaryDirectory(prefix='dubforge-comments-') as folder:
        graph, _ = prepare_filter(plan, video, folder)
        data = _run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', f'{seconds:.6f}',
                     '-i', str(video.resolve()), '-vf', f'setpts=PTS+{seconds:.6f}/TB,{graph}',
                     '-frames:v', '1', '-an', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'], cwd=folder)
        if not data:
            raise CommentError('当前位置没有画面，请选择视频结束前的时间', code='COMMENTS_PREVIEW_EMPTY')
        return data
