"""已确认评论的 ASS 图层；单帧预览和正式成片共享同一渲染路径。"""
import math
import re
import tempfile
import uuid
from pathlib import Path

from dubforge.comment_studio import CommentError, display_lines, validate_settings
from dubforge.pipeline.subtitle_style import _run, video_canvas
from dubforge.pipeline.text_style import paint_style, language_style, ass_inline, validate_font
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.ffmpeg import ass_file_filter


def _plain(text):
    # 平台文本不是 ASS 指令；全角替换保留可读含义，禁止覆盖样式、绘图或路径注入。
    text = str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝')
    text = ''.join(c for c in text if not ('\u202a' <= c <= '\u202e' or '\u2066' <= c <= '\u2069'))
    return re.sub(r'\s+', ' ', text).strip()


def _horizontal_margins(settings, canvas, alignment):
    width, height = canvas
    side = min(round(settings['margin']), round(width * .2))
    percent = settings['width_percent']
    # 自动与 100% 共用扣除边距后的可用宽度；实际文字不拉伸。
    fraction = percent / 100 if percent else 1
    text_width = max(1, min(width - side * 2, round(width * fraction)))
    if alignment in {1, 7}:
        return side, width - side - text_width
    if alignment in {3, 9}:
        return width - side - text_width, side
    left = (width - text_width) // 2
    return left, width - text_width - left


def _full_text_layout(parts, styles, available_width, max_height):
    """完整换行后在各自字号范围内缩小；达到下限后只扩展高度，不省略正文。"""
    from dubforge.pipeline.comment_text_layout import PreparedText
    prepared = [PreparedText(part, style.get('font'))
                for part, style in zip(parts, styles, strict=True)]

    def layout(scale):
        fitted, wrapped, widths = [], [], []
        for text, original in zip(prepared, styles, strict=True):
            maximum = original['font_size']
            minimum = min(maximum, original.get('min_font_size', 18))
            # 各语言分别守住下限，共享缩小程度；描边随本语言字号同比缩小。
            size = max(minimum, math.floor(maximum * scale * 10000) / 10000)
            style = {**original, 'font_size': size, 'outline': original['outline'] * size / maximum}
            lines, widest = text.wrap(size, max(1, available_width - 2 * style['outline'] - 2))
            fitted.append(style)
            wrapped.append(lines)
            widths.append(widest + 2 * style['outline'])
        total = sum(len(lines) * style['font_size'] * 1.45
                    for lines, style in zip(wrapped, fitted, strict=True))
        widest = max(widths, default=0)
        return fitted, wrapped, total, widest

    fitted, wrapped, total, widest = layout(1.0)
    if total <= max_height + 1e-6 and widest <= available_width + 1e-6:
        return fitted, wrapped, total, widest
    minimum_layout = layout(0.0)
    if minimum_layout[3] > available_width + 1e-6:
        raise CommentError('评论在最小字号下仍超出宽度，请增大评论区域宽度或调低最小字号',
                           code='COMMENTS_LAYOUT_OVERFLOW')
    if minimum_layout[2] > max_height + 1e-6:
        return minimum_layout
    low, high = 0.0, 1.0
    fitted, wrapped, total, widest = minimum_layout
    for _ in range(24):
        middle = (low + high) / 2
        candidate = layout(middle)
        if candidate[2] <= max_height + 1e-6 and candidate[3] <= available_width + 1e-6:
            low = middle
            fitted, wrapped, total, widest = candidate
        else:
            high = middle
    return fitted, wrapped, total, widest


def _item_layout(item, settings, canvas, alignment):
    parts = [_plain(text) for text in display_lines(item, settings)]
    if settings['show_author']:
        author = _plain(item.get('author') or '匿名观众')
        parts[0] = f'{author[:48]}：{parts[0]}'
    flags = ([True] if settings['language_mode'] == 'original' else
             [False] if settings['language_mode'] == 'translated' else
             [True, False] if settings['source_first'] else [False, True])
    styles = []
    for source in flags:
        independent = source and settings['source_style_enabled']
        style = language_style(settings, independent)
        prefix = 'source_' if independent else ''
        style['min_font_size'] = settings[prefix + 'min_font_size']
        styles.append(style)
    left, right = _horizontal_margins(settings, canvas, alignment)
    fitted, wrapped, total, _ = _full_text_layout(
        parts, styles, canvas[0] - left - right, canvas[1] * settings['height_percent'] / 100)
    return fitted, wrapped, total


