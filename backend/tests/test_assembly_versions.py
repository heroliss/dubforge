"""版本继承、局部重生成和真实 FFmpeg；不访问用户文件或B站。"""
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import config, workflow_plan
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as av
from dubforge.configuration_models import ConfigurationScope
from dubforge import configuration
from dubforge.errors import DubForgeError
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from conftest import wait_status
from test_assemble import _prepare_outputs_project


def definition(key='chinese', **overrides):
    return dict(id=key, name=key, dubbing=False, burn_subtitles=True, include_comments=False, overrides=overrides)


def wait(uid):
    wait_status(uid, 'assemble', {'done'}, timeout=30)
    until = time.monotonic() + 10
    while runner.current_job_id(crud.get_project(uid).id, 'assemble') is not None:
        assert time.monotonic() < until
        time.sleep(.01)


def test_explicit_equal_value_is_fixed_and_identity_does_not_affect_fingerprint(make_project):
    config.set_key('assemble.subtitle_track', 'trans')
    uid = make_project()
    fixed = definition('fixed', **{'assemble.subtitle_track': 'trans'})
    follow = definition('follow')
    deps = Dependencies(crud.get_project(uid))
    a, b = av.resolve(deps, fixed), av.resolve(deps, follow)
    assert a['origins']['assemble.subtitle_track'] == '本版本'
    assert b['origins']['assemble.subtitle_track'] == '全局默认'
    before = av.fingerprint(deps, a)
    config.set_key('assemble.subtitle_track', 'bilingual')
    deps = Dependencies(crud.get_project(uid))
    assert av.resolve(deps, follow)['values']['assemble.subtitle_track'] == 'trans'
    from test_configuration import save
    save('project:' + uid, {'assemble.subtitle_track': 'bilingual'})
    deps = Dependencies(crud.get_project(uid))
    assert av.resolve(deps, follow)['values']['assemble.subtitle_track'] == 'bilingual'
    assert av.fingerprint(deps, av.resolve(deps, fixed)) == before
    fixed['name'] = '新的分P标题'
    assert av.fingerprint(deps, av.resolve(deps, fixed)) == before
    fixed['overrides']['assemble.loudnorm'] = False  # 原声版不消费配音参数。
    assert av.fingerprint(deps, av.resolve(deps, fixed)) == before


@pytest.mark.parametrize('override', [{'tts.backend':'other'}, {'assemble.subtitle_track':None},
    {'assemble.loudnorm':'false'}, {'assemble.crf':99}, {'comments.background_opacity':-1}, {'assemble.subtitle_color':'red'}])
def test_invalid_version_values_are_rejected(override):
    from dubforge.errors import DubForgeError
    with pytest.raises(DubForgeError): av.validate(definition(**override))


def test_catalog_read_is_non_mutating_and_save_is_revisioned(make_project):
    uid=make_project();project=crud.get_project(uid)
    av.view(uid)
    with crud.session() as session: assert 'assembly.versions' not in configuration.resolve(uid)['overrides']
    values=[definition('a',**{'comments.background_opacity':0}),definition('b')]
    saved=av.save(uid,dict(revision=0,versions=values))
    assert saved['revision']==1 and saved['versions'][0]['overrides']['comments.background_opacity']==0
    assert crud.get_project(uid).params==project.params
    with pytest.raises(DubForgeError,match='其他页面'): av.save(uid,dict(revision=0,versions=values))


def test_global_versions_only_change_new_projects_until_explicitly_applied(make_project):
    from test_configuration import save
    old=make_project()
    original=av.catalog(old)['versions']
    values=[definition('a'),definition('b',**{'assemble.subtitle_track':'bilingual'})]
    save('global',{'assembly.versions':values})
    new=make_project()
    assert [v['id'] for v in av.catalog(new)['versions']]==['a','b']
    assert av.catalog(old)['versions']==original
    save('global',{'assembly.versions':[definition('c')]})
    assert [v['id'] for v in av.catalog(new)['versions']]==['a','b']
    plan=configuration.binding_plan([old,new],'')
    configuration.bind(dict(uids=[old,new],template_id='',confirmed=True,fingerprint=plan['fingerprint']))
    assert [v['id'] for v in av.catalog(old)['versions']]==['c']
    assert [v['id'] for v in av.catalog(new)['versions']]==['c']


