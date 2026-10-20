"""烧录与单帧预览共享字幕渲染；临时文件均在独立目录中，不覆盖工程产物。"""
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

from dubforge.errors import ConfigError, DubForgeError
from dubforge.utils.srt import parse_srt
from dubforge.pipeline.text_style import paint_style, validate_color, validate_opacity, validate_font, language_style, ass_inline, ass_text, ass_clock
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.ffmpeg import ass_file_filter


STYLE_DEFAULTS = {
    'assemble.subtitle_font': 'Microsoft YaHei',
    'assemble.subtitle_size': 48,
    'assemble.subtitle_auto_fit': True,
    'assemble.subtitle_min_size': 32,
    'assemble.subtitle_shrink_width_percent': 80,
    'assemble.subtitle_wrap_width_percent': 90,
    'assemble.subtitle_color': '#FFFFFF',
    'assemble.subtitle_outline_color': '#000000',
    'assemble.subtitle_outline': 2,
    'assemble.subtitle_margin': 50,
    'assemble.subtitle_background_color': '#000000',
    'assemble.subtitle_background_opacity': 0,
    'assemble.subtitle_source_style_enabled': False,
    'assemble.subtitle_source_font': 'Microsoft YaHei',
    'assemble.subtitle_source_size': 40,
    'assemble.subtitle_source_min_size': 28,
    'assemble.subtitle_source_color': '#FFFFFF',
    'assemble.subtitle_source_outline_color': '#000000',
    'assemble.subtitle_source_outline': 2,
}
PREVIEW_KEYS = {*STYLE_DEFAULTS, 'assemble.subtitle_track', 'assemble.burn_subtitles',
                'subtitles.source_first', 'subtitles.min_duration'}


def validate_style(values):
    """字体名不允许注入滤镜或 ASS 样式字段；颜色固定为六位 RGB。"""
    for key in ('assemble.subtitle_color', 'assemble.subtitle_outline_color', 'assemble.subtitle_background_color',
                'assemble.subtitle_source_color', 'assemble.subtitle_source_outline_color'):
        if key in values:
            validate_color(values[key])
    if 'assemble.subtitle_background_opacity' in values:
        validate_opacity(values['assemble.subtitle_background_opacity'])
    for key in ('assemble.subtitle_font', 'assemble.subtitle_source_font'):
        validate_font(values.get(key, STYLE_DEFAULTS[key]))
    if 'assemble.subtitle_source_style_enabled' in values and type(values['assemble.subtitle_source_style_enabled']) is not bool:
        raise ConfigError('请明确选择是否启用字幕原文独立样式')
    if 'assemble.subtitle_auto_fit' in values and type(values['assemble.subtitle_auto_fit']) is not bool:
        raise ConfigError('请明确选择是否启用长字幕自适应字号')
    # 只对完整有效值或明确的开启请求校验关系，不用缺省值猜测局部更新。
    shrink_key, wrap_key = 'assemble.subtitle_shrink_width_percent', 'assemble.subtitle_wrap_width_percent'
    if (values.get('assemble.subtitle_auto_fit') is True
            and shrink_key in values and wrap_key in values
            and type(values[shrink_key]) in (int, float) and type(values[wrap_key]) in (int, float)
            and values[shrink_key] > values[wrap_key]):
        raise ConfigError('缩小目标宽度不能大于换行上限宽度，请调整两个比例后保存。',
                          suggestion='请在字幕设置中保持缩小目标宽度不大于换行上限宽度，例如 80% 和 90%。')
    track = values.get('assemble.subtitle_track', 'trans')
    if not isinstance(track, str) or track not in {'source', 'trans', 'bilingual'}:
        raise ConfigError('请选择原文、目标语言或双语字幕')
    if 'assemble.burn_subtitles' in values and type(values['assemble.burn_subtitles']) is not bool:
        raise ConfigError('烧录字幕开关的值无效')
    if 'subtitles.source_first' in values and type(values['subtitles.source_first']) is not bool:
        raise ConfigError('双语字幕顺序的值无效，请重新选择')


def _run(cmd, **kwargs):
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DubForgeError('字幕预览处理失败，请检查 FFmpeg 是否可用或稍后重试',
                            code='SUBTITLE_PREVIEW_FAILED', detail=str(exc)) from exc
    if result.returncode:
        raise DubForgeError('字幕渲染失败，请检查视频、字体及 FFmpeg 字幕支持',
                            code='SUBTITLE_RENDER_FAILED', detail=result.stderr.decode('utf-8', 'replace')[-2000:])
    return result.stdout


