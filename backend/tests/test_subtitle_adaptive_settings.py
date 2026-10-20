"""字号范围沿用配置继承，并只使实际消费它的带字幕成片失效。"""
import pytest

from dubforge import configuration
from dubforge.domain import crud
from dubforge.errors import ConfigError
from dubforge.pipeline import assembly_versions as av
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint


def _save(uid, **updates):
    current = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=current['revision'], updates=updates))


def _fingerprint(uid, *, burn=True):
    deps = Dependencies(crud.get_project(uid))
    spec = dict(id='sample', name='测试版本', dubbing=False, burn_subtitles=burn,
                include_comments=False, overrides={})
    return av.fingerprint(deps, av.resolve(deps, spec))


def test_fit_range_is_inherited_and_changes_only_burned_video(make_project):
    uid = make_project()
    values = configuration.resolve(uid)['values']
    assert values['assemble.subtitle_auto_fit'] is True
    assert values['assemble.subtitle_min_size'] == 32
    assert values['assemble.subtitle_source_min_size'] == 28
    assert values['assemble.subtitle_shrink_width_percent'] == 80
    assert values['assemble.subtitle_wrap_width_percent'] == 90
    before = _fingerprint(uid)
    clean = _fingerprint(uid, burn=False)
    upstream = {key: compute_fingerprint(crud.get_project(uid), key)
                for key in ('split', 'translate', 'tts', 'subtitles')}
    _save(uid, **{'assemble.subtitle_min_size': 30})
    assert _fingerprint(uid) != before
    assert _fingerprint(uid, burn=False) == clean
    assert all(compute_fingerprint(crud.get_project(uid), key) == value
               for key, value in upstream.items())


@pytest.mark.parametrize('auto_fit', [False, True])
def test_unused_source_minimum_does_not_change_fingerprint(make_project, auto_fit):
    uid = make_project()
    _save(uid, **{'assemble.subtitle_auto_fit': auto_fit,
                  'assemble.subtitle_track': 'bilingual',
                  'assemble.subtitle_source_style_enabled': False})
    before = _fingerprint(uid)
    regular = compute_fingerprint(crud.get_project(uid), 'assemble')
    _save(uid, **{'assemble.subtitle_source_min_size': 24})
    assert _fingerprint(uid) == before
    assert compute_fingerprint(crud.get_project(uid), 'assemble') == regular


def test_disabled_fit_minima_do_not_invalidate_and_reenabling_uses_them(make_project):
    uid = make_project()
    _save(uid, **{'assemble.subtitle_auto_fit': False,
                  'assemble.subtitle_track': 'bilingual',
                  'assemble.subtitle_source_style_enabled': True})
    before = _fingerprint(uid)
    regular = compute_fingerprint(crud.get_project(uid), 'assemble')
    _save(uid, **{'assemble.subtitle_min_size': 24, 'assemble.subtitle_source_min_size': 20})
    assert _fingerprint(uid) == before
    assert compute_fingerprint(crud.get_project(uid), 'assemble') == regular
    _save(uid, **{'assemble.subtitle_auto_fit': True})
    assert _fingerprint(uid) != before


def test_fit_settings_available_to_version_overrides_and_preview():
    from dubforge.pipeline.subtitle_style import PREVIEW_KEYS
    keys = {'assemble.subtitle_auto_fit', 'assemble.subtitle_min_size',
            'assemble.subtitle_source_min_size', 'assemble.subtitle_shrink_width_percent',
            'assemble.subtitle_wrap_width_percent'}
    assert keys <= PREVIEW_KEYS
    assert keys <= av.RENDER_KEYS
    assert keys <= {field['path'] for field in av.fields()}


def test_invalid_merged_version_is_rejected_when_saving_without_changing_revision(make_project):
    from dubforge.errors import DubForgeError
    uid = make_project()
    before = configuration.resolve(uid)
    spec = dict(id='too-wide', name='过宽字幕', dubbing=False, burn_subtitles=True,
                include_comments=False, overrides={'assemble.subtitle_shrink_width_percent': 95})
    with pytest.raises(DubForgeError, match='过宽字幕.*缩小.*换行'):
        _save(uid, **{'assembly.versions': [spec], 'publish.version_ids': None})
    after = configuration.resolve(uid)
    assert after['revision'] == before['revision']
    assert after['values']['assembly.versions'] == before['values']['assembly.versions']