def test_same_dimensions_independent_subtitles_selective_rebuild_and_preview(make_project,monkeypatch):
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    api=TestClient(app)
    definitions=[definition('chinese',**{'assemble.subtitle_track':'trans'}),definition('bilingual',**{'assemble.subtitle_track':'bilingual'})]
    av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=definitions))
    config.set_key('subtitles.mode','target')  # 版本不依赖公共字幕导出类型。
    data=av.plan(uid,{'version_ids':['chinese','bilingual']})
    assert not data['blockers']
    assert all(step!='subtitles' for step,_,_ in Dependencies(crud.get_project(uid)).with_versions(data['versions']).inputs('assemble'))
    from dubforge.pipeline import step_assemble
    mux=step_assemble._mux_original_sub
    calls=[]
    def capture(ctx,*args,**kwargs):
        calls.append((ctx.version['id'],ctx.subtitle_path.read_text(encoding='utf-8')))
        return mux(ctx,*args,**kwargs)
    monkeypatch.setattr(step_assemble,'_mux_original_sub',capture)
    runner.start_versions(uid,{'version_ids':['chinese','bilingual'],'fingerprint':data['fingerprint']});wait(uid)
    assert [v[0] for v in calls]==['chinese','bilingual']
    assert calls[0][1]!=calls[1][1]
    assert len(list((root/'output').glob('final_*.mp4')))==2
    first=(root/'output/final_version_chinese.mp4').stat().st_mtime_ns
    old_ids={v['id']:v['artifact_id'] for v in av.view(uid)['versions']}
    assert all(v['fresh'] for v in av.view(uid)['versions'])
    definitions[1]['overrides']['assemble.subtitle_color']='#00FF00'
    av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=definitions))
    assert [v['fresh'] for v in av.view(uid)['versions']]==[True,False]
    next_plan=av.plan(uid,{'version_ids':['chinese','bilingual']})
    runner.start_versions(uid,{'version_ids':['chinese','bilingual'],'fingerprint':next_plan['fingerprint']});wait(uid)
    assert [v[0] for v in calls]==['chinese','bilingual','bilingual']
    assert (root/'output/final_version_chinese.mp4').stat().st_mtime_ns==first
    assert {v['id']:v['artifact_id'] for v in av.view(uid)['versions']}==old_ids
    before=copy.deepcopy(av.catalog(uid))
    draft=definition('chinese',**{'assemble.subtitle_track':'source','assemble.subtitle_color':'#FF00FF'})
    response=api.post(f'/api/projects/{uid}/assembly-versions/frame',json={'version':draft,'seconds':1})
    assert response.status_code==200 and response.content.startswith(b'\x89PNG')
    assert av.catalog(uid)==before
    response=api.post(f'/api/projects/{uid}/assembly-versions/preview',json={'version':draft,'start':1,'end':2})
    assert response.status_code==200,response.text
    wait(uid)
    assert len(list((root/'output').glob('preview_version_*.mp4')))==1
    assert av.catalog(uid)==before
    assert not list(root.rglob('.tmp*'))
    assert all(v['fresh'] for v in av.view(uid)['versions'])
    files=api.get(f'/api/publishing/projects/{uid}/candidates').json()['files']
    assert all(not f['has_dubbing'] for f in files if f.get('version_id'))


def test_manual_and_workflow_resolve_identical_versions(make_project,monkeypatch):
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    definitions=[definition('a',**{'assemble.subtitle_track':'trans'}),definition('b',**{'assemble.subtitle_track':'bilingual'})]
    av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=definitions))
    monkeypatch.setattr(workflow_plan,'production_checks',lambda *a,**kw:[])
    automatic=workflow_plan.plan(uid,{'mode':'produce','version_ids':['b','a']},online=False)
    manual=av.plan(uid,{'version_ids':['b','a']})
    assert automatic['version_specs']==manual['versions']
    assert 'subtitles' not in automatic['steps']
    config.set_key('assemble.subtitle_color','#FF0000')
    assert automatic['version_specs']==manual['versions'],'已确认的版本不随全局值漂移'


def test_stale_preflight_cannot_launch_and_invalid_ids_return_business_errors(make_project,monkeypatch):
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=[definition()]))
    plan=av.plan(uid,{'version_ids':['chinese']})
    av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=[definition(**{'assemble.subtitle_size':70})]))
    with pytest.raises(av.VersionError,match='重新预检'):
        runner.start_versions(uid,{'version_ids':['chinese'],'fingerprint':plan['fingerprint']})
    api=TestClient(app)
    for ids in [[],[{}],['chinese','chinese'],['no-such']]:
        assert api.post(f'/api/projects/{uid}/assembly-versions/plan',json={'version_ids':ids}).status_code==400


