"""自动流程文案补齐回归；隔离配置与项目，模型、制作及发布通信均为替身。"""
import copy
import uuid

import pytest

from dubforge import configuration, workflow_plan, workflows
from dubforge.backends import llm
from dubforge.publishing import copywriting, service
from test_configuration import bind, save
from test_publishing import setup_publish, upload
from test_workflows import configure, finish, flow, start


MANUAL_COPY = dict(title='我保存的项目标题', description='我核对过的简介',
                   tags=['本片标签'], dynamic='我保存的动态')


def _reject_generation(*args, **kwargs):
    pytest.fail('已经明确设置的文案不应再次请求模型')


def _start_saved(uid):
    preview = workflows.preview(uid, {'mode': 'draft'})
    assert not preview['blockers'], preview['blockers']
    row = workflows.start(uid, dict(confirmed=True, request_id=str(uuid.uuid4()),
                                   fingerprint=preview['fingerprint']))
    result = finish(row['id'])
    assert result['status'] == 'ready', result
    return preview, service.view(result['publication_id'])


@pytest.mark.parametrize('scope', ['project', 'global', 'series'])
def test_all_explicit_copy_skips_ai_and_reuses_saved_project_or_template(flow, monkeypatch, scope):
    uid, spec, calls, _ = flow
    spec.update(ai_copy=True, mode='draft')
    spec['overrides'] = {key: value for key, value in spec['overrides'].items()
                         if key not in copywriting.COPY_FIELDS}
    configure(uid, spec)
    save('project:' + uid, reset=['publish.' + field for field in copywriting.COPY_FIELDS])
    if scope == 'series':
        template = configuration.create_template('文案继承测试')
        bind([uid], template)
        key = template['key']
    else:
        key = 'project:' + uid if scope == 'project' else 'global'
    # AI 候选应用后的保存也走同一组稀疏 publish.* 覆盖键。
    save(key, {'publish.' + field: value for field, value in MANUAL_COPY.items()})
    monkeypatch.setattr(copywriting, 'generate', _reject_generation)
    preview, pub = _start_saved(uid)
    assert {field: preview['settings'][field] for field in MANUAL_COPY} == MANUAL_COPY
    assert {field: pub['settings'][field] for field in MANUAL_COPY} == MANUAL_COPY
    assert all(preview['origins'][field] != '默认' for field in MANUAL_COPY)
    assert not any('生成失败' in warning for warning in preview['warnings'])
    assert not any(operation in {'upload', 'submit'} for operation, _ in calls)


def test_partial_copy_only_fills_unconfigured_fields_and_keeps_explicit_empty(flow, monkeypatch):
    uid, spec, calls, _ = flow
    spec.update(ai_copy=True, mode='draft')
    spec['overrides']['description'] = ''
    generated_calls = []

    def generate(project, current, instruction):
        generated_calls.append(copy.deepcopy(current))
        return {'values': dict(title='不应覆盖标题', description='不应覆盖空简介',
                               tags=['不应覆盖标签'], dynamic='自动补充动态')}

    monkeypatch.setattr(copywriting, 'generate', generate)
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'ready', result
    pub = service.view(result['publication_id'])
    assert pub['settings']['title'] == spec['overrides']['title']
    assert pub['settings']['tags'] == spec['overrides']['tags']
    assert pub['settings']['description'] == ''
    assert pub['settings']['dynamic'] == '自动补充动态'
    assert len(generated_calls) == 1 and generated_calls[0]['description'] == ''
    assert not any(operation in {'upload', 'submit'} for operation, _ in calls)


def test_all_explicit_copy_can_include_empty_optional_fields(flow, monkeypatch):
    uid, spec, _, _ = flow
    spec.update(ai_copy=True, mode='draft')
    spec['overrides'].update(description='', dynamic='')
    monkeypatch.setattr(copywriting, 'generate', _reject_generation)
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'ready', result
    pub = service.view(result['publication_id'])
    assert pub['settings']['description'] == '' and pub['settings']['dynamic'] == ''


@pytest.mark.parametrize('missing_field', [None, 'dynamic'])
def test_preflight_only_requires_copy_model_when_a_field_is_unconfigured(setup_publish, monkeypatch, missing_field):
    uid, pub, _, _ = setup_publish
    manual = {**pub['draft']['overrides'], **MANUAL_COPY}
    if missing_field:
        manual.pop(missing_field)
    lookups = []
    monkeypatch.setattr(llm, '_profiles', lambda selected='': lookups.append(selected) or [])
    preview = workflow_plan.plan(uid, dict(mode='draft', variants=['src'], ai_copy=True,
        account_id='123', cover_source='custom', cover_id=pub['draft']['cover_id'],
        overrides=manual), online=False)
    assert bool(lookups) is bool(missing_field)
    assert any('LLM' in blocker for blocker in preview['blockers']) is bool(missing_field)


def test_explicit_empty_tags_remain_a_preflight_error_without_copy_model(setup_publish, monkeypatch):
    uid, pub, _, _ = setup_publish
    monkeypatch.setattr(llm, '_profiles', _reject_generation)
    preview = workflow_plan.plan(uid, dict(mode='draft', variants=['src'], ai_copy=True,
        account_id='123', cover_source='custom', cover_id=pub['draft']['cover_id'],
        overrides={**pub['draft']['overrides'], **MANUAL_COPY, 'tags': []}), online=False)
    assert any('请填写标签' in blocker for blocker in preview['blockers'])
    assert preview['settings']['tags'] == []


def test_manual_copy_does_not_bypass_models_needed_by_production(setup_publish, monkeypatch):
    uid, pub, _, _ = setup_publish
    lookups = []
    monkeypatch.setattr(llm, '_profiles', lambda selected='': lookups.append(selected) or [])
    preview = workflow_plan.plan(uid, dict(mode='draft', variants=['dub_hardsub'], ai_copy=True,
        account_id='123', cover_source='custom', cover_id=pub['draft']['cover_id'],
        overrides={**pub['draft']['overrides'], **MANUAL_COPY}), online=False)
    assert 'split' in preview['steps'] and 'translate' in preview['steps']
    assert lookups and any('LLM' in blocker for blocker in preview['blockers'])


def test_project_copy_saved_for_next_workflow_does_not_change_existing_snapshot(flow, monkeypatch):
    uid, spec, calls, _ = flow
    existing = service.list_publications(uid)[0]
    frozen = upload(existing)
    assert frozen['status'] == 'uploaded'
    snapshot = copy.deepcopy(frozen['snapshot'])
    spec.update(ai_copy=True, mode='draft')
    configure(uid, spec)
    save('project:' + uid, {'publish.' + field: value for field, value in MANUAL_COPY.items()})
    monkeypatch.setattr(copywriting, 'generate', _reject_generation)
    _, new_pub = _start_saved(uid)
    assert new_pub['id'] != frozen['id']
    assert {field: new_pub['settings'][field] for field in MANUAL_COPY} == MANUAL_COPY
    assert service.view(frozen['id'])['snapshot'] == snapshot
    assert service.view(frozen['id'])['settings'] == snapshot['settings']
    assert not any(operation == 'submit' for operation, _ in calls)
