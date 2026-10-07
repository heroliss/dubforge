"""隔离项目、伪模型/平台；实际穿过协调器与发布状态机，绝不真实投稿。"""
import json
import threading
import time
import uuid

import pytest
from sqlmodel import Session

from dubforge import config, workflows, workflow_plan
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import service, transport
from dubforge.workflow_models import WorkflowRun
from test_publishing import setup_publish
from test_configuration import save


def configure(uid, spec):
    """测试从各步骤保存设置；预检仅携带本次执行意图。"""
    from dubforge.pipeline.assembly_outputs import dimensions, VARIANT_LABELS
    fields = {'mode':'run.mode', 'account_id':'publish.account_id', 'ai_copy':'publish.ai_copy',
              'cover_source':'publish.cover_source', 'cover_id':'publish.cover_id', 'part_titles':'publish.part_titles',
              'comments_mode':'comments.mode', 'comment_limit':'comments.limit', 'comment_count':'comments.count', 'comment_prompt':'comments.prompt',
              'comment_failure_policy':'comments.failure_policy'}
    values = {path:spec[key] for key,path in fields.items() if key in spec}
    values.update({'publish.'+k:v for k,v in spec.get('overrides', {}).items()})
    values.update({'comments.'+k:v for k,v in spec.get('comment_settings', {}).items()})
    if spec.get('version_ids') is not None:
        values['publish.version_ids'] = spec['version_ids']
    else:
        values['assembly.versions'] = [dict(id=v, name=VARIANT_LABELS.get(v,'原片'), overrides={}, **dimensions(v)) for v in spec['variants']]
        values['publish.version_ids'] = spec['variants']
    save('project:'+uid, values)


def finish(key):
    deadline = time.monotonic() + 10
    while key in workflows._running and time.monotonic() < deadline:
        time.sleep(.01)
    assert key not in workflows._running
    return workflows.get(key)


@pytest.fixture()
def flow(setup_publish, monkeypatch):
    uid, pub, artifacts, calls = setup_publish
    spec = workflow_plan.recipe(dict(mode='publish', variants=['dub_hardsub', 'src_hardsub'],
        ai_copy=False, account_id='123', cover_source='custom', cover_id=pub['draft']['cover_id'],
        overrides=pub['draft']['overrides']))
    configure(uid, spec)
    from conftest import mark_upstream_done
    mark_upstream_done(uid, 'assemble')
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    from dubforge.runner import runner
    sequence = []
    def step(uid, key):
        sequence.append(key)
        row = crud.get_step(crud.get_project(uid).id, key)
        row.status = 'done'
        crud.save_step(row)
        return {'status': 'skipped'}
    monkeypatch.setattr(runner, 'start_step', step)
    def assemble(uid, body, *, frozen):
        from types import SimpleNamespace
        from dubforge.pipeline import assembly_versions as av
        from dubforge.pipeline.assembly_outputs import final_variant
        from dubforge.pipeline.dependencies import Dependencies
        project = crud.get_project(uid)
        sequence.append(frozen['versions'])
        for version in frozen['versions']:
            artifact = next((a for a in artifacts if final_variant(a.name) == version['id']), None)
            if artifact:
                av.record_result(SimpleNamespace(project=project), version, artifact.name, av.fingerprint(Dependencies(project), version))
        return step(uid, 'assemble')
    monkeypatch.setattr(runner, 'start_versions', assemble)
    yield uid, spec, calls, sequence
    for key in list(workflows._running):
        finish(key)
    assert not workflows._leases


def start(uid, spec):
    configure(uid, spec)
    preview = workflows.preview(uid, spec)
    assert not preview['blockers'], preview['blockers']
    body = dict(recipe=preview['recipe'], confirmed=True, request_id=str(uuid.uuid4()), fingerprint=preview['fingerprint'])
    return workflows.start(uid, body), body


def test_new_project_without_llm_is_blocked_before_any_job(make_project):
    uid = make_project()
    result = workflow_plan.plan(uid, dict(mode='produce', variants=['dub'], ai_copy=False), online=False)
    assert any('LLM' in item for item in result['blockers'])
    assert not crud.latest_job(crud.get_project(uid).id, 'download')


@pytest.mark.parametrize('patch', [dict(variants=[]), dict(variants=['dub', 'dub']), dict(ai_copy='true'),
    dict(comment_limit=10, comment_count=11), dict(part_titles={'dub': ''}), dict(mode='upload'), dict(unknown=True),
    dict(mode=[]), dict(variants=[{}]), dict(cover_source={}), dict(comment_settings=[])])
def test_invalid_recipe(patch):
    with pytest.raises(DubForgeError):
        workflow_plan.recipe(patch)


def test_defaults_inherit_without_rights_gate(make_project):
    first, second = make_project(), make_project()
    from dubforge import configuration
    save('global', {'run.mode':'publish','publish.account_id':'123'})
    assert workflows.view(second)['origin'] == '全局默认'
    assert 'rights_confirmed' not in workflows.view(second)['recipe']
    save('project:'+second, {'publish.account_id':'456'})
    save('global', {'publish.account_id':'789'})
    assert workflows.view(second)['recipe']['account_id'] == '456'
    with pytest.raises(DubForgeError, match='其他页面'):
        configuration.save('global', dict(revision=0, updates={'publish.account_id':'123'}))


