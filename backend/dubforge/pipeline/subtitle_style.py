"""烧录与单帧预览共享字幕渲染；临时文件均在独立目录中，不覆盖工程产物。"""
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

from dubforge.errors import ConfigError, DubForgeError
from dubforge.utils.srt import write_srt
from dubforge.pipeline.text_style import paint_style, validate_color, validate_opacity, validate_font, language_style, ass_inline, ass_text, ass_clock
from dubforge.utils.fs import atomic_write_bytes


STYLE_DEFAULTS = {
    'assemble.subtitle_font': 'Microsoft YaHei',
    'assemble.subtitle_size': 48,
    'assemble.subtitle_color': '#FFFFFF',
    'assemble.subtitle_outline_color': '#000000',
    'assemble.subtitle_outline': 2,
    'assemble.subtitle_margin': 50,
    'assemble.subtitle_background_color': '#000000',
    'assemble.subtitle_background_opacity': 0,
    'assemble.subtitle_source_style_enabled': False,
    'assemble.subtitle_source_font': 'Microsoft YaHei',
    'assemble.subtitle_source_size': 40,
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
    """语言分段由统一字幕文档提供；顺序变化不改变原文/译文的样式身份。"""
    from dubforge.pipeline.step_params import validate_numeric_updates
    values = {key: cfg(key, default) for key, default in STYLE_DEFAULTS.items()}
    validate_numeric_updates(values)
    settings = {k.removeprefix('assemble.subtitle_'): v for k, v in values.items()}
    settings['font_size'], settings['source_font_size'] = settings['size'], settings['source_size']
    paint = paint_style(**{key: settings[key] for key in ('color', 'outline_color', 'outline', 'background_color', 'background_opacity')})
    header = ('[Script Info]\nScriptType: v4.00+\n'
        f'PlayResX: {canvas[0]}\nPlayResY: {canvas[1]}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n'
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
        body = '\\N'.join(ass_inline(language_style(settings, language == 'source')) + ass_text(text) for language, text in parts)
        stop = min(line.end, end) if end is not None else line.end
        events.append(f'Dialogue: 0,{ass_clock(max(line.start, start)-start)},{ass_clock(stop-start)},Subtitle,,0,0,0,,{body}\n')
    return header + ''.join(events)


def prepare_subtitle_lines(lines, cfg, canvas, folder, *, name='preview', start=0.0, end=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise ConfigError('字幕文件名无效')
    if separate_source_style(cfg):
        filename = name + '.ass'
        atomic_write_bytes(Path(folder) / filename, ass_document(lines, cfg, canvas, start, end).encode('utf-8-sig'))
        return f'ass={filename}'
    write_srt(lines, Path(folder) / (name + '.srt'))
    return subtitle_filter(name + '.srt', cfg, canvas)


def prepare_burn_filter(ctx, video, source, start=None, duration=None):
    """独立样式只生成临时 ASS，普通样式继续复用已导出的 SRT；调用方负责清理。"""
    import uuid
    from dubforge.pipeline.step_subtitles import build_tracks
    canvas = video_canvas(video)
    if ctx.cfg('content_review.enabled', True):
        # 旧导出文件可能尚未更新；烧录始终从已筛查的统一稿重新生成临时字幕。
        track = ctx.cfg('assemble.subtitle_track', 'trans')
        tracks, _ = build_tracks(ctx, mode={'trans': 'target', 'source': 'source', 'bilingual': 'bilingual'}[track])
        name = '.subtitle_' + uuid.uuid4().hex
        lines = tracks[track + '.srt']
        if start is not None and not separate_source_style(ctx.cfg):
            from dubforge.utils.srt import SubLine
            lines = [SubLine(l.index, max(0.0, l.start-start), min(l.end, start+duration)-start, l.text)
                     for l in lines if l.end > start and l.start < start+duration]
        graph = prepare_subtitle_lines(lines, ctx.cfg, canvas, source.parent, name=name,
            start=start or 0 if separate_source_style(ctx.cfg) else 0,
            end=start + duration if separate_source_style(ctx.cfg) and start is not None else None)
        return graph, source.parent / (name + ('.ass' if separate_source_style(ctx.cfg) else '.srt'))
    if separate_source_style(ctx.cfg):
        track = ctx.cfg('assemble.subtitle_track', 'trans')
        tracks, _ = build_tracks(ctx, mode='target' if track == 'trans' else track)
        name = '.subtitle_' + uuid.uuid4().hex
        graph = prepare_subtitle_lines(tracks[track + '.srt'], ctx.cfg, canvas, source.parent, name=name,
            start=start or 0, end=start + duration if start is not None and duration is not None else None)
        return graph, source.parent / (name + '.ass')
    if start is not None:
        from dubforge.pipeline.step_assemble import _write_preview_srt
        path = source.with_name('.preview_' + uuid.uuid4().hex + '.srt')
        graph = subtitle_filter(path.name, ctx.cfg, canvas)
        _write_preview_srt(source, start, start + duration, path)
        return graph, path
    return subtitle_filter(source.name, ctx.cfg, canvas), None