def test_preview_context_contains_shared_subtitle_positions(make_project,monkeypatch):
    uid,_=_prepare_outputs_project(make_project,monkeypatch)
    response=TestClient(app).get(f'/api/projects/{uid}/assembly-versions/preview-context')
    assert response.status_code==200,response.text
    assert response.json()['duration']>0 and response.json()['subtitles']
    assert TestClient(app).post('/api/projects/missing/assembly-versions/frame',json={'version':definition()}).status_code==400
    assert TestClient(app).post('/api/projects/missing/assembly-versions/preview',json={'version':definition(),'start':0,'end':1}).status_code==400


@pytest.mark.parametrize('force_rebuild', [False, True])
def test_failed_replacement_keeps_previous_file_and_binding(make_project,monkeypatch,force_rebuild):
    from dubforge.pipeline import step_assemble
    from dubforge.errors import DubForgeError
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=[definition()]))
    body={'version_ids':['chinese']};body['fingerprint']=av.plan(uid,body)['fingerprint']
    runner.start_versions(uid,body);wait(uid)
    before=(root/'output/final_version_chinese.mp4').read_bytes()
    binding=av.results(uid)['chinese'].fingerprint
    if force_rebuild:
        body['force_rebuild'] = True
    else:
        av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=[definition(**{'assemble.subtitle_size':60})]))
    def fail(*args,**kwargs): raise DubForgeError('模拟编码失败')
    monkeypatch.setattr(step_assemble,'_mux_original_sub',fail)
    body['fingerprint']=av.plan(uid,body)['fingerprint']
    runner.start_versions(uid,body)
    wait_status(uid,'assemble',{'failed'})
    until=time.monotonic()+10
    while runner.current_job_id(crud.get_project(uid).id,'assemble') is not None:
        assert time.monotonic()<until
        time.sleep(.01)
    assert (root/'output/final_version_chinese.mp4').read_bytes()==before
    assert av.results(uid)['chinese'].fingerprint==binding
    assert av.view(uid)['versions'][0]['fresh'] is force_rebuild
    assert not list(root.rglob('.tmp*'))


def test_explicit_full_rebuild_materializes_fresh_legacy_snapshot_without_changing_normal_reuse(make_project, monkeypatch):
    from dubforge.pipeline import step_assemble
    from dubforge.pipeline.assembly_models import AssemblyResult
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=[definition()]))
    request = dict(version_ids=['chinese'])
    request['fingerprint'] = av.plan(uid, request)['fingerprint']
    runner.start_versions(uid, request)
    wait(uid)
    with runner._lock:
        futures = list(runner._futures)
    for future in futures:
        future.result(timeout=30)
    old = av.results(uid)['chinese']
    with crud.session() as session:
        stored = session.get(AssemblyResult, uid + ':chinese')
        snapshot = json.loads(stored.snapshot)
        snapshot.pop('sentence_inputs')
        stored.snapshot = json.dumps(snapshot)
        session.add(stored)
        session.commit()
    deps = Dependencies(crud.get_project(uid))
    assert av.is_current(deps, av.resolve(deps, definition()))
    ordinary = av.plan(uid, request)
    assert ordinary['force_rebuild'] is False and ordinary['reusable'] == ['chinese']
    assert runner.start_versions(uid, {**request, 'fingerprint': ordinary['fingerprint']})['status'] == 'skipped'
    assert 'sentence_inputs' not in json.loads(av.results(uid)['chinese'].snapshot)
    explicit = av.plan(uid, {**request, 'force_rebuild': True})
    assert explicit['force_rebuild'] is True and explicit['reusable'] == []
    assert explicit['fingerprint'] != ordinary['fingerprint']
    with pytest.raises(av.VersionError, match='重新预检'):
        runner.start_versions(uid, {**request, 'force_rebuild': True, 'fingerprint': ordinary['fingerprint']})
    with pytest.raises(av.VersionError, match='重新预检'):
        runner.start_versions(uid, {**request, 'fingerprint': explicit['fingerprint']})
    path = root / 'output/final_version_chinese.mp4'
    before = path.read_bytes()
    calls = []
    real = step_assemble._mux_original_sub
    def capture(ctx, video, target, *args, **kwargs):
        assert ctx.task_data['force_rebuild'] is True
        assert path.read_bytes() == before
        assert target.name.startswith('.tmp_pending_')
        calls.append(target)
        return real(ctx, video, target, *args, **kwargs)
    monkeypatch.setattr(step_assemble, '_mux_original_sub', capture)
    started = runner.start_versions(uid, {**request, 'force_rebuild': True, 'fingerprint': explicit['fingerprint']})
    assert started['status'] != 'skipped'
    wait(uid)
    rebuilt = av.results(uid)['chinese']
    assert len(calls) == 1 and rebuilt.artifact_id == old.artifact_id
    assert json.loads(rebuilt.snapshot)['sentence_inputs']['coverage'] == 'known'
    assert not list(root.rglob('.tmp*'))