def test_parts_order_collection_and_idempotency(flow):
    uid, spec, calls, sequence = flow
    spec['part_titles'] = dict(dub_hardsub='中文配音', src_hardsub='原声字幕')
    row, body = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'monitoring', result
    assert sequence[0] == 'download'
    submit = next(payload for op, payload in calls if op == 'submit')
    assert [p['title'] for p in submit['videos']] == ['中文配音', '原声字幕']
    assert submit['settings']['season_id'] == 1
    assert sum(op == 'collection' for op, _ in calls) == 1
    assert workflows.start(uid, body)['id'] == row['id']
    workflows.monitor_once()
    assert workflows.get(row['id'])['status'] == 'completed'
    assert sum(op == 'submit' for op, _ in calls) == 1
    assert any('已有自动流程发布记录' in item for item in workflows.preview(uid, spec)['blockers'])
    with pytest.raises(DubForgeError, match='重复稿件'):
        workflows.start(uid, {**body, 'request_id':str(uuid.uuid4())})


def test_old_plan_without_tts_is_blocked_and_resume_repairs_it(flow, monkeypatch):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline.dubbing_edit import record_take
    from dubforge.runner import runner
    from dubforge.utils.fs import atomic_write_bytes
    uid, spec, calls, sequence = flow
    project = crud.get_project(uid)
    deps = Dependencies(project)
    path = deps.path('tts', '1.wav'); content = path.read_bytes(); path.unlink()
    old_tts = crud.get_step(project.id, 'tts'); old_tts.status = 'failed'; crud.save_step(old_tts)
    original_plan = workflows._plan
    def broken_plan(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        result['steps'] = [s for s in result['steps'] if s != 'tts']
        return result
    monkeypatch.setattr(workflows, '_plan', broken_plan)
    row, _ = start(uid, spec)
    failed = finish(row['id'])
    assert failed['status'] == 'failed' and '待补 1 句' in failed['error']
    assert failed['cursor'] == 1 and not failed['publication_id']
    assert not any(isinstance(s, list) or s == 'assemble' for s in sequence)
    assert all(op == 'metadata' for op, _ in calls)
    monkeypatch.setattr(workflows, '_plan', original_plan)
    original_step = runner.start_step
    def fill(uid, key):
        if key == 'tts':
            atomic_write_bytes(path, content)
            crud.add_artifact(project.id, 'tts', 'audio', path.name, path, str(path.relative_to(deps.root)))
            record_take(project, deps.dubbing_lines()[0], deps.root, 1, 'fixture', {})
        return original_step(uid, key)
    monkeypatch.setattr(runner, 'start_step', fill)
    candidate = workflows.preview(uid, spec)
    assert candidate['dubbing']['pending'] == 1 and 'tts' in candidate['steps']
    # 输入文件的就绪变化不改设置指纹；必须用新增依赖修正旧游标。
    assert candidate['fingerprint'] == failed['plan']['fingerprint']
    workflows.resume(row['id'], dict(confirmed=True, recipe=spec, fingerprint=candidate['fingerprint']))
    result = finish(row['id'])
    assert result['status'] == 'monitoring', result
    assert sequence.index('tts') < sequence.index('assemble')
    assert sum(op == 'submit' for op, _ in calls) == 1


def test_failed_dubbing_stops_full_workflow_before_assembly_and_publication(flow, monkeypatch):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import runner
    uid, spec, calls, sequence = flow
    project = crud.get_project(uid)
    Dependencies(project).path('tts', '1.wav').unlink()
    original = runner.start_step
    def fail(uid, key):
        if key == 'tts':
            sequence.append(key)
            step = crud.get_step(project.id, key); step.status = 'failed'; crud.save_step(step)
            return {'status': 'started'}
        return original(uid, key)
    monkeypatch.setattr(runner, 'start_step', fail)
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'failed' and not result['publication_id']
    assert 'tts' in sequence and 'assemble' not in sequence
    assert not any(isinstance(s, list) for s in sequence)
    assert all(op == 'metadata' for op, _ in calls)


def test_publication_preparation_rechecks_dubbing_after_assembly(flow, monkeypatch):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import runner
    uid, spec, calls, sequence = flow
    path = Dependencies(crud.get_project(uid)).path('tts', '1.wav')
    original = runner.start_versions
    def assemble(*args, **kwargs):
        result = original(*args, **kwargs)
        path.unlink()
        return result
    monkeypatch.setattr(runner, 'start_versions', assemble)
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert 'assemble' in sequence
    assert result['status'] == 'failed' and '待补 1 句' in result['error']
    assert not result['publication_id'] and all(op == 'metadata' for op, _ in calls)


def test_manual_fields_win_over_ai_and_draft_never_uploads(flow, monkeypatch):
    from dubforge.publishing import copywriting
    uid, spec, calls, _ = flow
    spec.update(ai_copy=True, mode='draft')
    monkeypatch.setattr(copywriting, 'generate', lambda *args: {'values': dict(title='不应替换', description='自动简介', tags=['不应替换'], dynamic='自动动态')})
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'ready', result
    pub = service.view(result['publication_id'])
    assert pub['settings']['title'] == spec['overrides']['title']
    assert pub['settings']['tags'] == spec['overrides']['tags']
    assert pub['settings']['description'] == '自动简介'
    assert not any(op in {'upload', 'submit', 'cover', 'collection'} for op, _ in calls)


def test_produce_does_not_create_publication(flow):
    uid, spec, calls, _ = flow
    spec['mode'] = 'produce'
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'completed'
    assert not result['publication_id']
    assert calls == []


@pytest.mark.parametrize('ai_copy', [False, True])
def test_automatic_draft_keeps_source_publication_date(flow, monkeypatch, ai_copy):
    from dubforge.publishing import copywriting
    uid, spec, calls, _ = flow
    spec.update(ai_copy=ai_copy, mode='draft')
    monkeypatch.setattr(service.video_info, 'view', lambda uid: {'info': {
        'source': {'release_date': '2026-08-01', 'upload_date': '2026-07-30'}, 'summary_zh': '视频概述'}})
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *args, **kwargs: {
        'title': '自动标题', 'description': '自动简介', 'tags': ['旅行'], 'dynamic': ''})
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'ready', result
    description = service.view(result['publication_id'])['settings']['description']
    assert description.count('原视频发布时间：2026-08-01') == 1
    assert '2026-07-30' not in description
    assert not any(op in {'upload', 'submit', 'cover', 'collection'} for op, _ in calls)


