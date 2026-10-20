"""协调器过程诊断与执行身份回归；不访问真实模型或平台。"""
import copy
import json
import time
import uuid

from sqlmodel import Session

from dubforge import workflows
from dubforge.domain import crud
from dubforge.db import get_engine
from dubforge.pipeline import assembly_versions as versions
from dubforge.publishing import service
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun
from test_workflows import flow, start, finish
from test_publishing import setup_publish


def test_changed_future_version_uses_actual_successful_spec_for_publishing(flow, monkeypatch):
    uid, spec, calls, _ = flow
    spec['mode'] = 'draft'
    before = workflows._check_configuration
    changed = []
    def change_before_assemble(run, steps=None):
        before(run, steps)
        if steps and 'assemble' in steps and not changed:
            catalog = versions.catalog(uid)
            rows = copy.deepcopy(catalog['versions'])
            rows[0]['overrides']['assemble.crf'] = 26
            versions.save(uid, {'revision': catalog['revision'], 'versions': rows})
            changed.append(rows[0]['id'])
    monkeypatch.setattr(workflows, '_check_configuration', change_before_assemble)
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'ready', result.get('error')
    actual = result['plan']['assembled_versions'][0]
    assert result['plan']['version_specs'][0]['overrides'].get('assemble.crf') != 26
    assert actual['version']['overrides']['assemble.crf'] == 26
    assert actual['version']['values']['assemble.crf'] == 26
    pub = service.view(result['publication_id'])
    assert pub['draft']['parts'][0]['artifact_id'] == actual['artifact_id']
    record = versions.results(uid)[actual['version']['id']]
    assert record.artifact_hash == actual['artifact_sha256']
    assert not any(operation in {'submit', 'upload'} for operation, _ in calls)


def test_ai_and_cover_microstages_are_persisted_without_copy_text_or_paths(flow, monkeypatch):
    from dubforge.publishing import copywriting
    uid, spec, _, _ = flow
    spec.update(mode='draft', ai_copy=True, cover_source='platform')
    monkeypatch.setattr(service, 'platform_cover', lambda _: {'cover_id': spec['cover_id']})
    conversation_owners = []
    def generate(project, current, instruction, *, cancel_check, progress, conversation_owner_id):
        conversation_owners.append(conversation_owner_id)
        progress.phase('publish.copy.evidence', '收集发布文案资料')
        progress.llm_trace('request', '机密提示词', request_id='1', retry_attempt=1, retry_total=2)
        progress.llm_trace('reasoning_delta', '不公开的思考正文')
        progress.llm_trace('assistant_delta', '不公开的生成正文')
        progress.llm_trace('validation_error', '不公开的校验输出', retry_attempt=1, retry_total=2)
        progress.llm_trace('request', '第二次机密提示词', request_id='2', retry_attempt=2, retry_total=2)
        progress.llm_trace('validation_success', '完整正文', retry_attempt=2, retry_total=2)
        cancel_check()
        return {'values': dict(current, dynamic='测试动态')}
    monkeypatch.setattr(copywriting, 'generate', generate)
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'ready', result
    assert conversation_owners == [result['publication_id']]
    assert result['publication_id'] == 'workflow-' + row['id']
    execution = result['execution']
    assert execution['schema'] == 1 and execution['state'] == 'completed'
    phases = {item['phase'] for item in execution['history']}
    assert {'llm.waiting', 'llm.reasoning', 'llm.output', 'llm.retry', 'llm.validating',
            'publish.cover.fetch', 'publish.draft.save'} <= phases
    text = json.dumps(execution, ensure_ascii=False)
    assert all(secret not in text for secret in ('机密提示词', '不公开的思考正文', '不公开的生成正文', str(crud.project_dir(uid))))
    assert len(execution['history']) <= 24


def test_monitoring_execution_uses_workflow_deadline_but_preserves_chapter_deadline(setup_publish):
    from dubforge.execution_progress import Recorder
    uid, publication, _, _ = setup_publish
    future = time.time() + 120
    with Session(get_engine()) as session:
        pub = session.get(Publication, publication['id'])
        pub.status = 'submitted'
        session.add(pub)
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), publication_id=pub.id,
                          status='monitoring', next_check=future)
        session.add(row); session.commit(); session.refresh(row)
        key = row.id
    assert workflows.get(key)['execution']['next_check_at'] == future
    assert workflows.status(uid)['run']['execution']['next_check_at'] == future
    chapter_deadline = future + 300
    chapter = Recorder(owner='chapters', run_id=publication['id']).phase('chapters.pending', '等待章节就绪',
        state='waiting', next_check_at=chapter_deadline)
    with Session(get_engine()) as session:
        pub = session.get(Publication, publication['id'])
        pub.status = 'published'
        pub.snapshot = service._json(dict(settings=dict(service.DEFAULTS), origins={}, parts=[],
            chapter_options=dict(enabled=True, items=[dict(title='开场', start_time=0, end_time=10)])))
        pub.chapter_sync = service._json(dict(schema=1, status='pending', execution=chapter))
        session.add(pub); session.commit()
    assert workflows.get(key)['execution']['next_check_at'] == chapter_deadline
    assert workflows.status(uid)['run']['execution']['next_check_at'] == chapter_deadline