def test_force_rebuild_preview_and_nonboolean_values_are_rejected(make_project):
    uid = make_project()
    with pytest.raises(av.VersionError, match='试片'):
        av.plan(uid, dict(version=definition(), force_rebuild=True), allow_draft=True)
    with pytest.raises(av.VersionError, match='明确选择'):
        av.plan(uid, dict(version_ids=['chinese'], force_rebuild='true'))


def test_cut_renderer_revision_only_changes_cut_version_step_and_preview_fingerprints(make_project, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from dubforge.pipeline import output_cuts, assembly_preview
    from dubforge.pipeline import stale_explanation
    from dubforge.runner import compute_fingerprint
    from test_output_cuts import sample
    uid = make_project()
    project = crud.get_project(uid)
    deps = Dependencies(project)
    plain = av.resolve(deps, definition())
    cut_version = {**plain, 'output_cuts': sample()}
    uncut_before = av.fingerprint(deps, plain)
    cut_before = av.fingerprint(deps, cut_version)
    cut_legacy_before = av.fingerprint(deps, cut_version, legacy=True)
    monkeypatch.setattr(output_cuts, 'snapshot', lambda _: None)
    step_uncut_before = compute_fingerprint(project, 'assemble', dependencies=Dependencies(project))
    monkeypatch.setattr(output_cuts, 'snapshot', lambda _: sample())
    step_cut_before = compute_fingerprint(project, 'assemble', dependencies=Dependencies(project))
    preview_path = tmp_path / ('preview_version_chinese_' + 'a' * 32 + '.mp4')
    preview_path.write_bytes(b'isolated preview identity')
    ctx = SimpleNamespace(task_data={})
    assembly_preview.record(ctx, cut_version, preview_path, cut_before, start=0, end=5)
    preview_before = json.loads(assembly_preview.metadata_path(preview_path).read_text())['snapshot']
    monkeypatch.setattr(output_cuts, 'RENDER_VERSION', output_cuts.RENDER_VERSION + 1)
    assert av.fingerprint(deps, plain) == uncut_before
    cut_after = av.fingerprint(deps, cut_version)
    assert cut_after != cut_before
    assert av.fingerprint(deps, cut_version, legacy=True) != cut_legacy_before
    assert compute_fingerprint(project, 'assemble', dependencies=Dependencies(project)) != step_cut_before
    monkeypatch.setattr(output_cuts, 'snapshot', lambda _: None)
    assert compute_fingerprint(project, 'assemble', dependencies=Dependencies(project)) == step_uncut_before
    assembly_preview.record(ctx, cut_version, preview_path, cut_after, start=0, end=5)
    preview_after = json.loads(assembly_preview.metadata_path(preview_path).read_text())['snapshot']
    assert preview_before['render_fingerprint'] != preview_after['render_fingerprint']
    assert preview_before['plan_fingerprint'] != preview_after['plan_fingerprint']
    assert stale_explanation._valid_key('rule.output_cuts_renderer')
    assert stale_explanation._valid_key('version.chinese.rule.output_cuts_renderer')


def test_manifest_recovery_preserves_version_identity_and_missing_files(make_project,monkeypatch):
    import json
    from sqlmodel import select
    from dubforge.domain.models import Project, Artifact, Job, StepRun
    from dubforge.pipeline.assembly_models import AssemblyResult
    pending=[]
    submit=runner._pool.submit
    def track(*args,**kwargs):
        future=submit(*args,**kwargs)
        pending.append(future)
        return future
    monkeypatch.setattr(runner._pool,'submit',track)
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    av.save(uid,dict(revision=av.catalog(uid)['revision'],versions=[definition()]))
    plan=av.plan(uid,{'version_ids':['chinese']})
    runner.start_versions(uid,{'version_ids':['chinese'],'fingerprint':plan['fingerprint']});wait(uid)
    # done及current_job清除早于worker末尾的清单/项目状态写入；破坏数据库前必须等完整收尾。
    for future in pending:future.result(timeout=30)
    crud.write_manifest(uid)
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['configuration']['values']['assembly.versions'][0]['id']=='chinese'
    assert manifest['assembly_results'][0]['name']=='final_version_chinese.mp4'
    old=crud.get_project(uid)
    with crud.session() as s:
        for model in (Artifact,Job,StepRun):
            for row in s.exec(select(model).where(model.project_id==old.id)).all():s.delete(row)
        for row in s.exec(select(AssemblyResult).where(AssemblyResult.project_uid==uid)).all():s.delete(row)
        s.delete(s.get(ConfigurationScope,'project:'+uid));s.delete(s.get(Project,old.id));s.commit()
    assert crud.rebuild_from_dirs()['projects']==1
    assert av.view(uid)['versions'][0]['fresh'] is True
    (root/'output/final_version_chinese.mp4').unlink()
    assert av.view(uid)['versions'][0]['status']=='文件缺失'


def test_comments_presentation_is_sparse_and_does_not_mutate_shared_plan(make_project):
    from dubforge import comment_studio
    uid=make_project();deps=Dependencies(crud.get_project(uid))
    overlay=dict(settings=comment_studio.default_settings(),items=[])
    before=copy.deepcopy(overlay)
    spec={**definition(), 'include_comments':True,'overrides':{'comments.language_mode':'bilingual','comments.background_opacity':0}}
    value=av.resolve(deps,spec,comment_plan=overlay)
    assert value['comment_overlay']['settings']['language_mode']=='bilingual'
    assert value['comment_overlay']['settings']['background_opacity']==0
    assert overlay==before
    spec['include_comments']=False
    first=av.fingerprint(deps,av.resolve(deps,spec,comment_plan=overlay))
    spec['overrides']['comments.language_mode']='original'
    assert av.fingerprint(deps,av.resolve(deps,spec,comment_plan=overlay))==first


def test_comment_reconfirmation_keeps_historical_assembly_current(make_project):
    """重新确认同一批评论只更新时间时，旧成片仍可复用。

    旧版指纹包含 ``confirmed_at``，因此这里登记一个 legacy 指纹来验证
    迁移兼容；真正改变评论内容仍必须让成片过期。
    """
    from dubforge import comment_studio
    from dubforge.pipeline.assembly_models import AssemblyResult
    from conftest import mark_upstream_done

    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    version = dict(id='comments', name='评论版', dubbing=False, burn_subtitles=False,
                   include_comments=True, overrides={})
    overlay = dict(settings=comment_studio.default_settings(),
                   items=[dict(id='c1', source_hash='source', text='Hello',
                               display_text='你好', author='A')],
                   confirmed_at='2026-10-10T05:00:00+00:00')
    deps = Dependencies(project)
    resolved = av.resolve(deps, version, comment_plan=overlay)
    name = 'final_version_comments.mp4'
    path = root / 'output' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'old completed video')
    art = crud.add_artifact(project.id, 'assemble', 'video', name, path, f'output/{name}')
    step = crud.get_step(project.id, 'assemble')
    step.status = 'done'; crud.save_step(step)
    # 新建产物后读取一个新的依赖快照，模拟状态接口下一次请求。
    deps = Dependencies(project)
    old_fingerprint = av.fingerprint(deps, resolved, legacy=True)
    with crud.session() as session:
        session.add(AssemblyResult(key=f'{uid}:comments', project_uid=uid,
                    version_id='comments', artifact_id=art.id, artifact_hash=art.sha256,
                    fingerprint=old_fingerprint, snapshot=json.dumps(resolved, ensure_ascii=False)))
        session.commit()

    # A fresh confirmation changes only this bookkeeping field.
    reconfirmed = copy.deepcopy(resolved)
    reconfirmed['comment_overlay']['confirmed_at'] = '2026-10-10T06:00:00+00:00'
    assert av.is_current(deps, reconfirmed)

    changed = copy.deepcopy(reconfirmed)
    changed['comment_overlay']['items'][0]['display_text'] = '完全不同的评论'
    assert not av.is_current(deps, changed)


def test_publication_reference_blocks_version_removal(make_project):
    from test_configuration import save
    uid=make_project()
    av.save(uid,dict(revision=0,versions=[definition('a'),definition('b')]))
    save('project:'+uid,{'run.mode':'produce','publish.version_ids':['b'],'publish.part_titles':{'b':'默认双语'}})
    with pytest.raises(DubForgeError,match='分P引用'):
        av.save(uid,dict(revision=2,versions=[definition('a')]))
    save('global',{'assembly.versions':[definition('b')],'publish.part_titles':{'b':'默认双语'}})
    fresh=make_project()
    assert [(v['id'],v['name']) for v in av.catalog(fresh)['versions']]==[('b','b')]
    assert [v['id'] for v in av.catalog(uid)['versions']]==['a','b']
