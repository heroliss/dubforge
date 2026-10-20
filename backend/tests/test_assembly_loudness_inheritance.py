"""成片响度按真实层级恢复：全局、项目保存基础、项目共同参数、版本专用参数。"""
import copy

from fastapi.testclient import TestClient

from app.main import app
from dubforge import configuration as cfg
from dubforge.pipeline import assembly_versions


MODE = 'assemble.final_loudness_mode'
TARGET = 'assemble.final_loudness_lufs'


def _save(key, updates=None, reset=None):
    current = cfg.resolve(key=key)
    return cfg.save(key, dict(revision=current['revision'], fingerprint=current['fingerprint'],
                              updates=updates or {}, reset=reset or []))


def _step_params(client, uid):
    response = client.get(f'/api/projects/{uid}/step_params')
    assert response.status_code == 200, response.text
    return response.json()


def _reset_project(client, uid, paths):
    current = _step_params(client, uid)
    response = client.put(f'/api/projects/{uid}/step_params', json=dict(
        reset=paths, revision=current['revision'], fingerprint=current['fingerprint']))
    assert response.status_code == 200, response.text
    return _step_params(client, uid)


def test_project_loudness_reset_uses_saved_base_until_latest_defaults_are_applied(make_project):
    _save('global', {MODE: 'off', TARGET: -16.})
    uid = make_project()
    _save('global', {MODE: 'uniform', TARGET: -14.})
    _save('project:' + uid, {MODE: 'normalize', TARGET: -12.})
    client = TestClient(app)

    before = _step_params(client, uid)
    assert before['steps']['assemble']['values'][MODE] == 'normalize'
    assert before['steps']['assemble']['values'][TARGET] == -12.
    assert before['defaults'][MODE] == 'off'
    assert before['defaults'][TARGET] == -16.
    assert before['parent_sources'][TARGET] == {'kind': 'global', 'frozen': True}

    restored = _reset_project(client, uid, [MODE, TARGET])
    assert restored['steps']['assemble']['values'][MODE] == 'off'
    assert restored['steps']['assemble']['values'][TARGET] == -16.
    assert not restored['overridden'][MODE] and not restored['overridden'][TARGET]
    shared = assembly_versions.view(uid)['defaults']
    assert shared[MODE] == restored['defaults'][MODE] == 'off'
    assert shared[TARGET] == restored['defaults'][TARGET] == -16.
    assert cfg.resolve()['values'][MODE] == 'uniform'
    assert cfg.resolve()['values'][TARGET] == -14.


def test_apply_loudness_defaults_preserves_overrides_and_reset_uses_correct_layer(make_project):
    _save('global', {MODE: 'off', TARGET: -16.})
    uid = make_project()
    _save('global', {MODE: 'uniform', TARGET: -14.})
    _save('project:' + uid, {MODE: 'normalize'})
    catalog = assembly_versions.catalog(uid)
    definitions = copy.deepcopy(catalog['versions'])
    identifier = definitions[0]['id']
    definitions[0]['overrides'].update({MODE: 'off', TARGET: -18.})
    assembly_versions.save(uid, dict(versions=definitions, revision=catalog['revision'],
                                      fingerprint=catalog['fingerprint']))
    client = TestClient(app)
    url = f'/api/projects/{uid}/assembly-versions'

    response = client.get(url + '/defaults-plan')
    assert response.status_code == 200, response.text
    plan = response.json()
    changes = {row['path']: row for row in plan['changes']}
    assert changes[MODE]['before'] == 'off' and changes[MODE]['after'] == 'uniform'
    assert changes[MODE]['overridden'] is True
    assert changes[MODE]['effective_before'] == changes[MODE]['effective_after'] == 'normalize'
    assert changes[TARGET]['before'] == -16. and changes[TARGET]['after'] == -14.
    assert changes[TARGET]['effective_before'] == -16.
    assert changes[TARGET]['effective_after'] == -14.
    response = client.post(url + '/apply-defaults', json=dict(
        fingerprint=plan['fingerprint'], confirmed=True))
    assert response.status_code == 200, response.text
    view = response.json()
    project = _step_params(client, uid)

    # 项目基础更新，但已明确选择的共同处理方式及版本专用参数继续优先。
    assert project['defaults'][MODE] == 'uniform'
    assert project['defaults'][TARGET] == -14.
    assert project['steps']['assemble']['values'][MODE] == 'normalize'
    assert view['defaults'][MODE] == 'normalize'
    assert view['defaults'][TARGET] == project['steps']['assemble']['values'][TARGET] == -14.
    version = next(row for row in view['versions'] if row['id'] == identifier)
    assert version['values'][MODE] == 'off' and version['values'][TARGET] == -18.
    assert version['overrides'][MODE] == 'off'
    assert version['overrides'][TARGET] == -18.

    # 移除版本专用参数只恢复项目共同参数，不能越级恢复全局或出厂值。
    definitions = [{key: copy.deepcopy(row[key]) for key in
                    ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
                   for row in view['versions']]
    current = next(row for row in definitions if row['id'] == identifier)
    current['overrides'].pop(MODE)
    current['overrides'].pop(TARGET)
    response = client.put(url, json=dict(versions=definitions, revision=view['revision'],
                                         fingerprint=view['fingerprint']))
    assert response.status_code == 200, response.text
    version = next(row for row in response.json()['versions'] if row['id'] == identifier)
    assert version['values'][MODE] == 'normalize'
    assert version['values'][TARGET] == -14.

    # 再移除项目共同覆盖，才恢复刚应用的项目基础值；版本随项目一起更新。
    project = _reset_project(client, uid, [MODE])
    assert project['steps']['assemble']['values'][MODE] == project['defaults'][MODE] == 'uniform'
    response = client.get(url)
    assert response.status_code == 200, response.text
    view = response.json()
    version = next(row for row in view['versions'] if row['id'] == identifier)
    assert view['defaults'][MODE] == version['values'][MODE] == 'uniform'
    assert view['defaults'][TARGET] == version['values'][TARGET] == -14.
    assert version['sources'][MODE] == {'kind': 'global', 'frozen': True}
