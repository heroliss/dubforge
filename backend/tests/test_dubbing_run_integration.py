"""正式制作入口共用配音意图；隔离数据库、伪 TTS 和成片，绝不投稿。"""
import copy
import json
import time
import uuid

import pytest

from conftest import mark_upstream_done, synthesize, wait_status
from dubforge import workflows, workflow_plan
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit
from dubforge.runner import runner
from test_dubbing_preparation import index_prepared, prepared
from test_release_batches import batch_env


def policy(mode='all', target='new', slot=None, **kwargs):
    return dict(mode=mode, target=target, slot=slot, line_settings='preserve',
                include_manual_sources=False, **kwargs)


def state(client, uid):
    response = client.get(f'/api/projects/{uid}/workbench?probe=false')
    assert response.status_code == 200, response.json()
    return response.json()


def seed_audio(client, uid):
    base = f'/api/projects/{uid}/workbench'
    for line_id in (1, 2):
        assert synthesize(client, f'{base}/lines/{line_id}/synthesize', json={}).status_code == 200
        assert client.post(f'{base}/lines/{line_id}/confirm', json={}).status_code == 200


def change_project(client, uid, **updates):
    response = client.put(f'/api/projects/{uid}/step_params', json=dict(updates=updates))
    assert response.status_code == 200, response.json()


def chain_plan(client, uid, dubbing=None, steps=None, **kwargs):
    body = dict(steps=steps or ['tts'], **kwargs)
    if dubbing is not None:
        body['dubbing'] = dubbing
    response = client.post(f'/api/projects/{uid}/run_plan', json=body)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert not result['blockers'], result['blockers']
    return result


def start_chain(client, uid, plan, dubbing=None):
    body = dict(steps=plan['steps'], plan_fingerprint=plan['fingerprint'])
    if dubbing is not None:
        body['dubbing'] = dubbing
    response = client.post(f'/api/projects/{uid}/run_chain', json=body)
    assert response.status_code == 200, response.json()
    return response.json()


def wait_chain(client, uid, expected=None):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = client.get(f'/api/projects/{uid}').json().get('chain')
        project = crud.get_project(uid)
        idle = all(runner.current_job_id(project.id, key) is None for key in ('tts', 'assemble'))
        if idle and (result is None if expected is None else result and result['state'] == expected):
            return result
        time.sleep(.01)
    raise TimeoutError(f'制作链未完成收尾：{result}')


def wait_workflow(client, uid, key):
    deadline = time.monotonic() + 15
    while key in workflows._running and time.monotonic() < deadline:
        time.sleep(.01)
    assert key not in workflows._running
    return workflows.get(key)


def start_workflow(client, uid, intent):
    base = f'/api/projects/{uid}/workflow'
    response = client.post(base + '/plan', json=intent)
    assert response.status_code == 200, response.json()
    plan = response.json()
    assert not plan['blockers'], plan['blockers']
    response = client.post(base + '/start', json=dict(recipe=plan['recipe'],
        fingerprint=plan['fingerprint'], confirmed=True, request_id=str(uuid.uuid4())))
    assert response.status_code == 200, response.json()
    return response.json(), plan


def test_default_chain_reuses_changed_audio_and_keeps_confirmation(index_prepared):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    before = [dubbing_edit.take_path(crud.project_dir(uid), i, 1).read_bytes() for i in (1, 2)]
    change_project(client, uid, **{'tts.indextts25.duration_factor': 1.4})
    plan = chain_plan(client, uid)
    assert plan['dubbing']['generate'] == 0
    assert plan['dubbing']['reuse'] == 2 and plan['dubbing']['settings_changed'] == 2
    start_chain(client, uid, plan)
    wait_chain(client, uid)
    assert len(received) == 2
    assert [dubbing_edit.take_path(crud.project_dir(uid), i, 1).read_bytes() for i in (1, 2)] == before
    assert all(row['confirmed'] and row['selection']['take'] == 1 for row in state(client, uid)['lines'])


