"""字幕与评论共享的文字绘制规则：RGB/透明度、安全校验和独立文字底色。"""
import math
import re

from dubforge.errors import ConfigError


def validate_font(font):
    if not isinstance(font, str) or not re.fullmatch(r'[\w .\-]{1,100}', font) or not font.strip():
        raise ConfigError('请填写有效的字体名称，不含逗号、引号或换行')
    return font.strip()


def language_style(settings, source=False):
    """原文只在明确开启后采用独立字形；位置和底色仍属于整块文字。"""
    prefix = 'source_' if source and settings.get('source_style_enabled', False) else ''
    return {key: settings[prefix + key] for key in ('font', 'font_size', 'color', 'outline_color', 'outline')}


def ass_inline(style):
    """只生成经过校验的样式标签，不能将用户字体名注入 ASS 指令。"""
    return ('{\\fn' + validate_font(style['font']) + f'\\fs{style["font_size"]:g}'
            + '\\1c' + ass_color(style['color']) + '&\\3c' + ass_color(style['outline_color'])
            + f'&\\bord{validate_outline(style["outline"]):g}' + '}')


def ass_text(text):
    # 将正文与 ASS 指令分开；保留自然换行，但不执行正文中的覆盖标签。
    return str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\N')


def ass_clock(seconds):
    value = max(0, round(seconds * 100))
    return f'{value // 360000}:{value // 6000 % 60:02}:{value // 100 % 60:02}.{value % 100:02}'


def validate_color(value):
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', value):
        raise ConfigError('文字或背景颜色需为六位十六进制色值，如 #FFFFFF')
    return value.upper()


def validate_opacity(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ConfigError('背景不透明度需为 0–100，0 表示完全透明')
    return value


def validate_outline(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10:
        raise ConfigError('文字描边粗细需为 0–10，0 表示关闭描边')
    return value


def ass_color(rgb, opacity=100):
    rgb = validate_color(rgb)[1:]
    alpha = round(255 * (1 - validate_opacity(opacity) / 100))
    return f'&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}'


def paint_style(*, color='#FFFFFF', outline_color='#000000', outline=2,
                background_color='#000000', background_opacity=0):
    """libass 背景盒独立于描边；双语多行共用一块底色，透明时保留正常描边。"""
    opacity = validate_opacity(background_opacity)
    outline = validate_outline(outline)
    return {'PrimaryColour': ass_color(color), 'OutlineColour': ass_color(outline_color),
            'BackColour': ass_color(background_color, opacity), 'Outline': outline,
            'BorderStyle': 4 if opacity > 0 else 1, 'Shadow': 8 if opacity > 0 else 0}
