"""设置入口的集成契约：默认用于新项目，服务参数供下一次调用。"""
from fastapi.testclient import TestClient

from app.main import app
from dubforge import configuration
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint


def test_global_settings_save_keeps_existing_project_and_new_project_uses_new_defaults(make_project):
    uid = make_project()
    client = TestClient(app)
    project = crud.get_project(uid)
    previous = client.get('/api/configuration/project:' + uid).json()
    old_count = previous['values']['split.target_word_count']
    count = old_count + 1
    before_fp = compute_fingerprint(project, 'split')
    global_settings = client.get('/api/configuration/global').json()
    result = client.put('/api/configuration/global', json={
        'revision': global_settings['revision'], 'fingerprint': global_settings['fingerprint'],
        'updates': {'split.target_word_count': count}})
    assert result.status_code == 200, result.text
    unchanged = client.get('/api/configuration/project:' + uid).json()
    assert unchanged['values'] == previous['values']
    assert unchanged['fingerprint'] == previous['fingerprint']
    assert compute_fingerprint(project, 'split') == before_fp
    assert configuration.resolve(make_project())['values']['split.target_word_count'] == count
    plan = client.post('/api/configuration/binding-plan', json={'uids': [uid], 'template_id': ''}).json()
    assert 'split.target_word_count' in plan['projects'][0]['changes']
    assert client.post('/api/configuration/bind', json={
        'uids': [uid], 'template_id': '', 'fingerprint': plan['fingerprint'], 'confirmed': True,
    }).status_code == 200
    applied = client.get('/api/configuration/project:' + uid).json()
    assert applied['values']['split.target_word_count'] == count
    assert compute_fingerprint(project, 'split') != before_fp


def test_model_settings_save_preserves_result_fingerprint_and_changes_next_call(make_project):
    client = TestClient(app)
    profiles = [{'name': '示例', 'base_url': 'https://example.invalid/v1',
                 'model': 'first-model', 'api_key': '', 'params': {'temperature': .2}}]
    assert client.post('/api/settings', json={'updates': {'llm.profiles': profiles}}).status_code == 200
    project = crud.get_project(make_project())
    before = {key: compute_fingerprint(project, key) for key in ('split', 'translate', 'tts', 'assemble')}
    profiles[0].update(model='next-model', base_url='https://example.invalid/next/v1',
                       params={'temperature': .8})
    result = client.post('/api/settings', json={'updates': {'llm.profiles': profiles}})
    assert result.status_code == 200, result.text
    assert before == {key: compute_fingerprint(project, key) for key in before}
    assert Dependencies(project).cfg('llm.profiles')[0]['model'] == 'next-model'
    assert configuration.resolve(project.uid)['overrides'] == {}
    assert 'llm.profiles' not in configuration.resolve(project.uid)['values']