def test_changed_chain_generates_common_new_slot_without_covering_old_audio(index_prepared):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    old_versions = [dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), i, 1)) for i in (1, 2)]
    change_project(client, uid, **{'tts.indextts25.duration_factor': 1.4})
    intent = policy('changed')
    plan = chain_plan(client, uid, intent)
    assert plan['dubbing']['generate'] == 2 and plan['dubbing']['new_slot'] == 2
    assert plan['dubbing']['overwrite'] == 0
    start_chain(client, uid, plan, intent)
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    result = state(client, uid)
    assert [r['selection']['take'] for r in result['lines']] == [2, 2]
    assert [r['duration_factor'] for r in received] == [1.1, 1.1, 1.4, 1.4]
    assert [dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), i, 1)) for i in (1, 2)] == old_versions
    assert all(next(v for v in r['versions'] if v['id'] == 1)['confirmed'] for r in result['lines'])


@pytest.mark.parametrize('source,take', [('original', 1), ('dub', None)])
def test_all_chain_protects_manual_original_and_silent_sources(index_prepared, source, take):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/selection', json=dict(source=source, take=take)).status_code == 200
    intent = policy()
    plan = chain_plan(client, uid, intent)
    assert plan['dubbing']['generate'] == 1
    assert plan['dubbing']['original' if source == 'original' else 'silent'] == 1
    start_chain(client, uid, plan, intent)
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    rows = state(client, uid)['lines']
    assert rows[0]['selection']['source'] == source and rows[0]['selection']['take'] == take
    assert rows[1]['selection']['take'] == 2 and len(received) == 3
    assert dubbing_edit.take_ids(crud.project_dir(uid), 1) == [1]


def test_explicit_include_manual_sources_fills_requested_slot(index_prepared):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/selection', json=dict(source='original', take=1)).status_code == 200
    assert client.put(base + '/lines/2/selection', json=dict(source='dub', take=None)).status_code == 200
    intent = {**policy(target='slot', slot=7), 'include_manual_sources': True}
    plan = chain_plan(client, uid, intent)
    assert plan['dubbing']['generate'] == 2 and plan['dubbing']['overwrite'] == 0
    start_chain(client, uid, plan, intent)
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    rows = state(client, uid)['lines']
    assert all(r['selection']['source'] == 'dub' and r['selection']['take'] == 7 for r in rows)
    assert all(dubbing_edit.take_ids(crud.project_dir(uid), i) == [1, 7] for i in (1, 2))
    assert len(received) == 4


def test_line_fine_tuning_preserved_or_temporarily_ignored_by_run(index_prepared):
    client, uid, received = index_prepared
    base = f'/api/projects/{uid}/workbench'
    key = 'tts.indextts25.duration_factor'
    assert client.put(base + '/lines/1/params', json={'updates': {key: .8}}).status_code == 200
    seed_audio(client, uid)
    change_project(client, uid, **{key: 1.4})
    saved = copy.deepcopy(crud.project_params(crud.get_project(uid))['tts_lines'])
    preserve = policy()
    start_chain(client, uid, chain_plan(client, uid, preserve), preserve)
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    assert [r['duration_factor'] for r in received[-2:]] == [.8, 1.4]
    project_only = {**policy(), 'line_settings': 'project'}
    start_chain(client, uid, chain_plan(client, uid, project_only), project_only)
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    assert [r['duration_factor'] for r in received[-2:]] == [1.4, 1.4]
    assert crud.project_params(crud.get_project(uid))['tts_lines'] == saved
    assert client.get(base + '/lines/1/params').json()['steps']['tts']['values'][key] == .8
    assert [r['selection']['take'] for r in state(client, uid)['lines']] == [3, 3]


def test_changed_current_slot_only_overwrites_affected_sentence(index_prepared):
    client, uid, received = index_prepared
    base = f'/api/projects/{uid}/workbench'
    key = 'tts.indextts25.duration_factor'
    assert client.put(base + '/lines/1/params', json={'updates': {key: .8}}).status_code == 200
    seed_audio(client, uid)
    old = [dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), i, 1)) for i in (1, 2)]
    change_project(client, uid, **{key: 1.4})
    intent = policy('changed', target='current')
    candidate = chain_plan(client, uid, intent)
    assert candidate['dubbing']['generate'] == 1 and candidate['dubbing']['reuse'] == 1
    assert candidate['dubbing']['overwrite'] == 1 and candidate['dubbing']['confirmed'] == 1
    start_chain(client, uid, candidate, intent)
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    assert [r['duration_factor'] for r in received] == [.8, 1.1, 1.4]
    assert dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), 1, 1)) == old[0]
    assert dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), 2, 1)) != old[1]
    assert [r['confirmed'] for r in state(client, uid)['lines']] == [True, False]