def _validate_seconds(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise CommentError('评论预览位置必须是有效的非负秒数')


def _active_index(items, settings, seconds):
    # 直接比较分界点，避免 0.3 / 0.1 的浮点商将播放头算进上一条。
    return next((index for index in range(len(items) - 1, -1, -1)
        if settings['start'] + index * settings['interval'] <= seconds
        < settings['start'] + index * settings['interval'] + settings['duration']), None)


def _details(layout, settings, canvas):
    fitted, _, total = layout
    margin = min(round(settings['margin']), round(canvas[1] * .2))
    return dict(font_sizes=[style['font_size'] for style in fitted],
        height_exceeded=total > canvas[1] * settings['height_percent'] / 100 + 1e-6,
        video_height_exceeded=total > canvas[1] - margin + 1e-6,
        min_reached=any(style['font_size'] <= style['min_font_size'] + 1e-6 for style in fitted))


def layout_details(plan, canvas, seconds):
    """仅汇报当前时间对应评论；完整预览与正式成片共用相同排版结果。"""
    _validate_seconds(seconds)
    settings = validate_settings(plan.get('settings', {}))
    items = plan.get('items', [])
    alignment = {'top_left': 7, 'top': 8, 'top_right': 9,
                 'bottom_left': 1, 'bottom': 2, 'bottom_right': 3}[settings['position']]
    index = _active_index(items, settings, seconds)
    if index is None:
        return None
    return _details(_item_layout(items[index], settings, canvas, alignment), settings, canvas)


def _clock(seconds):
    value = max(0, round(seconds * 100))
    return f'{value // 360000}:{value // 6000 % 60:02}:{value // 100 % 60:02}.{value % 100:02}'


def ass_document(plan, canvas, start=0.0, end=None, *, _layouts=None, _event_bounds=None):
    if plan.get('internal_schema') == 'dubforge.comment-events.v1':
        return _explicit_document(plan, canvas, start, end)
    settings = validate_settings(plan.get('settings', {}))
    items = plan.get('items')
    if not isinstance(items, list) or not items or len(items) > 100:
        raise CommentError('没有有效的已确认评论', code='COMMENTS_NOT_CONFIRMED')
    width, height = canvas
    alignment = {'top_left': 7, 'top': 8, 'top_right': 9, 'bottom_left': 1, 'bottom': 2, 'bottom_right': 3}[settings['position']]
    left, right = _horizontal_margins(settings, canvas, alignment)
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
        begin = settings['start'] + index * settings['interval']
        finish = begin + settings['duration']
        if _event_bounds is not None:
            begin, finish = _event_bounds[index]
        if finish <= start or (end is not None and begin >= end):
            continue
        # 单帧只复用当前评论；即使距下一条不足 0.1 秒也不提前排版下一条。
        if _layouts is not None and index not in _layouts:
            continue
        styles, wrapped, _ = (_layouts[index] if _layouts is not None
                              else _item_layout(item, settings, canvas, alignment))
        rendered, previous_style = [], None
        for style, lines in zip(styles, wrapped, strict=True):
            inline = ass_inline(style)
            rendered.append(('' if inline == previous_style else inline) + '\\N'.join(lines))
            previous_style = inline
        body = '\\N'.join(rendered)
        stop = min(finish, end) if end is not None else finish
        # 与横向边距一样限制旧项目的异常大边距；顶部向下、底部向上扩展。
        margin = min(round(settings['margin']), round(height * .2))
        events.append(f'Dialogue: 10,{_clock(max(begin, start)-start)},{_clock(stop-start)},Comment,,0,0,{margin},,{body}\n')
    return header + ''.join(events)


def _explicit_document(plan, canvas, start, end):
    """已冻结的成片事件可混合旧、新评论的文字、位置与样式。"""
    events = plan.get('events')
    if not isinstance(events, list) or not events or len(events) > 1000:
        raise CommentError('成片评论事件记录无效', code='COMMENTS_DISPLAY_TEXT')
    prefix, style_format, event_format = None, None, None
    styles, dialogues = [], []
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or not isinstance(event.get('item'), dict)
                or not isinstance(event.get('settings'), dict)
                or any(type(event.get(key)) not in (int, float) or not math.isfinite(event[key])
                       for key in ('start', 'end')) or event['start'] < 0 or event['end'] <= event['start']):
            raise CommentError('成片评论事件的文字、样式或时段不完整', code='COMMENTS_DISPLAY_TEXT')
        # 已进入成片合同的历史文字不重新套用自动候选长度建议。
        item = {**event['item'], 'selection_origin': 'manual'}
        document = ass_document(dict(items=[item], settings=event['settings']), canvas, start, end,
                                _event_bounds=[(event['start'], event['end'])])
        info, rest = document.split('[V4+ Styles]\n', 1)
        style_block, event_block = rest.split('[Events]\n', 1)
        style_rows, event_rows = style_block.splitlines(), event_block.splitlines()
        prefix = prefix or info
        style_format = style_format or style_rows[0]
        event_format = event_format or event_rows[0]
        name = f'Comment{index}'
        styles.extend(row.replace('Style: Comment,', f'Style: {name},', 1)
                      for row in style_rows[1:] if row.startswith('Style: '))
        for row in event_rows[1:]:
            if row.startswith('Dialogue: '):
                fields = row.split(',', 4)
                fields[3] = name
                dialogues.append(','.join(fields))
    return (prefix + '[V4+ Styles]\n' + style_format + '\n' + '\n'.join(styles) + '\n'
            + '[Events]\n' + event_format + '\n' + '\n'.join(dialogues) + '\n')