@pytest.mark.parametrize('confirmed', [False, None, 1, 'true'])
def test_explicit_confirmation(flow, confirmed):
    uid, spec, calls, _ = flow
    candidate = workflows.preview(uid, spec)
    with pytest.raises(DubForgeError, match='确认'):
        workflows.start(uid, dict(recipe=spec, confirmed=confirmed, fingerprint=candidate['fingerprint'], request_id=str(uuid.uuid4())))
    assert not any(op == 'upload' for op, _ in calls)


def test_stale_preflight(flow):
    uid, spec, calls, _ = flow
    candidate = workflows.preview(uid, spec)
    config.set_key('assemble.bgm_volume', .27)
    with pytest.raises(DubForgeError, match='重新预检'):
        workflows.start(uid, dict(recipe=spec, confirmed=True, fingerprint=candidate['fingerprint'], request_id=str(uuid.uuid4())))
    assert not any(op == 'upload' for op, _ in calls)


def test_boundary_check_narrows_to_remaining_steps(flow):
    """边界按「剩余步骤消费的键」对比: 已完成步骤的键变化不打断;
    版本呈现参数已随版本规格冻结, 也不在边界指纹内 (ADR-0075)。
    直接对只读 preview 计划检查, 不启动运行, 避免与后台引擎竞态。"""
    uid, spec, calls, _ = flow
    plan = workflows.preview(uid, spec)
    assert plan['steps'][0] == 'download'
    assert 'configuration_snapshot' in plan
    run = {'project_uid': uid, 'plan': plan}
    config.set_key('download.resolution', 480)          # download 在剩余集合: 触发
    with pytest.raises(DubForgeError, match='设置'):
        workflows._check_configuration(run, ['download'])
    config.set_key('download.resolution', 1080)         # 还原后不再触发
    workflows._check_configuration(run, ['download'])
    config.set_key('assemble.bgm_volume', .5)
    workflows._check_configuration(run, ['assemble'])   # 版本模式: assemble 键随版本规格冻结
    workflows._check_configuration(run, [])             # 发布准备只校验来源身份


def test_post_timeout_never_replayed(flow, monkeypatch):
    uid, spec, calls, _ = flow
    original = transport.call
    def timeout(op, payload=None, **kwargs):
        if op == 'submit':
            calls.append((op, payload))
            raise service.PublishError('投稿响应超时')
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', timeout)
    row, body = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'failed'
    assert service.view(result['publication_id'])['status'] == 'unknown'
    workflows.recover()
    with pytest.raises(DubForgeError, match='核对'):
        workflows.resume(row['id'], dict(confirmed=True))
    with pytest.raises(DubForgeError, match='重复稿件'):
        workflows.start(uid, {**body, 'request_id':str(uuid.uuid4())})
    assert sum(op == 'submit' for op, _ in calls) == 1


def test_restart_pauses_without_post(make_project, monkeypatch):
    uid = make_project()
    with Session(get_engine()) as session:
        for status in ('queued', 'running', 'stopping'):
            session.add(WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status=status))
        session.commit()
    monkeypatch.setattr(service, 'enqueue', lambda *args: pytest.fail('恢复不能投稿'))
    workflows.recover()
    assert all(row['status'] == 'paused' for row in workflows.view(uid)['runs'])