def video_canvas(video):
    """按显示宽高比设定 1080 高的字幕画布，兼顾竖屏、旋转及非方形像素。"""
    raw = _run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_streams',
                '-of', 'json', str(video)])
    try:
        stream = json.loads(raw)['streams'][0]
        width, height = float(stream['width']), float(stream['height'])
        if not all(math.isfinite(value) and value > 0 for value in (width, height)):
            raise ValueError('无效的视频尺寸')
        sar = stream.get('sample_aspect_ratio', '1:1').split(':')
        if len(sar) == 2 and float(sar[0]) > 0 and float(sar[1]) > 0:
            width *= float(sar[0]) / float(sar[1])
        rotation = next((float(s['rotation']) for s in stream.get('side_data_list', [])
                         if 'rotation' in s), float(stream.get('tags', {}).get('rotate', 0)))
        if round(rotation) % 180:
            width, height = height, width
        return max(1, round(1080 * width / height)), 1080
    except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError) as exc:
        raise DubForgeError('无法读取视频画面尺寸', code='VIDEO_DIMENSIONS') from exc


def render_canvas(ctx, video):
    """本轮预览/合成共用画幅；源文件身份改变后重新探测，不留跨任务缓存。"""
    path = Path(video).resolve()
    try:
        stat = path.stat()
    except OSError:
        # 没有可核对的文件身份时仍保留原探测与错误行为，不能缓存未知结果。
        return video_canvas(video)
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    cache = getattr(ctx, '_render_canvas_cache', None)
    if cache is None:
        cache = ctx._render_canvas_cache = {}
    key = str(path)
    saved = cache.get(key)
    if saved is not None and saved[0] == identity:
        return saved[1]
    canvas = video_canvas(video)
    cache[key] = (identity, canvas)
    return canvas


def subtitle_filter(filename, cfg, canvas):
    """仅接收内部简单文件名，以工作目录规避 Windows 路径的多层转义。"""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', filename):
        raise ConfigError('字幕文件名无效')
    values = {key: cfg(key, default) for key, default in STYLE_DEFAULTS.items()}
    from dubforge.pipeline.step_params import validate_numeric_updates
    validate_numeric_updates(values)
    style = {
        'PlayResX': canvas[0], 'PlayResY': canvas[1],
        'FontName': values['assemble.subtitle_font'].strip(),
        'FontSize': values['assemble.subtitle_size'],
        **paint_style(color=values['assemble.subtitle_color'], outline_color=values['assemble.subtitle_outline_color'],
                      outline=values['assemble.subtitle_outline'], background_color=values['assemble.subtitle_background_color'],
                      background_opacity=values['assemble.subtitle_background_opacity']),
        'MarginV': values['assemble.subtitle_margin'],
        'MarginL': 50, 'MarginR': 50, 'Alignment': 2,
    }
    return f"subtitles={filename}:force_style='" + ','.join(f'{k}={v}' for k, v in style.items()) + "'"


def preview_frame(video: Path, lines, cfg, seconds: float):
    if not math.isfinite(seconds) or seconds < 0:
        raise ConfigError('预览位置必须为有效的非负秒数')
    canvas = video_canvas(video)
    with tempfile.TemporaryDirectory(prefix='dubforge-subtitle-') as folder:
        graph = prepare_subtitle_lines(lines, cfg, canvas, Path(folder))
        # 快速定位后恢复字幕时间轴；只渲染当前一帧，不合成整段视频。
        filters = f'setpts=PTS+{seconds:.6f}/TB,' + graph
        data = _run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', f'{seconds:.6f}',
                     '-i', str(video.resolve()), '-vf', filters, '-frames:v', '1', '-an',
                     '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'], cwd=folder)
        if not data:
            raise DubForgeError('当前位置没有可预览的画面，请将视频移到结束前', code='SUBTITLE_PREVIEW_EMPTY')
        return data


def separate_source_style(cfg):
    return cfg('assemble.subtitle_source_style_enabled', False) and cfg('assemble.subtitle_track', 'trans') != 'trans'


