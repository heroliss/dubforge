"""共享文字底色的纯接口与真实画面测试；不依赖任何用户工程。"""
import copy

import numpy as np
import pytest

from dubforge import comment_studio as cs
from dubforge.errors import ConfigError
from dubforge.pipeline import comment_overlay as co
from dubforge.pipeline.subtitle_style import STYLE_DEFAULTS, preview_frame, subtitle_filter
from dubforge.pipeline.text_style import ass_color, paint_style
from dubforge.utils.srt import SubLine
from test_subtitle_style import cfg, pixels, video


def test_shared_background_preserves_outline_and_exact_transparency():
    assert ass_color('#1234AB', 0) == '&HFFAB3412'
    assert ass_color('#1234AB', 100) == '&H00AB3412'
    transparent = paint_style(outline=3, background_opacity=0)
    solid = paint_style(outline=3, background_opacity=100, background_color='#0033FF')
    assert transparent['BorderStyle'] == 1 and transparent['Shadow'] == 0
    assert solid['BorderStyle'] == 4 and solid['BackColour'] == '&H00FF3300'
    assert solid['Outline'] == transparent['Outline'] == 3
    style = subtitle_filter('test.srt', cfg({**STYLE_DEFAULTS, 'assemble.subtitle_background_color': '#0033FF', 'assemble.subtitle_background_opacity': 100}), (1920, 1080))
    assert 'BorderStyle=4' in style and 'BackColour=&H00FF3300' in style


@pytest.mark.parametrize('values', [
    {'background_color': "#000000',Shadow=99"}, {'background_opacity': None},
    {'background_opacity': -1}, {'background_opacity': 101}, {'background_opacity': True},
])
def test_bad_background_is_rejected(values):
    with pytest.raises(ConfigError):
        paint_style(**values)
    from dubforge.pipeline.step_params import validate_numeric_updates
    with pytest.raises(ConfigError):
        validate_numeric_updates({f'assemble.subtitle_{key}': val for key, val in values.items()})


def test_real_subtitle_background_color_alpha_and_bilingual_box(video):
    lines = [SubLine(1, 0, 2, '双语字幕\nEnglish subtitle')]
    base = {**STYLE_DEFAULTS, 'assemble.subtitle_background_color': '#00FF00', 'assemble.subtitle_size': 60}
    images = [pixels(preview_frame(video, lines, cfg({**base, 'assemble.subtitle_background_opacity': alpha}), 1)) for alpha in (0, 50, 100)]
    green = lambda p: (p[:, :, 1] > p[:, :, 0] + 30) & (p[:, :, 0] < 40) & (p[:, :, 2] < 40)
    assert not green(images[0]).any()
    assert green(images[1]).sum() > 300 and green(images[2]).sum() > 300
    assert images[2][:, :, 1][green(images[2])].mean() > images[1][:, :, 1][green(images[1])].mean() + 70


def test_real_comment_top_left_bilingual_and_transparent_background(video):
    plan = {'settings': {**cs.DEFAULT_SETTINGS, 'start': 0, 'duration': 2, 'background_color': '#0033FF', 'background_opacity': 100,
                         'language_mode': 'bilingual'}, 'items': [{'display_text': '很棒的氛围，感谢分享！', 'text': 'Great vibes, thanks for sharing!'}]}
    left = pixels(co.preview_frame(video, plan, 1))
    blue = lambda p: (p[:, :, 2].astype(int) - p[:, :, 0].astype(int) > 80)
    mask = blue(left); y, x = np.where(mask)
    assert len(x) > 500 and x.mean() < 250 and y.mean() < 100 and x.min() < 35
    transparent = copy.deepcopy(plan); transparent['settings']['background_opacity'] = 0
    assert not blue(pixels(co.preview_frame(video, transparent, 1))).any()
    right = copy.deepcopy(plan); right['settings']['position'] = 'top_right'
    assert np.where(blue(pixels(co.preview_frame(video, right, 1))))[1].mean() > 350


@pytest.mark.parametrize('values', [
    {'outline_color': 'red'}, {'outline_color': "#000000',Outline=99"}, {'outline_color': None},
    {'outline': None}, {'outline': True}, {'outline': '2'}, {'outline': -1}, {'outline': 11},
    {'outline': float('inf')}, {'outline': float('nan')},
])
def test_invalid_comment_outline_rejected_by_save_and_renderer(values):
    with pytest.raises(ConfigError):
        cs.validate_settings(values)
    with pytest.raises(ConfigError):
        paint_style(**values)


def test_legacy_comment_outline_defaults_and_ass_styles():
    settings = cs.validate_settings({'color': '#FFFFFF'})
    assert settings['outline_color'] == '#000000' and settings['outline'] == 2
    plan = {'settings': {**settings, 'outline_color': '#12AB34', 'outline': 4.5},
            'items': [{'display_text': '带描边评论'}]}
    style = next(line for line in co.ass_document(plan, (1920, 1080)).splitlines() if line.startswith('Style:'))
    fields = style.split(',')
    assert fields[5] == '&H0034AB12' and fields[16] == '4.5'


@pytest.mark.parametrize('kind', ['comment', 'subtitle'])
def test_real_outline_color_width_zero_and_background_independence(video, kind):
    def render(width, color='#00FF00', opacity=0):
        style = {'outline': width, 'outline_color': color, 'background_color': '#0000FF', 'background_opacity': opacity}
        if kind == 'comment':
            plan = {'settings': {**cs.DEFAULT_SETTINGS, **style, 'start': 0}, 'items': [{'display_text': '描边 Outline'}]}
            return pixels(co.preview_frame(video, plan, 1)).astype(int)
        settings = {**STYLE_DEFAULTS, 'assemble.subtitle_size': 90, **{f'assemble.subtitle_{key}': value for key, value in style.items()}}
        return pixels(preview_frame(video, [SubLine(1, 0, 2, '描边 Outline')], cfg(settings), 1)).astype(int)

    green = lambda p: ((p[:, :, 1] > p[:, :, 0] + 30) & (p[:, :, 1] > p[:, :, 2] + 30)).sum()
    assert green(render(0)) == 0
    thin = green(render(2)); thick = green(render(6))
    assert thin > 20 and thick > thin * 1.3
    red = render(6, color='#FF0000')
    assert green(red) == 0 and ((red[:, :, 0] > red[:, :, 1] + 30) & (red[:, :, 0] > red[:, :, 2] + 30)).sum() > 20
    background = render(6, opacity=100)
    assert green(background) > 20
    assert ((background[:, :, 2] > background[:, :, 0] + 30) & (background[:, :, 2] > background[:, :, 1] + 30)).sum() > 20