def test_runtime_edit_of_finished_step_is_rejected_without_interrupting_the_workflow(flow, monkeypatch):
    """已完成步骤的设置也锁定；拒绝编辑不改变快照，制作结束后才可保存。"""
    from dubforge.runner import runner
    from dubforge import configuration, runtime_edit
    from app.api.projects import StepParamsUpdate, update_project_step_params
    uid, spec, calls, _ = flow
    waiting, release = threading.Event(), threading.Event()
    original = runner.start_versions
    def wait(uid, body, *, frozen):
        waiting.set()
        assert release.wait(10)
        return original(uid, body, frozen=frozen)
    monkeypatch.setattr(runner, 'start_versions', wait)
    row, _ = start(uid, spec)
    try:
        assert waiting.wait(5)
        assert crud.get_step(crud.get_project(uid).id, 'download').status == 'done'
        before = configuration.resolve(uid)
        params_before = crud.get_project(uid).params
        snapshot = workflows.get(row['id'])['plan']['configuration_snapshot']
        with pytest.raises(runtime_edit.RuntimeEditError) as error:
            update_project_step_params(uid, StepParamsUpdate(updates={'download.resolution': 480}))
        assert error.value.code == 'CONFIGURATION_BUSY'
        assert configuration.resolve(uid) == before
        assert crud.get_project(uid).params == params_before
        active = workflows.get(row['id'])
        assert active['status'] == 'running' and not active['error']
        assert active['plan']['configuration_snapshot'] == snapshot
    finally:
        release.set()
    result = finish(row['id'])
    assert result['status'] == 'monitoring', result
    assert result['plan']['configuration_snapshot'] == snapshot
    assert sum(op == 'submit' for op, _ in calls) == 1
    # 远程审核等待没有本地制作写入，允许设置下一次重跑的值。
    assert not runtime_edit.editability('project:' + uid)['locked']
    update_project_step_params(uid, StepParamsUpdate(updates={'download.resolution': 480}))
    assert configuration.resolve(uid)['values']['download.resolution'] == 480
    assert workflows.get(row['id'])['plan']['configuration_snapshot'] == snapshot


def test_lease_still_blocks_manual_step_start(flow):
    """流程租约同时禁止外部编辑和插入手动单步。"""
    uid, spec, calls, _ = flow
    row, _ = start(uid, spec)
    with pytest.raises(DubForgeError, match='全流程'):
        workflows.check_idle(uid)
    assert finish(row['id'])['status'] == 'monitoring'


def test_collection_attention_never_reposts(flow):
    uid, spec, calls, _ = flow
    row, _ = start(uid, spec)
    result = finish(row['id'])
    service._update(result['publication_id'], status='collection_pending', error='合集未完成')
    workflows.monitor_once()
    assert workflows.get(row['id'])['status'] == 'attention'
    assert sum(op == 'submit' for op, _ in calls) == 1


def test_auto_comments_scores_translation_and_timeline(flow, monkeypatch):
    uid, spec, _, _ = flow
    spec.update(comments_mode='auto', comment_count=3)
    calls, saved = [], []
    monkeypatch.setattr(workflows.video_info, 'update', lambda *a, **k: calls.append('collect'))
    monkeypatch.setattr(workflows.comment_studio, 'run', lambda uid, action, **k: calls.append(action))
    rows = [dict(id=str(i), source_hash=str(i), text='original', text_zh='译文') for i in range(3)]
    monkeypatch.setattr(workflows.comment_studio, 'view', lambda uid: dict(rows=rows, filter={'items': [{'id': '2'}, {'id': '1'}, {'id': '0'}]}, overlay_revision='rev'))
    monkeypatch.setattr(workflows.comment_studio, 'save', lambda uid, body: saved.append(body))
    monkeypatch.setattr(service, '_probe', lambda path: 27)
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), recipe=json.dumps(spec), plan='{"warnings":[]}')
        session.add(row); session.commit(); session.refresh(row); key = row.id
    workflows._comments(workflows.get(key), threading.Event())
    assert calls == ['collect', 'filter', 'translate']
    assert [item['id'] for item in saved[0]['items']] == ['2', '1']
    assert all(item['display_text'] == '译文' for item in saved[0]['items'])
    assert workflows.get(key)['plan']['warnings']


def test_resume_keeps_original_steps_for_saved_cursor(flow, monkeypatch):
    from dubforge.runner import runner
    uid, spec, _, sequence = flow
    spec['mode'] = 'produce'
    original_plan, original_step = workflows._plan, runner.start_step
    phase = ['first']
    def plan(*args, **kwargs):
        value = original_plan(*args, **kwargs)
        value['steps'] = ['download', 'tts', 'assemble'] if phase[0] == 'first' else ['download', 'assemble']
        value['labels']['tts'] = '合成配音'
        return value
    def pause(uid, key):
        result = original_step(uid, key)
        if phase[0] == 'first':
            workflows.stop(workflows._leases[uid])
        return result
    monkeypatch.setattr(workflows, '_plan', plan)
    monkeypatch.setattr(runner, 'start_step', pause)
    monkeypatch.setattr(workflows.Dependencies, 'fresh', lambda self, key: True)
    row, _ = start(uid, spec)
    result = finish(row['id'])
    assert result['status'] == 'paused' and result['cursor'] == 1
    phase[0] = 'resume'
    candidate = workflows.preview(uid, spec)
    workflows.resume(row['id'], dict(confirmed=True, recipe=spec, fingerprint=candidate['fingerprint']))
    assert finish(row['id'])['status'] == 'completed'
    assert sequence[:2] == ['download', 'tts']
    assert sequence.count('download') == 1


def test_same_start_id_rejects_different_recipe(flow):
    uid, spec, _, _ = flow
    spec['mode'] = 'produce'
    row, body = start(uid, spec)
    finish(row['id'])
    with pytest.raises(DubForgeError, match='同一启动标识'):
        workflows.start(uid, {**body, 'recipe': {**spec, 'variants': ['src']}})


def test_mixed_comment_dimensions_do_not_apply_comments_to_all_parts(flow, monkeypatch):
    uid, spec, _, sequence = flow
    spec.update(mode='produce', variants=['dub_hardsub', 'src_hardsub_comments'])
    monkeypatch.setattr(workflows.comment_studio, 'view', lambda uid: {'overlay_revision': 'fixed'})
    monkeypatch.setattr(workflows.comment_studio, 'confirmed', lambda *a: {'settings':workflows.comment_studio.DEFAULT_SETTINGS,'items':[]})
    row, _ = start(uid, spec)
    assert finish(row['id'])['status'] == 'completed'
    dimensions = next(s for s in sequence if isinstance(s, list))
    assert [s['include_comments'] for s in dimensions] == [False, True]