def test_preview_checks_partial_draft_against_project_before_reading_media(make_project, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import workbench
    uid = make_project()
    before = configuration.resolve(uid)
    def unexpected_media(*args, **kwargs):
        pytest.fail('无效宽度草稿不应读取媒体或启动渲染')
    monkeypatch.setattr(workbench, 'resolve_video', unexpected_media)
    with TestClient(app) as client:
        response = client.post(f'/api/projects/{uid}/workbench/subtitle-preview', json={
            'seconds': 1, 'settings': {'assemble.subtitle_shrink_width_percent': 95}})
    assert response.status_code == 400
    assert response.json()['code'] == 'CONFIG_ERROR'
    assert '缩小目标宽度' in response.json()['message']
    assert configuration.resolve(uid)['revision'] == before['revision']


@pytest.mark.parametrize('key,value', [
    ('assemble.subtitle_shrink_width_percent', 75),
    ('assemble.subtitle_wrap_width_percent', 85),
])
def test_width_percent_changes_only_burned_video_and_not_upstream(make_project, key, value):
    uid = make_project()
    _save(uid, **{'assemble.burn_subtitles': True})
    before, clean = _fingerprint(uid), _fingerprint(uid, burn=False)
    regular = compute_fingerprint(crud.get_project(uid), 'assemble')
    upstream = {step: compute_fingerprint(crud.get_project(uid), step)
                for step in ('split', 'translate', 'tts', 'subtitles')}
    _save(uid, **{key: value})
    assert _fingerprint(uid) != before
    assert compute_fingerprint(crud.get_project(uid), 'assemble') != regular
    assert _fingerprint(uid, burn=False) == clean
    assert all(compute_fingerprint(crud.get_project(uid), step) == fingerprint
               for step, fingerprint in upstream.items())


def test_soft_width_is_inactive_when_fit_disabled_but_hard_width_remains_active(make_project):
    uid = make_project()
    _save(uid, **{'assemble.burn_subtitles': True, 'assemble.subtitle_auto_fit': False})
    before = _fingerprint(uid)
    regular = compute_fingerprint(crud.get_project(uid), 'assemble')
    _save(uid, **{'assemble.subtitle_shrink_width_percent': 50})
    assert _fingerprint(uid) == before
    assert compute_fingerprint(crud.get_project(uid), 'assemble') == regular
    _save(uid, **{'assemble.subtitle_wrap_width_percent': 85})
    assert _fingerprint(uid) != before
    assert compute_fingerprint(crud.get_project(uid), 'assemble') != regular
    before = _fingerprint(uid)
    _save(uid, **{'assemble.subtitle_auto_fit': True})
    assert _fingerprint(uid) != before
    before = _fingerprint(uid)
    _save(uid, **{'assemble.subtitle_shrink_width_percent': 45})
    assert _fingerprint(uid) != before


@pytest.mark.parametrize('key', ['assemble.subtitle_shrink_width_percent', 'assemble.subtitle_wrap_width_percent'])
@pytest.mark.parametrize('value', [9, 101, None, True, '80', float('inf'), float('nan')])
def test_width_percent_rejects_invalid_numeric_values(key, value):
    from dubforge.pipeline.step_params import validate_numeric_updates
    with pytest.raises(ConfigError):
        validate_numeric_updates({key: value})


def test_width_relationship_checks_merged_settings_and_preserves_failed_save(make_project):
    uid = make_project()
    _save(uid, **{'assemble.subtitle_shrink_width_percent': 75})
    before = configuration.resolve(uid)
    with pytest.raises(ConfigError):
        _save(uid, **{'assemble.subtitle_wrap_width_percent': 70})
    after = configuration.resolve(uid)
    assert after['revision'] == before['revision'] and after['values'] == before['values']
    # 关闭缩字时软边界只是保留值；重新启用必须再次核对实际合并的范围。
    _save(uid, **{'assemble.subtitle_auto_fit': False, 'assemble.subtitle_wrap_width_percent': 70})
    with pytest.raises(ConfigError):
        _save(uid, **{'assemble.subtitle_auto_fit': True})
    _save(uid, **{'assemble.subtitle_shrink_width_percent': 60})
    _save(uid, **{'assemble.subtitle_auto_fit': True})


def test_version_width_overrides_validate_against_inherited_values(make_project):
    uid = make_project()
    spec = dict(id='sample', name='测试版本', dubbing=False, burn_subtitles=True,
                include_comments=False, overrides={'assemble.subtitle_wrap_width_percent': 70})
    with pytest.raises(ConfigError):
        av.resolve(Dependencies(crud.get_project(uid)), spec)
    _save(uid, **{'assemble.subtitle_auto_fit': False})
    resolved = av.resolve(Dependencies(crud.get_project(uid)), spec)
    assert resolved['values']['assemble.subtitle_wrap_width_percent'] == 70
    assert resolved['values']['assemble.subtitle_shrink_width_percent'] == 80