def test_confirmed_chain_plan_rejects_settings_change_before_start(index_prepared):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    intent = policy('changed')
    plan = chain_plan(client, uid, intent)
    change_project(client, uid, **{'tts.indextts25.duration_factor': 1.4})
    response = client.post(f'/api/projects/{uid}/run_chain', json=dict(steps=plan['steps'],
        dubbing=intent, plan_fingerprint=plan['fingerprint']))
    assert response.status_code == 400
    assert response.json()['code'] == 'CHAIN_PLAN_CHANGED'
    assert len(received) == 2 and crud.project_params(crud.get_project(uid)).get('_chain') is None


def test_regeneration_preflight_checks_new_reference_even_with_playable_old_audio(index_prepared):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    missing = str(crud.project_dir(uid) / 'missing-reference.wav')
    change_project(client, uid, **{'tts.indextts25.timbre_audio': missing})
    response = client.post(f'/api/projects/{uid}/run_plan', json=dict(steps=['tts'], dubbing=policy()))
    assert response.status_code == 200, response.json()
    plan = response.json()
    assert plan['blockers'], plan
    assert any(item['name'] == missing for item in plan['blockers'])
    response = client.post(f'/api/projects/{uid}/run_chain', json=dict(steps=plan['steps'],
        dubbing=policy(), plan_fingerprint=plan['fingerprint']))
    assert response.status_code == 400 and response.json()['code'] == 'DEPS_NOT_MET'
    assert len(received) == 2


def test_project_only_regeneration_preflight_ignores_inactive_line_reference(index_prepared):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    missing = str(crud.project_dir(uid) / 'missing-line-reference.wav')
    response = client.put(f'/api/projects/{uid}/workbench/lines/1/params',
        json=dict(updates={'tts.indextts25.timbre_audio': missing}))
    assert response.status_code == 200, response.json()
    preserve = client.post(f'/api/projects/{uid}/run_plan', json=dict(steps=['tts'], dubbing=policy())).json()
    assert preserve['blockers'], preserve
    intent = {**policy(), 'line_settings': 'project'}
    candidate = chain_plan(client, uid, intent)
    assert candidate['dubbing']['generate'] == 2
    start_chain(client, uid, candidate, intent)
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    assert len(received) == 4
    assert crud.project_params(crud.get_project(uid))['tts_lines']['1']['settings']['tts.indextts25.timbre_audio'] == missing


def test_project_space_run_many_carries_per_project_destination(index_prepared):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    intent = policy(target='slot', slot=9)
    plan = chain_plan(client, uid, intent)
    response = client.post('/api/projects/run_many', json=dict(uids=[uid],
        steps_by_uid={uid: plan['steps']}, dubbing_by_uid={uid: intent},
        plan_fingerprints_by_uid={uid: plan['fingerprint']}))
    assert response.status_code == 200, response.json()
    assert response.json()['results'][0].get('started'), response.json()
    wait_status(uid, 'tts', {'done'})
    wait_chain(client, uid)
    assert len(received) == 4
    assert [r['selection']['take'] for r in state(client, uid)['lines']] == [9, 9]
    assert all(dubbing_edit.take_ids(crud.project_dir(uid), i) == [1, 9] for i in (1, 2))


@pytest.mark.parametrize('patch', [dict(mode='unexpected'), dict(target='slot', slot=0),
    dict(target='slot', slot=True), dict(line_settings='delete'), dict(include_manual_sources='true')])
def test_formal_preflight_rejects_invalid_dubbing_intent(index_prepared, patch):
    client, uid, received = index_prepared
    response = client.post(f'/api/projects/{uid}/run_plan', json=dict(steps=['tts'], dubbing={**policy(), **patch}))
    assert response.status_code == 400 and response.json()['code'] == 'DUB_POLICY_INVALID'
    assert not received