@pytest.mark.parametrize('field,empty', [('title', ''), ('tags', [])])
def test_explicit_empty_required_copy_is_not_silently_filled(flow, field, empty):
    uid, spec, _, _ = flow
    spec['ai_copy'] = True
    spec['overrides'][field] = empty
    configure(uid, spec)
    assert workflows.preview(uid, spec)['blockers']


def test_relative_import_source_and_changed_local_file(make_project, monkeypatch):
    uid = make_project()
    project = crud.get_project(uid)
    project.source_kind, project.source_url = 'local', 'media/import.mp4'
    crud.save_project(project)
    path = crud.project_dir(uid) / project.source_url
    path.write_bytes(b'fixture')
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *a, **k: [])
    first = workflow_plan.plan(uid, dict(mode='produce', variants=['src']), online=False)
    assert not first['blockers']
    path.write_bytes(b'changed fixture')
    second = workflow_plan.plan(uid, dict(mode='produce', variants=['src']), online=False)
    assert first['fingerprint'] != second['fingerprint']


def test_empty_auto_comments_stops_without_saving_or_posting(flow, monkeypatch):
    uid, spec, calls, _ = flow
    monkeypatch.setattr(workflows.video_info, 'update', lambda *a, **k: None)
    monkeypatch.setattr(workflows.comment_studio, 'run', lambda *a, **k: None)
    monkeypatch.setattr(workflows.comment_studio, 'view', lambda *a: {'filter': {'items': []}, 'rows': []})
    monkeypatch.setattr(workflows.comment_studio, 'save', lambda *a: pytest.fail('零条评论不能默默省略'))
    with pytest.raises(DubForgeError, match='没有可上屏'):
        workflows._comments({'project_uid': uid, 'recipe': spec}, threading.Event())
    assert not calls


@pytest.mark.parametrize('mode,expected', [('translated',['b','c']), ('bilingual',['b','c']), ('original',['a','b'])])
def test_auto_comments_translates_all_for_reading_but_only_uses_usable_ranked_results(flow, monkeypatch, mode, expected):
    uid, spec, calls, _ = flow
    spec['comment_count'] = 2
    spec['comment_failure_policy'] = 'skip'
    spec['comment_settings']['language_mode'] = mode
    ran, saved, warnings = [], [], []
    monkeypatch.setattr(workflows.video_info, 'update', lambda *a, **k:None)
    def process(uid, action, **kwargs):
        ran.append((action,kwargs))
        return {'job':{'warning':'有一条评论未完成'}}
    monkeypatch.setattr(workflows.comment_studio, 'run', process)
    rows = [dict(id=k, source_hash=k, text='original', text_zh='' if k=='a' else '完整译文') for k in 'abcd']
    monkeypatch.setattr(workflows.comment_studio, 'view', lambda uid: dict(rows=rows, overlay_revision='rev', filter={
        'items':[{'id':'a'},{'id':'b'}], 'assessments':[dict(id=k, eligible=k!='d', score=90-i) for i,k in enumerate('abcd')]}))
    monkeypatch.setattr(service, '_probe', lambda *a:1000)
    monkeypatch.setattr(workflows, '_update', lambda *a, **k:None)
    monkeypatch.setattr(workflows.comment_studio, 'save', lambda uid, body:saved.append(body))
    workflows._comments(dict(id='fixture',project_uid=uid,recipe=spec,plan={'warnings':warnings}),threading.Event())
    assert [action for action,_ in ran] == ['filter','translate']
    assert all(kwargs['failure_policy']=='skip' and 'ids' not in kwargs for _,kwargs in ran)
    assert [item['id'] for item in saved[0]['items']] == expected
    assert len(warnings) == (2 if mode == 'original' else 3) and not calls


@pytest.mark.parametrize('mode,version_mode,expected', [('translated', None, ['source_long','good']),
    ('original', None, ['zh_long','good']), ('bilingual', None, ['good','extra']),
    ('translated', 'bilingual', ['good','extra']), ('original', 'translated', ['good','extra'])])
def test_automatic_comments_exclude_long_candidates_and_backfill(flow, monkeypatch, mode, version_mode, expected):
    uid, spec, calls, _ = flow
    spec['comment_count'] = 2
    spec['comment_settings']['language_mode'] = mode
    saved, warnings = [], []
    monkeypatch.setattr(workflows.video_info, 'update', lambda *a, **k:None)
    monkeypatch.setattr(workflows.comment_studio, 'run', lambda *a, **k:{'job':{}})
    rows = [dict(id=k, source_hash=k, text='x' * (720 if k=='source_long' else 30),
                 text_zh='中' * (241 if k=='zh_long' else 20)) for k in ('source_long','zh_long','good','extra')]
    monkeypatch.setattr(workflows.comment_studio, 'view', lambda uid: dict(rows=rows, overlay_revision='rev',
        filter={'assessments':[dict(id=row['id'],eligible=True,score=99-i) for i,row in enumerate(rows)]}))
    monkeypatch.setattr(service, '_probe', lambda *a:1000)
    monkeypatch.setattr(workflows, '_update', lambda *a, **k:None)
    monkeypatch.setattr(workflows.comment_studio, 'save', lambda uid, body:saved.append(body))
    plan = {'warnings':warnings}
    if version_mode:
        plan['version_specs'] = [dict(include_comments=True,values={'comments.language_mode':version_mode})]
    workflows._comments(dict(id='fixture',project_uid=uid,recipe=spec,plan=plan),threading.Event())
    assert [row['id'] for row in saved[0]['items']] == expected
    assert any('已自动排除' in warning and '按评分递补' in warning for warning in warnings)
    assert len(rows[0]['text']) == 720 and len(rows[1]['text_zh']) == 241
    assert not calls  # 不上传、投稿或调用真实模型。


