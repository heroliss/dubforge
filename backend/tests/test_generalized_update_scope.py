"""运行范围与真实原稿更新预检的集成；平台替身、不启动模型或真实投稿。"""
import json
import uuid
from types import SimpleNamespace

import pytest

from dubforge import workflow_plan, workflows
from dubforge.pipeline import assembly_versions, dubbing_policy
from dubforge.publishing import service
from test_configuration import save
from test_publication_updates import env, finish as finish_update
from test_publishing import setup_publish
from test_workflows import finish as finish_workflow


@pytest.fixture
def metadata_target(env, monkeypatch):
    definitions = [dict(id='source-v', name='新原声', dubbing=False, burn_subtitles=False,
                        include_comments=False, overrides={}),
                   dict(id='dub-v', name='新配音', dubbing=True, burn_subtitles=False,
                        include_comments=False, overrides={})]
    save('project:' + env['uid'], {'assembly.versions': definitions,
        'publish.version_ids': [v['id'] for v in definitions],
        'publish.part_titles': {'source-v': '原声修订版', 'dub-v': '配音修订版'},
        'publish.title': '修订后的标题', 'publish.description': '修订后的简介',
        'publish.tags': ['修订'], 'publish.chapters_enabled': True})
    records = {v['id']: SimpleNamespace(version_id=v['id'], artifact_id=a.id, artifact_hash=a.sha256,
        snapshot=json.dumps({**v, 'values': {'assemble.subtitle_track': 'bilingual'}}))
               for v, a in zip(definitions, env['artifacts'])}
    monkeypatch.setattr(assembly_versions, 'results', lambda uid: records)
    monkeypatch.setattr(assembly_versions, 'selected', lambda *a, **kw: pytest.fail('资料更新不能解析制作版本'))
    monkeypatch.setattr(assembly_versions, 'resolve', lambda *a, **kw: pytest.fail('资料更新不能解析视频制作参数'))
    monkeypatch.setattr(dubbing_policy, 'preview', lambda *a, **kw: pytest.fail('资料更新不能检查配音'))

    def production(deps, steps, **kw):
        assert steps == []
        return []

    monkeypatch.setattr(workflow_plan, 'production_checks', production)
    return env


def intent(target, fields):
    return dict(mode='update', production_steps=[], update=dict(
        publication_id=target['source']['id'], replace_videos=False, sync_fields=fields))


def test_explicit_description_scope_ignores_unselected_project_changes(metadata_target):
    target = metadata_target
    candidate = workflows.preview(target['uid'], intent(target, ['description']))
    assert candidate['steps'] == [] and 'version_specs' not in candidate
    assert not candidate['dubbing']['needed'] and not candidate['blockers'], candidate['blockers']
    plan = candidate['update_plan']
    assert plan['values'] == {'description': '修订后的简介'}
    assert plan['chapters']['enabled'] is False and plan['collection']['enabled'] is False
    assert not target['writes'] and not target['uploads']


def test_explicit_empty_scope_never_implicitly_applies_configured_fields(metadata_target):
    candidate = workflows.preview(metadata_target['uid'], intent(metadata_target, []))
    plan = candidate['update_plan']
    assert plan['values'] == {} and plan['chapters']['enabled'] is False
    assert plan['collection']['enabled'] is False
    assert candidate['steps'] == [] and not metadata_target['writes']


def test_part_titles_and_order_use_metadata_evidence_without_rendering(metadata_target):
    target = metadata_target
    candidate = workflows.preview(target['uid'], intent(target, ['part_titles', 'part_order']))
    assert candidate['steps'] == [] and 'version_specs' not in candidate
    assert not candidate['blockers'], candidate['blockers']
    assert candidate['update_plan']['values'] == {}
    assert all(not part.get('replaced') for part in candidate['update_plan']['parts'])
    assert [p['title'] for p in candidate['update_plan']['parts']] == ['原声修订版', '配音修订版']


def test_part_metadata_mapping_does_not_enable_video_replacement_or_production(metadata_target):
    target = metadata_target
    body = intent(target, ['part_titles'])
    body['update']['replacements'] = [dict(old_cid=part['cid'], version_id=version)
        for part, version in zip(target['remote']['videos'], ['source-v', 'dub-v'])]
    candidate = workflows.preview(target['uid'], body)
    assert not candidate['blockers'], candidate['blockers']
    assert candidate['steps'] == [] and 'version_specs' not in candidate
    assert all(not part.get('replaced') for part in candidate['update_plan']['parts'])
    assert candidate['update_plan']['part_titles'] == {'1000': '原声修订版', '1001': '配音修订版'}


def test_scope_display_labels_do_not_change_authorized_business_fingerprint(metadata_target, monkeypatch):
    from dubforge import workflow_update
    body = intent(metadata_target, ['description'])
    before = workflows.preview(metadata_target['uid'], body)
    original = workflow_update.prepare
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        result['scope_options'][0]['label'] = '仅改变展示名称'
        return result
    monkeypatch.setattr(workflow_update, 'prepare', prepare)
    after = workflows.preview(metadata_target['uid'], body)
    assert before['fingerprint'] == after['fingerprint']
    assert before['update_plan']['scope_options'] != after['update_plan']['scope_options']


def test_metadata_update_without_explicit_production_scope_defaults_to_no_jobs(metadata_target):
    body = intent(metadata_target, ['description'])
    body.pop('production_steps')
    candidate = workflows.preview(metadata_target['uid'], body)
    assert candidate['recipe']['production_steps'] == [] and candidate['steps'] == []
    assert not candidate['blockers'], candidate['blockers']


def test_description_only_workflow_updates_original_once_without_video_upload(metadata_target, monkeypatch):
    target = metadata_target
    from dubforge.runner import runner
    monkeypatch.setattr(runner, 'start_step', lambda *a, **kw: pytest.fail('资料更新不应执行制作步骤'))
    monkeypatch.setattr(runner, 'start_versions', lambda *a, **kw: pytest.fail('资料更新不应合成'))
    monkeypatch.setattr(service, 'create', lambda *a, **kw: pytest.fail('更新原稿不能创建新稿'))
    candidate = workflows.preview(target['uid'], intent(target, ['description']))
    assert not candidate['blockers'], candidate['blockers']
    request = dict(recipe=candidate['recipe'], confirmed=True, request_id=str(uuid.uuid4()),
                   fingerprint=candidate['fingerprint'])
    result = finish_workflow(workflows.start(target['uid'], request)['id'])
    assert result['update_id'], {'status': result['status'], 'phase': result['phase'], 'error': result['error']}
    task = finish_update(result['update_id'])
    assert task['status'] == 'completed', task
    workflows._update(result['id'], next_check=0)
    workflows.monitor_once()
    result = workflows.get(result['id'])
    assert result['status'] == 'completed' and result['publication_id'] == target['source']['id']
    assert not target['uploads'] and len(target['writes']) == 1
    assert target['writes'][0]['values'] == {'description': '修订后的简介'}
    assert workflows.start(target['uid'], request)['id'] == result['id']
    assert len(target['writes']) == 1