def ass_document(lines, cfg, canvas, start=0.0, end=None):
    """语言分段由统一字幕文档提供；两种语言分别缩字、换行，共用时间轴。"""
    from dubforge.pipeline.step_params import validate_numeric_updates
    from dubforge.pipeline.subtitle_layout import layout_text
    values = {key: cfg(key, default) for key, default in STYLE_DEFAULTS.items()}
    validate_numeric_updates(values)
    settings = {k.removeprefix('assemble.subtitle_'): v for k, v in values.items()}
    settings['font_size'], settings['source_font_size'] = settings['size'], settings['source_size']
    paint = paint_style(**{key: settings[key] for key in ('color', 'outline_color', 'outline', 'background_color', 'background_opacity')})
    header = ('[Script Info]\nScriptType: v4.00+\n'
        f'PlayResX: {canvas[0]}\nPlayResY: {canvas[1]}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n'
        '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: Subtitle,{validate_font(settings["font"])},{settings["font_size"]},{paint["PrimaryColour"]},&H00FFFFFF,{paint["OutlineColour"]},{paint["BackColour"]},0,0,0,0,100,100,0,0,{paint["BorderStyle"]},{paint["Outline"]},{paint["Shadow"]},2,50,50,{settings["margin"]},1\n'
        '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    events = []
    for line in lines:
        if line.end <= start or (end is not None and line.start >= end):
            continue
        parts = line.parts
        if parts is None:
            track = cfg('assemble.subtitle_track', 'trans')
            if track == 'bilingual':
                raise DubForgeError('字幕缺少语言分段信息，请从字幕工作区重新生成预览', code='SUBTITLE_LANGUAGE_PARTS')
            parts = ((track, line.text),)
        rendered = []
        for language, text in parts:
            style = language_style(settings, language == 'source')
            minimum_key = ('source_min_size' if language == 'source' and settings['source_style_enabled']
                           else 'min_size')
            # 百分比相对完整画面；不再额外乘统一比例，避免重复缩窄用户设置。
            # 仍遵守 ASS 左右各 50 的安全边距，并容纳描边与底色。
            padding = style['outline'] * 2 + (16 if settings['background_opacity'] > 0 else 0)
            def available_width(percent):
                return max(1.0, min(canvas[0] * percent / 100, canvas[0] - 100) - padding)
            shrink_width = available_width(settings['shrink_width_percent'])
            available = available_width(settings['wrap_width_percent'])
            # 正文的安全替换会改变字宽，因此先替换，再按实际渲染文字估算。
            plain = str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝')
            size, wrapped = layout_text(plain, style['font_size'], settings[minimum_key], available,
                                        settings['auto_fit'], shrink_width=shrink_width)
            rendered.append(ass_inline({**style, 'font_size': size}) + ass_text(wrapped))
        body = '\\N'.join(rendered)
        stop = min(line.end, end) if end is not None else line.end
        events.append(f'Dialogue: 0,{ass_clock(max(line.start, start)-start)},{ass_clock(stop-start)},Subtitle,,0,0,0,,{body}\n')
    return header + ''.join(events)


def prepare_subtitle_lines(lines, cfg, canvas, folder, *, name='preview', start=0.0, end=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise ConfigError('字幕文件名无效')
    # 普通、独立样式和预览都走同一份显式排版，不再依赖 libass 的分词能力。
    filename = name + '.ass'
    atomic_write_bytes(Path(folder) / filename, ass_document(lines, cfg, canvas, start, end).encode('utf-8-sig'))
    return ass_file_filter(Path(folder) / filename)


def prepare_burn_filter(ctx, video, source, start=None, duration=None):
    """所有烧录组合共用临时 ASS；导出文件保留，调用方负责清理临时文件。"""
    import uuid
    from dubforge.pipeline.step_subtitles import build_tracks
    canvas = render_canvas(ctx, video)
    track = ctx.cfg('assemble.subtitle_track', 'trans')
    # 版本执行器刚从统一稿准备的列表保留双语 parts；只复用当前上下文私有
    # 结果，旧公共 SRT 和其他版本不能绕过这里的审查与时间轴重建。
    prepared = getattr(ctx, '__dict__', {}).get('_prepared_subtitle_lines')
    if (prepared is not None and prepared['track'] == track
            and getattr(ctx, 'subtitle_path', None) == source):
        lines = prepared['lines']
    elif ctx.cfg('content_review.enabled', True) or track == 'bilingual':
        # 旧导出文件可能尚未更新；烧录始终从已筛查的统一稿重新生成临时字幕。
        tracks, _ = build_tracks(ctx, mode={'trans': 'target', 'source': 'source', 'bilingual': 'bilingual'}[track])
        lines = tracks[track + '.srt']
    else:
        # 关闭审查的历史单语导出仍可独立烧录；语言身份由明确的单语轨决定。
        lines = parse_srt(source)
        for line in lines:
            line.parts = ((track, line.text),)
    name = '.subtitle_' + uuid.uuid4().hex
    graph = prepare_subtitle_lines(lines, ctx.cfg, canvas, source.parent, name=name,
        start=start or 0, end=start + duration if start is not None and duration is not None else None)
    return graph, source.parent / (name + '.ass')
