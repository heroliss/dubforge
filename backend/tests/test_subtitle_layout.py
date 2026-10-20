"""长字幕按语言独立适配，纯排版与真实 FFmpeg 画面均不触碰用户产物。"""
import copy
import re
from types import SimpleNamespace

import numpy as np
import pytest

from dubforge.pipeline import subtitle_style
from dubforge.pipeline.subtitle_layout import layout_text, text_units
from dubforge.utils.srt import SubLine, write_srt
from test_subtitle_style import cfg, pixels, video


FIRST_TRANS = '每当我说AI领域从不缺新鲜事，我都是认真的，因为Anthropic显然又在秘密打造一个新模型'
FIRST_SOURCE = ("When I say there's never a dull day in the AI space, I truly mean it, "
                'because Anthropic is apparently cooking up yet another new model,')


def settings(**updates):
    return {**subtitle_style.STYLE_DEFAULTS, 'assemble.subtitle_track': 'bilingual',
            'assemble.subtitle_size': 80, 'assemble.subtitle_min_size': 32,
            'assemble.subtitle_auto_fit': True,
            'assemble.subtitle_shrink_width_percent': 80,
            'assemble.subtitle_wrap_width_percent': 90,
            'assemble.subtitle_source_style_enabled': True,
            'assemble.subtitle_source_size': 40, 'assemble.subtitle_source_min_size': 28,
            'assemble.subtitle_color': '#00FF00', 'assemble.subtitle_source_color': '#FF0000',
            **updates}


def event_body(document):
    return next(line.split(',', 9)[9] for line in document.splitlines() if line.startswith('Dialogue:'))


def assert_inside_width(text, size, width):
    assert all(text_units(line) * size <= width + 1e-6 for line in text.splitlines())


def test_shrink_to_single_line_before_adding_breaks():
    text = '这是一条需要适当缩小字号但可以完整单行显示的字幕'
    width = text_units(text) * 45
    size, wrapped = layout_text(text, 80, 32, width)
    assert 32 <= size < 80
    assert wrapped == text
    assert_inside_width(wrapped, size, width)


@pytest.mark.parametrize('count,expected', [(9, 'unchanged'), (10, 'shrunk')])
def test_soft_eighty_percent_starts_shrinking_before_hard_ninety_percent(count, expected):
    text = '字' * count
    size, wrapped = layout_text(text, 80, 32, 900, shrink_width=800)
    assert wrapped == text
    assert (size == 80) if expected == 'unchanged' else (32 <= size < 80)
    assert_inside_width(wrapped, size, 800)


def test_exact_soft_threshold_does_not_change_size():
    text = '正好位于缩字边界'
    width = text_units(text) * 80
    assert layout_text(text, 80, 32, width * 90 / 80, shrink_width=width) == (80, text)


@pytest.mark.parametrize('count,wraps', [(10, False), (11, True)])
def test_minimum_font_uses_buffer_until_hard_boundary(count, wraps):
    text = '字' * count
    size, wrapped = layout_text(text, 100, 80, 900, shrink_width=800)
    assert size == 80
    assert ('\n' in wrapped) == wraps
    assert wrapped.replace('\n', '') == text
    assert_inside_width(wrapped, size, 900)
    if not wraps:
        assert text_units(wrapped) * size > 800


def test_disabled_fit_ignores_soft_threshold_and_still_wraps_at_hard_boundary():
    short, long = '字' * 10, '字' * 11
    for soft in (400, 800):
        assert layout_text(short, 80, 32, 900, False, shrink_width=soft) == (80, short)
        size, wrapped = layout_text(long, 80, 32, 900, False, shrink_width=soft)
        assert size == 80 and '\n' in wrapped
        assert wrapped.replace('\n', '') == long
        assert_inside_width(wrapped, size, 900)


@pytest.mark.parametrize('auto_fit', [False, True])
def test_first_sentence_wraps_without_losing_translation(auto_fit):
    size, wrapped = layout_text(FIRST_TRANS, 80, 32, 1820, auto_fit=auto_fit)
    assert 32 <= size <= 80
    if not auto_fit:
        assert size == 80 and '\n' in wrapped
    assert wrapped.replace('\n', '') == FIRST_TRANS
    assert_inside_width(wrapped, size, 1820)