@pytest.fixture
def complete_production(index_prepared, stub_heavy_steps, monkeypatch):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    mark_upstream_done(uid, 'assemble')
    stub_heavy_steps('assemble')
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    from dubforge.publishing import service
    def forbidden(*args, **kwargs):
        pytest.fail('制作模式不得触碰投稿或准备发布资料')
    monkeypatch.setattr(service, 'enqueue', forbidden)
    monkeypatch.setattr(workflows, '_prepare_publication', forbidden)
    return client, uid, received


def test_overview_workflow_uses_new_slot_and_reaches_assembly(complete_production):
    client, uid, received = complete_production
    intent = dict(mode='produce', production_steps=['tts', 'assemble'], dubbing=policy())
    row, candidate = start_workflow(client, uid, intent)
    assert candidate['recipe']['dubbing'] == policy()
    assert candidate['dubbing']['new_slot'] == 2
    result = wait_workflow(client, uid, row['id'])
    assert result['status'] == 'completed', result
    assert result['cursor'] == len(result['plan']['steps']) and not result['publication_id']
    assert crud.latest_job(crud.get_project(uid).id, 'assemble').status == 'done'
    assert [r['selection']['take'] for r in state(client, uid)['lines']] == [2, 2]
    assert len(received) == 4


def test_workflow_fill_needs_no_new_reference_or_online_tts_for_valid_audio(index_prepared, stub_heavy_steps, monkeypatch):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    mark_upstream_done(uid, 'assemble')
    stub_heavy_steps('assemble')
    from dubforge import tts_service
    monkeypatch.setattr(tts_service._BACKENDS['indextts25'], 'is_online', lambda *args: False)
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda *args: '隔离测试已有工具')
    change_project(client, uid, **{'tts.indextts25.timbre_audio': str(crud.project_dir(uid) / 'missing-reference.wav')})
    intent = dict(mode='produce', production_steps=['tts', 'assemble'])
    row, candidate = start_workflow(client, uid, intent)
    assert candidate['dubbing']['generate'] == 0 and candidate['dubbing']['reuse'] == 2
    result = wait_workflow(client, uid, row['id'])
    assert result['status'] == 'completed', result
    assert len(received) == 2


def test_workflow_regeneration_checks_actual_line_references(complete_production):
    client, uid, received = complete_production
    missing = str(crud.project_dir(uid) / 'missing-line-reference.wav')
    response = client.put(f'/api/projects/{uid}/workbench/lines/1/params',
        json=dict(updates={'tts.indextts25.timbre_audio': missing}))
    assert response.status_code == 200
    base = f'/api/projects/{uid}/workflow'
    intent = dict(mode='produce', production_steps=['tts', 'assemble'], dubbing=policy())
    response = client.post(base + '/plan', json=intent)
    assert response.status_code == 200, response.json()
    assert response.json()['blockers'], response.json()
    project_only = {**intent, 'dubbing': {**policy(), 'line_settings': 'project'}}
    row, candidate = start_workflow(client, uid, project_only)
    assert candidate['dubbing']['generate'] == 2
    result = wait_workflow(client, uid, row['id'])
    assert result['status'] == 'completed', result
    assert len(received) == 4
    assert crud.project_params(crud.get_project(uid))['tts_lines']['1']['settings']['tts.indextts25.timbre_audio'] == missing


def test_workflow_preserve_uses_valid_line_references_when_project_reference_is_missing(index_prepared, stub_heavy_steps, monkeypatch):
    client, uid, received = index_prepared
    seed_audio(client, uid)
    mark_upstream_done(uid, 'assemble')
    stub_heavy_steps('assemble')
    from dubforge import tts_service
    monkeypatch.setattr(tts_service._BACKENDS['indextts25'], 'is_online', lambda *args: True)
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda *args: '隔离测试已有工具')
    path = 'tts.indextts25.timbre_audio'
    reference = str(crud.project_dir(uid) / 'reference.wav')
    for line_id in (1, 2):
        response = client.put(f'/api/projects/{uid}/workbench/lines/{line_id}/params',
            json=dict(updates={path: reference}))
        assert response.status_code == 200, response.json()
    missing = str(crud.project_dir(uid) / 'missing-project-reference.wav')
    change_project(client, uid, **{path: missing})
    intent = dict(mode='produce', production_steps=['tts', 'assemble'], dubbing=policy())
    row, candidate = start_workflow(client, uid, intent)
    assert candidate['dubbing']['generate'] == 2
    result = wait_workflow(client, uid, row['id'])
    assert result['status'] == 'completed', result
    assert len(received) == 4
    response = client.post(f'/api/projects/{uid}/workflow/plan', json={**intent,
        'dubbing': {**policy(), 'line_settings': 'project'}})
    assert response.status_code == 200, response.json()
    assert response.json()['blockers'], response.json()


