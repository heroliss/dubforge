"""字体真实测宽、回退与资源释放；真实渲染只用隔离生成的黑底画面。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import re
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from dubforge import comment_studio as cs
from dubforge.pipeline import comment_overlay as co, comment_text_layout as metrics
from dubforge.pipeline.subtitle_layout import layout_text, text_units


@pytest.fixture(autouse=True)
def clear_measure_cache():
    with metrics._cache_lock:
        metrics._width_cache.clear()
    yield
    with metrics._cache_lock:
        metrics._width_cache.clear()


@pytest.mark.parametrize('text', ['复杂文字 العربية', '组合 e\u0301', '表情 👩🏽\u200d💻🇻🇳', 'क ख ग'])
def test_complex_text_uses_unchanged_conservative_cluster_wrapping(text, monkeypatch):
    monkeypatch.setattr(metrics, '_native_widths', lambda *_: pytest.fail('复杂塑形不得使用简单 GDI 测量'))
    prepared = metrics.PreparedText(text, 'Microsoft YaHei')
    assert not prepared.native
    lines, widest = prepared.wrap(36, 160)
    assert lines == layout_text(text, 36, 36, 160, auto_fit=False)[1].split('\n')
    assert ''.join(lines).replace(' ', '') == text.replace(' ', '')
    assert widest == pytest.approx(max(text_units(line) for line in lines) * 36)


def test_utf16_unit_count_keeps_astral_emoji_whole():
    assert metrics._utf16_length('A👩🏽\u200d💻🇻🇳') == 12
    assert metrics._utf16_length('中文') == 2


def test_non_windows_uses_existing_widths_without_loading_native_api(monkeypatch):
    monkeypatch.setattr(metrics, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(metrics, '_gdi', lambda: pytest.fail('非 Windows 不应加载 GDI'))
    prepared = metrics.PreparedText('中文 Hello wonderful world', 'Microsoft YaHei')
    assert not prepared.native
    assert prepared.units('中文') == text_units('中文')


def test_measurement_batches_cache_numerical_widths_and_releases_each_font_context(monkeypatch):
    sessions, measured = [], []
    @contextmanager
    def font_context(api, font):
        sessions.append('open')
        try:
            yield 'dc'
        finally:
            sessions.append('close')
    monkeypatch.setattr(metrics, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(metrics, '_gdi', lambda: object())
    monkeypatch.setattr(metrics, '_font_context', font_context)
    def measure(api, dc, token):
        measured.append(token)
        return len(token) * .5
    monkeypatch.setattr(metrics, '_measure_token', measure)
    prepared = metrics.PreparedText('one two three 中文', 'Measured font')
    assert prepared.native and sessions == ['open', 'close']
    before = list(measured)
    for size in range(18, 43):
        lines, widest = prepared.wrap(size, 140)
        assert widest <= 140 and ''.join(lines).replace(' ', '') == 'onetwothree中文'
    assert measured == before
    assert metrics.PreparedText('one two three 中文', 'Measured font').native
    assert sessions == ['open', 'close']


def test_cache_is_bounded_and_parallel_readers_get_consistent_results(monkeypatch):
    monkeypatch.setattr(metrics, '_CACHE_LIMIT', 16)
    monkeypatch.setattr(metrics, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(metrics, '_gdi', lambda: object())
    @contextmanager
    def font_context(*_):
        yield 'dc'
    monkeypatch.setattr(metrics, '_font_context', font_context)
    monkeypatch.setattr(metrics, '_measure_token', lambda api, dc, token: len(token) * .5)
    def render(index):
        prepared = metrics.PreparedText(f'word{index} same', 'Measured font')
        return prepared.wrap(36, 140)[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(render, range(30)))
    assert len(metrics._width_cache) <= 16
    assert all(''.join(lines).replace(' ', '') == f'word{index}same'
               for index, lines in enumerate(results))


@pytest.mark.parametrize('failure', ['create_dc', 'create_font', 'select', 'body', None])
def test_native_handles_are_released_on_every_exit(failure, monkeypatch):
    calls = []
    class API:
        def CreateCompatibleDC(self, _):
            return 0 if failure == 'create_dc' else 11
        def CreateFontW(self, *_):
            return 0 if failure == 'create_font' else 22
        def SelectObject(self, dc, font):
            calls.append(('select', dc, font))
            return 0 if failure == 'select' else 33
        def DeleteObject(self, font):
            calls.append(('delete_font', font))
        def DeleteDC(self, dc):
            calls.append(('delete_dc', dc))
    monkeypatch.setattr(metrics, '_font_matches', lambda *_: True)
    try:
        with metrics._font_context(API(), 'Measured font') as dc:
            if failure == 'body':
                raise RuntimeError('排版异常仍需释放句柄')
            assert bool(dc) == (failure not in {'create_dc', 'create_font', 'select'})
    except RuntimeError:
        assert failure == 'body'
    assert ('delete_font', 22) in calls if failure not in {'create_dc', 'create_font'} else ('delete_font', 22) not in calls
    assert ('delete_dc', 11) in calls if failure != 'create_dc' else ('delete_dc', 11) not in calls
    if failure not in {'create_dc', 'create_font', 'select'}:
        assert ('select', 11, 33) in calls


@pytest.mark.skipif(sys.platform != 'win32', reason='验证 Windows 字体度量与真实 libass')
def test_real_font_aliases_and_missing_glyphs_fall_back_safely():
    assert metrics.PreparedText('中文 English', 'Microsoft YaHei').native
    assert metrics.PreparedText('中文 English', '微软雅黑').native
    assert not metrics.PreparedText('中文 English', 'DubForge Missing Font 20261016').native
    # Arial 本身缺少中文，不用缺字方框的宽度代表 libass 的中文回退字体。
    assert not metrics.PreparedText('中文 English', 'Arial').native


@pytest.mark.skipif(sys.platform != 'win32', reason='验证常见西文标点使用真实字体测宽')
def test_curly_quotes_dashes_and_ellipsis_use_native_widths_without_losing_text():
    text = 'It’s “clear” — this isn’t a reason to waste space… A thoughtful comment stays complete.'
    prepared = metrics.PreparedText(text, 'Microsoft YaHei')
    assert prepared.native
    lines, widest = prepared.wrap(36, 520)
    assert ''.join(lines).replace(' ', '') == text.replace(' ', '')
    assert widest <= 520
    # 同区间的方向控制符仍不能交给简单字体测量。
    assert not metrics._simple_text('text\u202Ehidden')


CHINESE = ('我很喜欢这个视频对历史背景的解释，不仅提供了丰富的信息，'
           '也让我们从不同角度理解事件的发展。希望以后能看到更多这样内容充实、表达清楚的作品。') * 3
ENGLISH = ('I really enjoyed how this video explains the historical background. '
           'It provides helpful details and gives us a fresh perspective on the story. '
           'I would love to see more thoughtful videos like this in the future. ') * 3


@pytest.mark.skipif(sys.platform != 'win32', reason='验证 Windows 字体度量与真实 libass')
@pytest.mark.parametrize('text', [CHINESE, ENGLISH])
def test_real_full_width_uses_available_area_without_clipping_text(text, tmp_path):
    settings = cs.validate_settings(dict(start=0, font='Microsoft YaHei', font_size=36, min_font_size=36,
        width_percent=100, height_percent=100, outline=0, margin=60, background_opacity=0))
    item = dict(display_text=text + ' 完整末尾ENDTAIL', selection_origin='manual')
    plan = dict(settings=settings, items=[item])
    document = co.ass_document(plan, (1920, 1080))
    body = next(line.split(',', 9)[9] for line in document.splitlines() if line.startswith('Dialogue:'))
    visible = re.sub(r'\{[^}]*\}|\\N|\s', '', body)
    assert visible == re.sub(r'\s', '', item['display_text'])
    ass = tmp_path / 'font.ass'
    ass.write_text(document, encoding='utf-8-sig')
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=black:s=1920x1080:r=1:d=1', '-vf', 'ass=font.ass', '-frames:v', '1',
        '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'], cwd=tmp_path,
        capture_output=True, check=True, timeout=30).stdout
    frame = np.frombuffer(raw, dtype=np.uint8).reshape(1080, 1920)
    ys, xs = np.where(frame > 100)
    assert len(xs) > 1000
    assert 1800 * .87 < np.ptp(xs) + 1 <= 1800
    assert xs.min() >= 60 and xs.max() < 1860
    assert ys.min() >= 60 and ys.max() < 1080


def test_short_text_does_not_stretch_and_bilingual_ranges_remain_independent():
    styles = [dict(font='Microsoft YaHei', font_size=60, min_font_size=24, outline=2),
              dict(font='Microsoft YaHei', font_size=40, min_font_size=20, outline=2)]
    fitted, wrapped, _, widest = co._full_text_layout(['短评', 'A short comment'], styles, 1800, 1000)
    assert [style['font_size'] for style in fitted] == [60, 40]
    assert wrapped == [['短评'], ['A short comment']] and widest < 900
    fitted, wrapped, total, widest = co._full_text_layout([CHINESE, ENGLISH], styles, 700, 10)
    assert [style['font_size'] for style in fitted] == [24, 20]
    assert total > 10 and widest <= 700
    assert ''.join(wrapped[0]) == CHINESE
    assert ''.join(wrapped[1]).replace(' ', '') == ENGLISH.replace(' ', '')