@pytest.mark.parametrize('new_dependency', [False, True])
def test_resume_repairs_deleted_prior_dependencies(flow, monkeypatch, new_dependency):
    from dubforge.pipeline.registry import STEP_MAP
    uid, spec, _, sequence = flow
    spec['mode'] = 'produce'
    candidate = workflows.preview(uid, spec)
    candidate.update(steps=['download', 'assemble'], labels={k: s.label for k, s in STEP_MAP.items()})
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='paused',
            cursor=1, recipe=json.dumps(candidate['recipe']), plan=json.dumps(candidate))
        session.add(row); session.commit(); session.refresh(row); key = row.id
    if new_dependency:
        candidate['steps'] = ['download', 'preprocess', 'assemble']
    monkeypatch.setattr(workflows, '_plan', lambda *a, **k: candidate)
    monkeypatch.setattr(workflows.Dependencies, 'fresh', lambda self, step: False)
    workflows.resume(key, dict(recipe=spec, fingerprint=candidate['fingerprint'], confirmed=True))
    assert finish(key)['status'] == 'completed'
    assert sequence[0] == 'download'
    assert ('preprocess' in sequence) is new_dependency


def test_workflow_api_read_save_and_confirmation(make_project):
    from fastapi.testclient import TestClient
    from app.main import app
    uid = make_project()
    client = TestClient(app)
    prefix = f'/api/projects/{uid}/workflow'
    response = client.get(prefix)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    spec = response.json()['recipe']
    response = client.put(prefix + '/recipe', json=dict(scope='project', revision=0, recipe=spec))
    assert response.status_code in {404,405}
    response = client.put(prefix + '/recipe', json=dict(scope={}, revision=1, recipe=spec))
    assert response.status_code in {404,405}
    response = client.post(prefix + '/start', json=dict(recipe=spec, request_id=str(uuid.uuid4())))
    assert response.status_code == 400 and '确认' in response.json()['message']
    assert not workflows.view(uid)['runs']


def test_bad_source_url_is_a_preflight_issue(make_project, monkeypatch):
    uid = make_project()
    project = crud.get_project(uid)
    project.source_url = 'http://[invalid'
    crud.save_project(project)
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *a, **k: [])
    result = workflow_plan.plan(uid, dict(mode='produce', variants=['src']), online=False)
    assert '请填写有效的视频链接' in result['blockers']


def test_lightweight_status_keeps_strategy_and_frozen_run_separate(flow, monkeypatch):
    from dubforge import configuration
    from dubforge.pipeline import assembly_versions
    uid, spec, calls, _ = flow
    row, _ = start(uid, spec)
    result = finish(row['id'])
    save('global', {'run.mode':'produce'})
    current = configuration.view('project:'+uid)
    configuration.save('project:'+uid, dict(revision=current['revision'],fingerprint=current['fingerprint'],reset=['run.mode']))
    # 看板读取不能扫描成品、连模型、连 B站，也不能暴露完整快照或凭据。
    monkeypatch.setattr(assembly_versions, 'catalog', lambda *a: pytest.fail('状态轮询不能扫描版本'))
    monkeypatch.setattr(service, 'view', lambda *a: pytest.fail('状态轮询不能读取完整稿件'))
    monkeypatch.setattr(transport, 'call', lambda *a, **kw: pytest.fail('状态轮询不能联网'))
    status = workflows.status(uid)
    assert status['mode'] == 'produce'
    assert status['mode_source']['kind'] == 'global'
    assert status['run']['mode'] == 'publish'
    assert status['run']['status'] == 'monitoring'
    assert status['run']['publication']['id'] == result['publication_id']
    assert status['existing_publication_id'] == result['publication_id']
    assert 'recipe' not in status['run'] and 'plan' not in status['run']


def test_status_before_start_and_api_are_read_only(make_project):
    from fastapi.testclient import TestClient
    from app.main import app
    uid = make_project()
    save('project:'+uid, {'run.mode':'publish'})
    response = TestClient(app).get(f'/api/projects/{uid}/workflow/status')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == dict(mode='publish',mode_source={'kind':'project'},run=None,existing_publication_id='')
    assert not workflows.view(uid)['runs']


@pytest.mark.parametrize('phase', ['queued','running','monitoring'])
def test_preflight_explains_existing_open_run_before_start(flow, phase):
    uid, spec, calls, _ = flow
    with Session(get_engine()) as session:
        session.add(WorkflowRun(project_uid=uid,request_id=str(uuid.uuid4()),status=phase))
        session.commit()
    result = workflows.preview(uid, spec)
    assert any('已有自动流程' in issue for issue in result['blockers'])
    assert not any(op in {'upload','submit'} for op, _ in calls)