def test_minimum_size_wraps_cjk_without_spaces_and_never_ellipsizes():
    text = '这是没有空格而且很长的中文字幕，所有文字必须完整保留。' * 5
    size, wrapped = layout_text(text, 80, 32, 800)
    assert size == 32 and '\n' in wrapped
    assert wrapped.replace('\n', '') == text
    assert '…' not in wrapped
    assert_inside_width(wrapped, size, 800)


def test_english_prefers_word_boundaries_and_long_words_remain_complete():
    text = 'There is a useful space between these ordinary words'
    size, wrapped = layout_text(text, 48, 48, 430)
    assert size == 48 and '\n' in wrapped
    assert wrapped.split() == text.split()
    assert_inside_width(wrapped, size, 430)
    long_word = 'supercalifragilisticexpialidocious' * 5
    size, wrapped = layout_text(long_word, 60, 32, 200)
    assert wrapped.replace('\n', '') == long_word
    assert_inside_width(wrapped, size, 200)


@pytest.mark.parametrize('cluster', ['e\u0301', '👩🏽\u200d💻', '🇻🇳', '❤️'])
def test_wrapping_keeps_combining_characters_and_emoji_together(cluster):
    text = cluster * 20
    size, wrapped = layout_text(text, 48, 32, 150)
    assert wrapped.replace('\n', '') == text
    assert all(line.replace(cluster, '') == '' for line in wrapped.splitlines())
    assert_inside_width(wrapped, size, 150)


def test_explicit_line_breaks_survive_single_line_fitting():
    size, wrapped = layout_text('第一行\nSecond original line', 48, 32, 1820)
    assert size == 48 and wrapped == '第一行\nSecond original line'


@pytest.mark.parametrize('base,minimum', [(24, 32), (48, 60)])
def test_minimum_above_existing_size_does_not_enlarge_old_projects(base, minimum):
    text = '旧项目的较小字号不能被新增的下限反向放大' * 4
    size, wrapped = layout_text(text, base, minimum, 500)
    assert size == base and '\n' in wrapped
    assert wrapped.replace('\n', '') == text
    assert_inside_width(wrapped, size, 500)


@pytest.mark.parametrize('source_first', [False, True])
def test_bilingual_parts_fit_independently_preserve_identity_and_clip_time(source_first):
    parts = (('trans', FIRST_TRANS), ('source', FIRST_SOURCE))
    if source_first:
        parts = parts[::-1]
    line = SubLine(1, 9.5, 15, '\n'.join(text for _, text in parts), parts)
    before = copy.deepcopy(line)
    document = subtitle_style.ass_document([line], cfg(settings()), (1920, 1080), start=10, end=12)
    event = next(line for line in document.splitlines() if line.startswith('Dialogue:'))
    assert '0:00:00.00,0:00:02.00' in event
    # 语言身份来自 parts，不能把英文自动折成的两行误认为两个独立原文句子。
    body = event_body(document)
    chunks = re.split(r'(?=\{\\fn)', body)
    chunks = [chunk for chunk in chunks if chunk.startswith('{\\fn')]
    assert len(chunks) == 2
    for chunk, (language, original) in zip(chunks, parts, strict=True):
        size = float(re.search(r'\\fs([\d.]+)', chunk)[1])
        assert (28 <= size <= 40) if language == 'source' else (32 <= size <= 80)
        assert ('&H000000FF' if language == 'source' else '&H0000FF00') in chunk
        plain = re.sub(r'^\{[^}]*\}', '', chunk).replace('\\N', '')
        assert ''.join(plain.split()) == ''.join(original.split())
    assert line == before
    assert 'Dialogue:' not in subtitle_style.ass_document([line], cfg(settings()), (1920, 1080), start=20, end=21)


@pytest.mark.parametrize('percent,hard,soft', [(90, 1724, 1532), (100, 1816, 1816)])
def test_languages_share_width_percentages_but_keep_separate_font_ranges(monkeypatch, percent, hard, soft):
    from dubforge.pipeline import subtitle_layout
    calls = []

    def capture(text, base_size, min_size, available_width, auto_fit=True, *, shrink_width=None):
        calls.append((base_size, min_size, available_width, shrink_width, auto_fit))
        return base_size, text

    monkeypatch.setattr(subtitle_layout, 'layout_text', capture)
    parts = (('trans', '译文'), ('source', 'Original'))
    values = settings(**{'assemble.subtitle_wrap_width_percent': percent,
                         'assemble.subtitle_shrink_width_percent': 80 if percent == 90 else 100})
    subtitle_style.ass_document([SubLine(1, 0, 2, 'unused', parts)], cfg(values), (1920, 1080))
    assert calls == [(80, 32, hard, soft, True), (40, 28, hard, soft, True)]


