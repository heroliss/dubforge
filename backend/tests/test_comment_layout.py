"""评论宽度、按词换行、继承和实际 FFmpeg 预览；不访问用户项目。"""
import numpy as np
import pytest

from dubforge import comment_studio as cs, configuration
from dubforge.errors import DubForgeError
from dubforge.domain import crud
from dubforge.pipeline import comment_overlay as co, assembly_versions as av
from dubforge.pipeline.dependencies import Dependencies
from test_configuration import save, bind
from test_subtitle_style import video, pixels


def test_full_text_layout_uses_maximum_when_wrapped_height_fits():
    styles = [dict(font_size=36, min_font_size=18, outline=2)]
    fitted, lines, total, widest = co._full_text_layout(['hello wonderful world'], styles, 240, 240)
    assert fitted[0]['font_size'] == 36
    assert len(lines[0]) >= 2
    assert ''.join(lines[0]).replace(' ', '') == 'hellowonderfulworld'
    assert total <= 240 and widest <= 240


def test_full_text_layout_shrinks_only_within_range_before_extending_height():
    parts = ['hello world ' * 8]
    styles = [dict(font_size=60, min_font_size=18, outline=2)]
    fitted, lines, total, widest = co._full_text_layout(parts, styles, 300, 180)
    assert 18 < fitted[0]['font_size'] < 60
    assert total <= 180.000001 and widest <= 300.000001
    assert ''.join(lines[0]).replace(' ', '') == parts[0].replace(' ', '')
    extended, extended_lines, extended_total, _ = co._full_text_layout(parts, styles, 300, 20)
    assert extended[0]['font_size'] == 18
    assert extended_total > 20
    assert ''.join(extended_lines[0]).replace(' ', '') == parts[0].replace(' ', '')


def test_language_ranges_keep_independent_minima_and_shared_height():
    styles = [dict(font_size=60, min_font_size=24, outline=3),
              dict(font_size=30, min_font_size=22, outline=1)]
    parts = ['中文' * 100, 'one two three four ' * 40]
    fitted, wrapped, total, _ = co._full_text_layout(parts, styles, 300, 80)
    assert [style['font_size'] for style in fitted] == [24, 22]
    assert fitted[0]['outline'] == pytest.approx(1.2)
    assert fitted[1]['outline'] == pytest.approx(22 / 30)
    assert total > 80
    for original, lines in zip(parts, wrapped):
        assert ''.join(lines).replace(' ', '') == original.replace(' ', '')


@pytest.mark.parametrize('text', ['MWWM' * 100, 'https://example.com/' * 40,
                                  '👩🏽\u200d💻🇻🇳❤️' * 25, '中文，完整文字。' * 30])
def test_complete_width_wrapping_preserves_wide_letters_urls_and_clusters(text):
    from dubforge.pipeline.subtitle_layout import text_units
    styles = [dict(font_size=36, min_font_size=18, outline=2)]
    fitted, wrapped, _, widest = co._full_text_layout([text], styles, 260, 80)
    assert ''.join(wrapped[0]).replace(' ', '') == text.replace(' ', '')
    assert widest <= 260.000001
    assert all(text_units(line) * fitted[0]['font_size'] <= 260 for line in wrapped[0])
    assert '…' not in ''.join(wrapped[0])


@pytest.mark.parametrize('alignment', [1, 2, 3, 7, 8, 9])
def test_width_percent_alignment_and_margin_limit(alignment):
    settings = cs.validate_settings({'width_percent': 50, 'margin': 60})
    left, right = co._horizontal_margins(settings, (1920, 1080), alignment)
    assert 1920 - left - right == 960
    assert (left == 60 if alignment in {1, 7} else right == 60 if alignment in {3, 9} else left == right)
    settings['width_percent'] = 100
    assert co._horizontal_margins(settings, (1920, 1080), alignment) == (60, 60)


@pytest.mark.parametrize('alignment', [1, 2, 3, 7, 8, 9])
@pytest.mark.parametrize('canvas', [(1920, 1080), (1080, 1920), (1081, 1081)])
def test_automatic_width_uses_available_area_in_every_position(canvas, alignment):
    settings = cs.validate_settings({'width_percent': 0, 'margin': 20})
    assert co._horizontal_margins(settings, canvas, alignment) == (20, 20)
    settings['width_percent'] = 100
    assert co._horizontal_margins(settings, canvas, alignment) == (20, 20)