def test_resume_preflight_checks_project_and_publication_boundary(flow, make_project):
    uid, spec, _, _ = flow
    for invalid in ([], {}, 1, '', 'x' * 101):
        with pytest.raises(DubForgeError, match='标识无效'):
            workflows._availability(uid, 'publish', invalid)
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid,request_id=str(uuid.uuid4()),status='paused')
        session.add(row);session.commit();session.refresh(row);key=row.id
    assert not workflows.preview(uid, {**spec,'resume_id':key})['blockers']
    with pytest.raises(DubForgeError, match='不属于'):
        workflows._availability(make_project(), 'publish', key)
    workflows._update(key,publication_id='existing')
    assert any('现有稿件' in issue for issue in workflows.preview(uid, {**spec,'resume_id':key})['blockers'])


def paused_recovery(flow, monkeypatch, *, steps, cursor, fresh_steps):
    """只登记隔离的暂停流程，用固定产物状态核对预检与入队，不执行任务。"""
    from dubforge.pipeline import recovery
    from dubforge.pipeline.registry import STEP_MAP
    uid, spec, _, _ = flow
    spec['mode'] = 'produce'
    configure(uid, spec)
    candidate = workflows._plan(uid, spec, online=False)
    candidate.update(steps=list(steps), labels={key: step.label for key, step in STEP_MAP.items()})
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='failed', cursor=cursor,
            recipe=json.dumps(candidate['recipe']), plan=json.dumps(candidate))
        session.add(row); session.commit(); session.refresh(row); key = row.id
    state = dict(fresh_steps)
    monkeypatch.setattr(recovery, 'fresh', lambda _deps, step: state.get(step, True))
    monkeypatch.setattr(workflows, '_plan', lambda *args, **kwargs: json.loads(json.dumps(candidate)))
    launched = []
    monkeypatch.setattr(workflows, '_launch', lambda key, uid: launched.append(workflows.get(key)))
    return uid, spec, key, candidate, state, launched


def recovery_body(preview, *, request_id=None):
    result = dict(confirmed=True, recipe=preview['recipe'], fingerprint=preview['fingerprint'],
                  recovery_fingerprint=preview['recovery_fingerprint'])
    if request_id is not None:
        result['request_id'] = request_id
    return result


def test_recovery_preview_and_resume_share_earlier_stale_start(flow, monkeypatch):
    uid, spec, key, candidate, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'split', 'translate', 'assemble'], cursor=2,
        fresh_steps={'download': True, 'split': False, 'translate': False, 'assemble': False})
    # 新计划不再含有效下载，恢复仍使用与原游标对应的步骤表。
    candidate['steps'] = ['split', 'translate', 'assemble']
    row = crud.get_step(crud.get_project(uid).id, 'split')
    row.status = 'stale'; crud.save_step(row)
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    assert preview['recovery']['previous_step'] == 'translate'
    assert preview['recovery']['start_step'] == 'split'
    assert preview['recovery']['cursor'] == 1
    assert 'download' in preview['recovery']['reusable_steps']
    result = workflows.resume(key, recovery_body(preview))
    assert result['cursor'] == 1 and len(launched) == 1
    assert result['plan']['steps'] == ['download', 'split', 'translate', 'assemble']
    assert result['plan']['recovery'] == preview['recovery']


def test_recovery_new_missing_dependency_replaces_old_step_table(flow, monkeypatch):
    uid, spec, key, candidate, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1,
        fresh_steps={'download': True, 'preprocess': False, 'assemble': False})
    candidate['steps'] = ['download', 'preprocess', 'assemble']
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    assert preview['recovery']['start_step'] == 'preprocess'
    assert preview['recovery']['cursor'] == 0
    result = workflows.resume(key, recovery_body(preview))
    assert result['cursor'] == 0 and len(launched) == 1
    assert result['plan']['steps'] == candidate['steps']
    assert result['plan']['recovery_fingerprint'] == preview['recovery_fingerprint']


def test_recovery_changed_configuration_replans_and_reuses_valid_outputs(flow, monkeypatch):
    uid, spec, key, candidate, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1,
        fresh_steps={'download': False, 'assemble': True})
    candidate['fingerprint'] = 'new-confirmed-configuration'
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    assert not preview['blockers']
    assert preview['recovery']['changed'] is True
    assert preview['recovery']['start_step'] == 'download'
    assert preview['recovery']['execute_steps'] == ['download']
    assert 'assemble' in preview['recovery']['reusable_steps']
    result = workflows.resume(key, recovery_body(preview))
    assert result['cursor'] == 0 and len(launched) == 1
    assert result['plan']['recovery'] == preview['recovery']


@pytest.mark.parametrize('change', ['missing_dependency', 'earlier_stale'])
def test_recovery_fingerprint_rejects_changed_scope_with_same_configuration(flow, monkeypatch, change):
    uid, spec, key, candidate, state, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'split', 'assemble'], cursor=2,
        fresh_steps={'download': True, 'split': True, 'assemble': False})
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    old_configuration = preview['fingerprint']
    if change == 'missing_dependency':
        candidate['steps'].insert(1, 'preprocess')
        state['preprocess'] = False
    else:
        state['split'] = False
    refreshed = workflows.preview(uid, {**spec, 'resume_id': key})
    assert refreshed['fingerprint'] == old_configuration
    assert refreshed['recovery_fingerprint'] != preview['recovery_fingerprint']
    with pytest.raises(DubForgeError) as error:
        workflows.resume(key, recovery_body(preview))
    assert error.value.code == 'WORKFLOW_PLAN_CHANGED'
    assert not launched and workflows.get(key)['status'] == 'failed'
    assert not workflows.get(key)['plan'].get('_resume_requests')


