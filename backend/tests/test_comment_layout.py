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


@pytest.mark.parametrize('text,units,expected', [
    ('Hello wonderful world', 9, ['Hello wonderful', 'world']),
    ("We don't split words.", 6, ["We don't", 'split', 'words.']),
    ('one   two three', 5, ['one two', 'three']),
    ('你好 wonderful 世界！', 6, ['你好', 'wonderful', '世界！']),
    ('cafe\u0301 nice', 3, ['cafe\u0301', 'nice']),
    ('你好，世界！', 3, ['你好，', '世界！']),
    ('abcdefghijk', 3, ['abcde', 'fghij', 'k']),
    ('  hello  ', 4, ['hello']),
    ('', 5, []),
])
def test_word_wrapping_preserves_words_and_mixed_text(text, units, expected):
    assert co._wrap(text, units) == expected
    assert all(co._units(line) <= units + 1e-9 for line in expected)


@pytest.mark.parametrize('cluster', ['e\u0301', '👩🏽\u200d💻', '🇻🇳', '❤️'])
def test_long_unbroken_text_never_splits_combining_marks_or_emoji(cluster):
    lines = co._wrap(cluster * 10, 2)
    assert ''.join(lines) == cluster * 10
    assert all(line.replace(cluster, '') == '' for line in lines)
    assert all(co._units(line) <= 2 + 1e-9 for line in lines)


@pytest.mark.parametrize('alignment', [1, 2, 3, 7, 8, 9])
def test_width_percent_alignment_and_margin_limit(alignment):
    settings = cs.validate_settings({'width_percent': 50, 'margin': 60})
    left, right = co._horizontal_margins(settings, (1920, 1080), alignment)
    assert 1920 - left - right == 960
    assert (left == 60 if alignment in {1, 7} else right == 60 if alignment in {3, 9} else left == right)
    settings['width_percent'] = 100
    assert co._horizontal_margins(settings, (1920, 1080), alignment) == (60, 60)


def test_automatic_width_preserves_landscape_portrait_and_center_layout():
    settings = cs.validate_settings({'width_percent': 0, 'margin': 20})
    assert co._horizontal_margins(settings, (1920, 1080), 7) == (20, 594)
    assert co._horizontal_margins(settings, (1080, 1920), 7) == (20, 88)
    assert co._horizontal_margins(settings, (1920, 1080), 8) == (154, 154)


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
    assert cs.project_settings(uid)['width_percent'] == 75


def test_render_revision_invalidates_old_wrapping_without_changing_source_plan():
    plan = dict(settings={'width_percent': 0}, items=[{'display_text': 'Hello world'}])
    assert cs.render_fingerprint_plan(plan) == dict(settings={}, items=plan['items'], render_version='word-wrap-v2')
    assert 'render_version' not in plan and plan['settings']['width_percent'] == 0


@pytest.mark.parametrize('independent', [False, True])
def test_extremely_narrow_width_reports_layout_issue_instead_of_drawing_outside(independent):
    plan = dict(settings=cs.validate_settings(dict(width_percent=1, language_mode='original',
        source_style_enabled=independent)), items=[dict(source_text='好')])
    with pytest.raises(cs.CommentError, match='增大宽度') as caught:
        co.ass_document(plan, (608, 1080))
    assert caught.value.code == 'COMMENTS_LAYOUT_OVERFLOW'


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
    assert graph == f'ass={path.name}'
    assert path.read_text(encoding='utf-8-sig') == co.ass_document(plan, co.video_canvas(video))