def test_automatic_width_and_full_width_share_the_same_complete_render(video):
    settings = cs.validate_settings(dict(start=.5, position='bottom', width_percent=0,
        font_size=36, min_font_size=36, height_percent=100, margin=24))
    plan = dict(settings=settings, items=[dict(display_text='完整中文与 English ' * 12)])
    automatic_ass = co.ass_document(plan, (640, 360))
    automatic_frame = co.preview_frame(video, plan, .5)
    settings['width_percent'] = 100
    assert co.ass_document(plan, (640, 360)) == automatic_ass
    assert co.preview_frame(video, plan, .5) == automatic_frame


@pytest.mark.parametrize('value', [-1, 101, None, True, '50', float('nan'), float('inf')])
def test_invalid_width_is_rejected(value):
    with pytest.raises(DubForgeError): cs.validate_settings({'width_percent': value})
    with pytest.raises(DubForgeError): av.validate(dict(id='test', name='测试', dubbing=False,
        burn_subtitles=False, include_comments=True, overrides={'comments.width_percent': value}))


def test_width_inherits_global_series_project_and_version_and_changes_only_comment_fingerprint(make_project):
    uid = make_project()
    save('global', {'comments.width_percent': 75})
    template = configuration.create_template('旅行系列')
    save(template['key'], {'comments.width_percent': 60}); bind([uid], template)
    assert cs.project_settings(uid)['width_percent'] == 60
    save('project:' + uid, {'comments.width_percent': 50})
    assert cs.project_settings(uid)['width_percent'] == 50
    definition = dict(id='test', name='测试', dubbing=False, burn_subtitles=False,
        include_comments=True, overrides={'comments.width_percent': 40})
    deps = Dependencies(crud.get_project(uid))
    plan = {'settings': cs.project_settings(uid), 'items': [{'display_text': '测试宽度'}]}
    resolved = av.resolve(deps, definition, comment_plan=plan)
    assert resolved['values']['comments.width_percent'] == resolved['comment_overlay']['settings']['width_percent'] == 40
    before = av.fingerprint(deps, resolved)
    definition['overrides']['comments.width_percent'] = 65
    assert av.fingerprint(deps, av.resolve(deps, definition, comment_plan=plan)) != before
    definition['include_comments'] = False
    before = av.fingerprint(deps, av.resolve(deps, definition))
    definition['overrides']['comments.width_percent'] = 30
    assert av.fingerprint(deps, av.resolve(deps, definition)) == before
    save('project:' + uid, reset=['comments.width_percent'])
    assert cs.project_settings(uid)['width_percent'] == 60
    save(template['key'], reset=['comments.width_percent'])
    assert cs.project_settings(uid)['width_percent'] == 60
    bind([uid], template)
    assert cs.project_settings(uid)['width_percent'] == 75


def test_render_revision_invalidates_old_wrapping_without_changing_source_plan():
    plan = dict(settings={'width_percent': 0}, items=[{'display_text': 'Hello world'}])
    assert cs.render_fingerprint_plan(plan) == dict(settings={'width_percent': 100, 'min_font_size': 18, 'height_percent': 30},
        items=plan['items'], render_version='font-metrics-range-v5')
    assert 'render_version' not in plan and plan['settings']['width_percent'] == 0


def test_confirmation_timestamp_is_excluded_from_render_fingerprint():
    plan = dict(settings=cs.default_settings(), items=[{'display_text': 'Hello world'}],
                confirmed_at='2026-10-10T05:00:00+00:00')
    without_timestamp = {key: value for key, value in plan.items() if key != 'confirmed_at'}
    assert cs.render_fingerprint_plan(plan) == cs.render_fingerprint_plan(without_timestamp)
    # 仅供历史 AssemblyResult 读取；旧指纹仍能按原规则重算。
    assert cs.render_fingerprint_plan(plan, legacy=True) != cs.render_fingerprint_plan(plan)


@pytest.mark.parametrize('independent', [False, True])
def test_extremely_narrow_width_reports_layout_issue_instead_of_drawing_outside(independent):
    plan = dict(settings=cs.validate_settings(dict(width_percent=1, language_mode='original',
        source_style_enabled=independent)), items=[dict(source_text='好')])
    with pytest.raises(cs.CommentError, match='增大评论区域宽度') as caught:
        co.ass_document(plan, (608, 1080))
    assert caught.value.code == 'COMMENTS_LAYOUT_OVERFLOW'