def test_recovery_guard_rechecks_scope_inside_start_locks(flow, monkeypatch):
    uid, spec, key, candidate, state, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'split', 'assemble'], cursor=2,
        fresh_steps={'download': True, 'split': True, 'assemble': False})
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    def changing_plan(*args, online=True, **kwargs):
        if not online:
            state['split'] = False
        return json.loads(json.dumps(candidate))
    monkeypatch.setattr(workflows, '_plan', changing_plan)
    with pytest.raises(DubForgeError) as error:
        workflows.resume(key, recovery_body(preview))
    assert error.value.code == 'WORKFLOW_PLAN_CHANGED'
    assert not launched and workflows.get(key)['cursor'] == 2


def test_recovery_guard_rechecks_new_blockers_inside_start_locks(flow, monkeypatch):
    uid, spec, key, candidate, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1, fresh_steps={'download': True, 'assemble': False})
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    def blocked_plan(*args, online=True, **kwargs):
        result = json.loads(json.dumps(candidate))
        if not online:
            result['blockers'] = ['本地原视频不存在']
        return result
    monkeypatch.setattr(workflows, '_plan', blocked_plan)
    with pytest.raises(DubForgeError) as error:
        workflows.resume(key, recovery_body(preview))
    assert error.value.code == 'WORKFLOW_PLAN_CHANGED'
    assert '本地原视频不存在' in error.value.message
    assert not launched and workflows.get(key)['cursor'] == 1


def test_recovery_request_replay_after_failure_is_persistently_idempotent(flow, monkeypatch):
    uid, spec, key, _, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1, fresh_steps={'download': True, 'assemble': False})
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    body = recovery_body(preview, request_id=str(uuid.uuid4()))
    accepted = workflows.resume(key, body)
    assert len(launched) == 1
    assert workflows.resume(key, body)['status'] == 'queued'
    workflows._update(key, status='failed', error='新的失败')
    # 请求已被接受的记录不会因为新计划或再次失败而丢失；重送直接返回已有结果。
    monkeypatch.setattr(workflows, '_plan', lambda *args, **kwargs: pytest.fail('重送不应重新预检'))
    replayed = workflows.resume(key, body)
    assert replayed['status'] == 'failed' and len(launched) == 1
    record = accepted['plan']['_resume_requests'][body['request_id']]
    assert record['accepted_state']['status'] == 'failed'
    assert record['accepted_state']['cursor'] == 1
    with pytest.raises(DubForgeError) as error:
        workflows.resume(key, {**body, 'fingerprint': 'different-plan'})
    assert error.value.code == 'WORKFLOW_CONFLICT'
    assert len(launched) == 1


def test_recovery_preserves_all_request_receipts_across_new_attempts(flow, monkeypatch):
    uid, spec, key, _, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1, fresh_steps={'download': True, 'assemble': False})
    first = recovery_body(workflows.preview(uid, {**spec, 'resume_id': key}), request_id=str(uuid.uuid4()))
    workflows.resume(key, first)
    workflows._update(key, status='failed')
    second = recovery_body(workflows.preview(uid, {**spec, 'resume_id': key}), request_id=str(uuid.uuid4()))
    workflows.resume(key, second)
    workflows._update(key, status='failed')
    result = workflows.resume(key, first)
    assert len(launched) == 2
    assert set(result['plan']['_resume_requests']) == {first['request_id'], second['request_id']}
    assert '_resume_requests' not in workflows.preview(uid, {**spec, 'resume_id': key})


@pytest.mark.parametrize('request_id', [None, '', 'invalid', 'x' * 36, 1, {}])
def test_recovery_rejects_invalid_provided_request_id(flow, monkeypatch, request_id):
    uid, spec, key, _, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1, fresh_steps={'download': True, 'assemble': False})
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    with pytest.raises(DubForgeError, match='请求标识无效'):
        workflows.resume(key, {**recovery_body(preview), 'request_id': request_id})
    assert not launched


@pytest.mark.parametrize('publication_status', ['draft', 'unknown', 'published'])
def test_recovery_publication_boundary_never_restarts_production(flow, setup_publish, monkeypatch, publication_status):
    uid, spec, key, _, _, launched = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1, fresh_steps={'download': True, 'assemble': False})
    pub = setup_publish[1]
    service._update(pub['id'], status=publication_status,
        receipt={'bvid': 'BV1234567890'} if publication_status == 'published' else {})
    workflows._update(key, publication_id=pub['id'])
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    assert any('现有稿件' in item for item in preview['blockers'])
    assert 'recovery_fingerprint' not in preview
    body = dict(confirmed=True, request_id=str(uuid.uuid4()))
    if publication_status == 'published':
        assert workflows.resume(key, body)['status'] == 'monitoring'
        assert workflows.resume(key, body)['status'] == 'monitoring'
    else:
        with pytest.raises(DubForgeError, match='核对'):
            workflows.resume(key, body)
    assert not launched
    assert not any(op in {'upload', 'submit'} for op, _payload in setup_publish[3])