def prepare_filter(plan, video, cwd, *, start=0.0, duration=None, canvas=None, _layouts=None):
    """仅消费本轮内部 ASS，路径统一转义；调用者负责 finally 删除临时文件。"""
    if not plan:
        return '', None
    path = Path(cwd) / f'.comments_{uuid.uuid4().hex}.ass'
    data = ass_document(plan, canvas or video_canvas(video), start,
                        start + duration if duration is not None else None, _layouts=_layouts)
    atomic_write_bytes(path, data.encode('utf-8-sig'))
    return ass_file_filter(path), path


def preview_result(video, plan, seconds):
    """画面和布局说明共享一次画幅探测、一次当前评论排版。"""
    _validate_seconds(seconds)
    canvas = video_canvas(video)
    settings = validate_settings(plan.get('settings', {}))
    items = plan.get('items', [])
    index = _active_index(items, settings, seconds)
    layouts, details = {}, None
    if index is not None:
        alignment = {'top_left': 7, 'top': 8, 'top_right': 9,
                     'bottom_left': 1, 'bottom': 2, 'bottom_right': 3}[settings['position']]
        layouts[index] = _item_layout(items[index], settings, canvas, alignment)
        details = _details(layouts[index], settings, canvas)
    with tempfile.TemporaryDirectory(prefix='dubforge-comments-') as folder:
        # 单帧只排版当前位置，不为未来几十条评论重复运行字号搜索。
        graph, _ = prepare_filter(plan, video, folder, start=seconds, duration=.1,
                                  canvas=canvas, _layouts=layouts)
        data = _run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', f'{seconds:.6f}',
                     '-i', str(video.resolve()), '-vf', f'setpts=PTS-STARTPTS,{graph}',
                     '-frames:v', '1', '-an', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'], cwd=folder)
        if not data:
            raise CommentError('当前位置没有画面，请选择视频结束前的时间', code='COMMENTS_PREVIEW_EMPTY')
        return data, details


def preview_frame(video, plan, seconds):
    return preview_result(video, plan, seconds)[0]