def test_expanded_production_chain_keeps_confirmed_fingerprint(complete_production):
    client, uid, received = complete_production
    intent = policy()
    candidate = chain_plan(client, uid, intent, steps=['assemble'], include_dependencies=True)
    assert 'tts' in candidate['steps']
    start_chain(client, uid, candidate, intent)
    wait_status(uid, 'assemble', {'done'})
    wait_chain(client, uid)
    assert len(received) == 4 and [r['selection']['take'] for r in state(client, uid)['lines']] == [2, 2]


def test_interrupted_workflow_resumes_same_slot_and_reuses_successes(complete_production, monkeypatch):
    client, uid, received = complete_production
    from dubforge.backends import tts_indextts25
    synth = tts_indextts25.synth
    failed = False
    def interrupted(url, text, out, lang, **kwargs):
        nonlocal failed
        if text == '世界' and not failed:
            failed = True
            raise DubForgeError('隔离测试模拟服务中断', code='TTS_REQUEST_TIMEOUT')
        return synth(url, text, out, lang, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', interrupted)
    intent = dict(mode='produce', production_steps=['tts', 'assemble'], dubbing=policy())
    row, _ = start_workflow(client, uid, intent)
    result = wait_workflow(client, uid, row['id'])
    assert result['status'] == 'failed', result
    assert not crud.latest_job(crud.get_project(uid).id, 'assemble')
    old_success = dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), 1, 2))
    response = client.post(f'/api/projects/{uid}/workflow/plan', json=dict(mode='produce', resume_id=row['id']))
    assert response.status_code == 200, response.json()
    plan = response.json()
    assert plan['recipe']['dubbing'] == policy()
    assert plan['dubbing']['resumed'] and plan['dubbing']['completed'] == 1
    assert plan['dubbing']['new_slot'] == 2 and plan['dubbing']['generate'] == 1
    response = client.post(f'/api/projects/{uid}/workflow/{row["id"]}/resume', json=dict(
        recipe=plan['recipe'], fingerprint=plan['fingerprint'], confirmed=True,
        recovery_fingerprint=plan.get('recovery_fingerprint'), request_id=str(uuid.uuid4())))
    assert response.status_code == 200, response.json()
    result = wait_workflow(client, uid, row['id'])
    assert result['status'] == 'completed', result
    assert dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), 1, 2)) == old_success
    assert len(received) == 4
    assert [r['selection']['take'] for r in state(client, uid)['lines']] == [2, 2]
    assert all(dubbing_edit.take_ids(crud.project_dir(uid), i) == [1, 2] for i in (1, 2))