def test_long_bilingual_comment_expands_height_without_ellipsis():
    settings = cs.validate_settings(dict(
        start=30, interval=25, duration=15, position='top_left', font_size=50, margin=20,
        width_percent=0, show_author=True, language_mode='bilingual', source_first=False,
        source_style_enabled=True, source_font_size=30, min_font_size=24, source_min_font_size=20,
        height_percent=5,
    ))
    plan = dict(settings=settings, items=[dict(
        display_text='这是一条很长的中文评论，' * 19,
        source_text='This is a long comment that should remain readable while fitting the overlay. ' * 7,
        author='@viewer', selection_origin='manual',
    )])
    ass = co.ass_document(plan, (1920, 1080))
    assert '…' not in ass
    assert '\\fs24' in ass and '\\fs20' in ass
    assert 'This' in ass and 'readable' in ass
    assert 'Dialogue:' in ass


@pytest.mark.parametrize('independent', [False, True])
def test_bilingual_ass_word_breaks_apply_to_both_style_modes(independent):
    source = 'Yahoo Sierra and Tanner wonderful adventures together forever'
    plan = dict(settings=cs.validate_settings(dict(width_percent=35, language_mode='bilingual',
        source_style_enabled=independent, font_size=48, source_font_size=40)),
        items=[dict(display_text='旅途愉快，祝福你们！', source_text=source)])
    ass = co.ass_document(plan, (1920, 1080))
    assert '\\N' in ass
    for word in source.split(): assert word in ass
    assert '旅途愉快' in ass


def test_real_preview_wraps_more_when_narrowed_and_filter_uses_same_ass(video, tmp_path):
    text = 'We love this beautiful adventure together and hope to see you again!'
    plan = dict(settings=cs.validate_settings(dict(start=.5, duration=2, width_percent=80,
        font='Arial', font_size=50, color='#FF0000', outline=0, background_opacity=0)),
        items=[dict(display_text=text)])
    wide = pixels(co.preview_frame(video, plan, 1))
    plan['settings']['width_percent'] = 35
    narrow = pixels(co.preview_frame(video, plan, 1))
    bounds = []
    for frame in (wide, narrow):
        ys, xs = np.where((frame[:, :, 0] > 100) & (frame[:, :, 1] < 50))
        assert len(xs) > 200
        bounds.append((np.ptp(xs), np.ptp(ys)))
    assert bounds[1][0] < bounds[0][0] and bounds[1][1] > bounds[0][1]
    assert bounds[1][0] <= 640 * .35 + 5
    graph, path = co.prepare_filter(plan, video, tmp_path)
    from dubforge.utils.ffmpeg import ass_file_filter
    assert graph == ass_file_filter(path)
    assert path.read_text(encoding='utf-8-sig') == co.ass_document(plan, co.video_canvas(video))


def test_single_frame_only_lays_out_active_comment_and_keeps_timeline_gaps(video, monkeypatch):
    settings = cs.validate_settings(dict(start=.3, interval=1.2, duration=1, show_author=False,
        font='Arial', font_size=36, min_font_size=18, color='#FF0000', outline=0, background_opacity=0))
    plan = dict(settings=settings, items=[dict(display_text=f'Full comment number {index}') for index in range(100)])
    rendered = []
    original = co._item_layout
    def record(item, *args):
        rendered.append(item['display_text'])
        return original(item, *args)
    monkeypatch.setattr(co, '_item_layout', record)
    for seconds, active in [(.3, True), (1.3, False), (1.5, True), (2.5, False), (2.7, True), (2.9, True)]:
        frame = pixels(co.preview_frame(video, plan, seconds))
        assert (frame.max() > 100) == active
        assert (co.layout_details(plan, co.video_canvas(video), seconds) is not None) == active
    # layout_details 和单帧各只处理当前条；未来的 97 条不做字号搜索。
    assert rendered == [f'Full comment number {index}' for index in [0, 0, 1, 1, 2, 2, 2, 2]]


@pytest.mark.parametrize('mode,independent,expected', [
    ('translated', True, [24]), ('original', True, [20]), ('original', False, [24]),
    ('bilingual', True, [24, 20]), ('bilingual', False, [24, 24]),
])
def test_minimum_size_report_tracks_language_order_and_independent_style(mode, independent, expected):
    settings = cs.validate_settings(dict(start=0, show_author=False, language_mode=mode,
        source_first=False, source_style_enabled=independent, width_percent=30, height_percent=1,
        font_size=60, min_font_size=24, source_font_size=40, source_min_font_size=20))
    plan = dict(settings=settings, items=[dict(display_text='完整正文' * 50,
        source_text='Complete original content ' * 20, selection_origin='manual')])
    details = co.layout_details(plan, (1920, 1080), 0)
    assert details['font_sizes'] == expected
    assert details['min_reached'] and details['height_exceeded']