def test_all_burn_paths_use_shared_layout_and_keep_export_unchanged(video, tmp_path, monkeypatch):
    from dubforge.pipeline import step_subtitles
    line = SubLine(1, 1, 2.5, FIRST_TRANS, (('trans', FIRST_TRANS),))
    source = tmp_path / 'trans.srt'
    write_srt([line], source)
    before = source.read_bytes()
    monkeypatch.setattr(step_subtitles, 'build_tracks', lambda *_, **__: ({'trans.srt': [line]}, []))
    values = settings(**{'assemble.subtitle_track': 'trans', 'assemble.subtitle_source_style_enabled': False})
    for review_enabled in (False, True):
        ctx = SimpleNamespace(cfg=cfg({**values, 'content_review.enabled': review_enabled}))
        graph, generated = subtitle_style.prepare_burn_filter(ctx, video, source, start=1.5, duration=.5)
        from dubforge.utils.ffmpeg import ass_file_filter
        assert generated.suffix == '.ass' and graph == ass_file_filter(generated)
        text = generated.read_text(encoding='utf-8-sig')
        assert '0:00:00.00,0:00:00.50' in text
        assert re.sub(r'^\{[^}]*\}', '', event_body(text)).replace('\\N', '') == FIRST_TRANS
        generated.unlink()
    assert source.read_bytes() == before
    assert line.start == 1 and line.end == 2.5 and line.text == FIRST_TRANS


@pytest.mark.parametrize('auto_fit', [False, True])
def test_real_first_sentence_keeps_both_languages_inside_frame(video, auto_fit):
    parts = (('trans', FIRST_TRANS), ('source', FIRST_SOURCE))
    line = SubLine(1, 0, 2, '\n'.join(text for _, text in parts), parts)
    rgb = pixels(subtitle_style.preview_frame(video, [line],
        cfg(settings(**{'assemble.subtitle_auto_fit': auto_fit})), 1)).astype(int)
    green = (rgb[:, :, 1] > 90) & (rgb[:, :, 1] > rgb[:, :, 0] + 40) & (rgb[:, :, 1] > rgb[:, :, 2] + 40)
    red = (rgb[:, :, 0] > 90) & (rgb[:, :, 0] > rgb[:, :, 1] + 40) & (rgb[:, :, 0] > rgb[:, :, 2] + 40)
    for mask in (green, red):
        y, x = np.where(mask)
        assert x.size > 100
        # 1080 高画布的左右安全区为 50，映射到 360 高后仍留约 16 像素。
        assert x.min() >= 13 and x.max() <= 626
        assert y.min() > 0 and y.max() < 350
    assert np.where(green)[0].mean() < np.where(red)[0].mean()
    if not auto_fit:
        # 译文 80 号关闭缩放时必须折行，且每行之间存在空白行像素。
        rows = np.flatnonzero(green.any(axis=1))
        assert (np.diff(rows) > 2).any()


@pytest.mark.parametrize('wrap_percent', [50, 90])
def test_real_hard_width_keeps_long_cjk_inside_configured_screen_fraction(video, wrap_percent):
    text = '字幕显示必须遵守配置的最大宽度，并保留完整正文。' * 3
    values = settings(**{'assemble.subtitle_track': 'trans',
                         'assemble.subtitle_size': 48,
                         'assemble.subtitle_auto_fit': False,
                         'assemble.subtitle_wrap_width_percent': wrap_percent})
    rgb = pixels(subtitle_style.preview_frame(video, [SubLine(1, 0, 2, text)], cfg(values), 1)).astype(int)
    mask = (rgb[:, :, 1] > 90) & (rgb[:, :, 1] > rgb[:, :, 0] + 40) & (rgb[:, :, 1] > rgb[:, :, 2] + 40)
    y, x = np.where(mask)
    assert x.size > 100 and y.min() > 0 and y.max() < 350
    margin = 640 * (1 - wrap_percent / 100) / 2
    assert x.min() >= margin - 2 and x.max() <= 640 - margin + 2