@pytest.mark.parametrize('legacy_repair_flag', [False, True])
def test_interrupted_legacy_chain_recovers_same_slot_and_continues_assembly(complete_production, monkeypatch, legacy_repair_flag):
    client, uid, received = complete_production
    from dubforge.backends import tts_indextts25
    synth = tts_indextts25.synth
    failed = False
    def interrupted(url, text, out, lang, **kwargs):
        nonlocal failed
        if text == '世界' and not failed:
            failed = True
            raise DubForgeError('隔离测试模拟服务中断', code='TTS_REQUEST_TIMEOUT')
        return synth(url, text, out, lang, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', interrupted)
    intent = policy()
    candidate = chain_plan(client, uid, intent, steps=['tts', 'assemble'])
    start_chain(client, uid, candidate, intent)
    held = wait_chain(client, uid, 'held')
    assert held['remaining'] == ['tts', 'assemble']
    old_success = dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), 1, 2))
    project = crud.get_project(uid)
    chain_params = crud.project_params(project)
    operation = chain_params['_chain_dubbing']['operation_id']
    if legacy_repair_flag:
        # 模拟旧版已中断任务与制作链保留的显式运行策略，确认恢复时须使用项目设置。
        chain_params['_chain_dubbing']['policy']['repair_timing'] = False
        project.params = json.dumps(chain_params)
        crud.save_project(project)
        previous_job = crud.latest_job(project.id, 'tts')
        previous_task = json.loads(previous_job.task_data)
        previous_task['policy']['repair_timing'] = False
        previous_job.task_data = json.dumps(previous_task)
        crud.update_job(previous_job)
    response = client.post(f'/api/projects/{uid}/chain/plan', json={})
    assert response.status_code == 200, response.json()
    plan = response.json()
    assert not plan['blockers'], plan
    assert plan['dubbing']['policy'] == policy()
    assert plan['dubbing']['resumed'] and plan['dubbing']['completed'] == 1
    assert plan['dubbing']['new_slot'] == 2 and plan['dubbing']['generate'] == 1
    response = client.post(f'/api/projects/{uid}/chain/resume', json=dict(
        fingerprint=plan['fingerprint'], confirmed=True, request_id=str(uuid.uuid4())))
    assert response.status_code == 200, response.json()
    wait_status(uid, 'assemble', {'done'})
    wait_chain(client, uid)
    assert json.loads(crud.latest_job(project.id, 'tts').task_data)['operation_id'] == operation
    assert dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), 1, 2)) == old_success
    assert len(received) == 4
    assert [r['selection']['take'] for r in state(client, uid)['lines']] == [2, 2]
    assert all(dubbing_edit.take_ids(crud.project_dir(uid), i) == [1, 2] for i in (1, 2))


def test_configuration_batch_plan_and_start_preserve_execution_policy(complete_production):
    client, uid, received = complete_production
    response = client.post('/api/configuration/batch-plan', json=dict(uids=[uid], mode='produce', dubbing=policy(target='slot', slot=6)))
    assert response.status_code == 200, response.json()
    item = response.json()['items'][0]
    assert not item['blockers'], item
    assert item['plan']['recipe']['dubbing']['slot'] == 6
    response = client.post('/api/configuration/batch-start', json=dict(confirmed=True, request_id=str(uuid.uuid4()),
        items=[dict(uid=uid, recipe=item['plan']['recipe'], fingerprint=item['plan']['fingerprint'])]))
    assert response.status_code == 200, response.json()
    result = response.json()['items'][0]
    assert result['error'] == '', result
    run = wait_workflow(client, uid, result['run_id'])
    assert run['status'] == 'completed', run
    assert [r['selection']['take'] for r in state(client, uid)['lines']] == [6, 6]
    assert len(received) == 4


def test_ordered_release_batch_persists_policy_for_every_recipe_and_resume(batch_env):
    from app.main import app
    from fastapi.testclient import TestClient
    from test_release_batches import SCOPE, finish
    client = TestClient(app)
    intent = policy('changed', target='slot', slot=8)
    body = dict(project_uids=batch_env['uids'], scope=SCOPE, order_confirmed=True, dubbing=intent)
    response = client.post('/api/release-batches/plan', json=body)
    assert response.status_code == 200, response.json()
    plan = response.json()
    assert not plan['blockers'], plan
    assert all(item['recipe']['dubbing'] == intent for item in plan['items'])
    response = client.post('/api/release-batches', json=dict(body,
        fingerprint=plan['fingerprint'], confirmed=True, request_id=str(uuid.uuid4())))
    assert response.status_code == 200, response.json()
    row = response.json()
    assert not batch_env['started']
    response = client.post(f'/api/release-batches/{row["id"]}/plan', json={})
    assert response.status_code == 200, response.json()
    restored = response.json()
    assert all(item['recipe']['dubbing'] == intent for item in restored['items'])
    response = client.post(f'/api/release-batches/{row["id"]}/start', json=dict(
        fingerprint=restored['fingerprint'], confirmed=True, rights_confirmed=True,
        request_id=str(uuid.uuid4())))
    assert response.status_code == 200, response.json()
    completed = finish(row['id'])
    assert completed['status'] == 'done', completed
    assert batch_env['started'] == batch_env['uids']
    for item in completed['items']:
        assert workflows.get(item['workflow_id'])['recipe']['dubbing'] == intent
