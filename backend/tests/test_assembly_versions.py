"""版本继承、局部重生成和真实 FFmpeg；不访问用户文件或B站。"""
import copy
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
    uid = make_project()
    config.set_key('assemble.subtitle_track', 'trans')
    fixed = definition('fixed', **{'assemble.subtitle_track': 'trans'})
    follow = definition('follow')
    deps = Dependencies(crud.get_project(uid))
    a, b = av.resolve(deps, fixed), av.resolve(deps, follow)
    assert a['origins']['assemble.subtitle_track'] == '本版本'
    assert b['origins']['assemble.subtitle_track'] == '全局默认'
    before = av.fingerprint(deps, a)
    config.set_key('assemble.subtitle_track', 'bilingual')
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


def test_global_versions_are_inherited_by_existing_and_new_projects(make_project):
    from test_configuration import save
    old=make_project()
    values=[definition('a'),definition('b',**{'assemble.subtitle_track':'bilingual'})]
    save('global',{'assembly.versions':values})
    new=make_project()
    assert [v['id'] for v in av.catalog(new)['versions']]==['a','b']
    assert [v['id'] for v in av.catalog(old)['versions']]==['a','b']
    save('global',{'assembly.versions':[definition('c')]})
    assert [v['id'] for v in av.catalog(new)['versions']]==['c']


def test_same_dimensions_independent_subtitles_selective_rebuild_and_preview(make_project,monkeypatch):
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    api=TestClient(app)
    definitions=[definition('chinese',**{'assemble.subtitle_track':'trans'}),definition('bilingual',**{'assemble.subtitle_track':'bilingual'})]
    av.save(uid,dict(revision=0,versions=definitions))
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
    av.save(uid,dict(revision=1,versions=definitions))
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
    av.save(uid,dict(revision=0,versions=definitions))
    monkeypatch.setattr(workflow_plan,'production_checks',lambda *a,**kw:[])
    automatic=workflow_plan.plan(uid,{'mode':'produce','version_ids':['b','a']},online=False)
    manual=av.plan(uid,{'version_ids':['b','a']})
    assert automatic['version_specs']==manual['versions']
    assert 'subtitles' not in automatic['steps']
    config.set_key('assemble.subtitle_color','#FF0000')
    assert automatic['version_specs']==manual['versions'],'已确认的版本不随全局值漂移'


def test_stale_preflight_cannot_launch_and_invalid_ids_return_business_errors(make_project,monkeypatch):
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    av.save(uid,dict(revision=0,versions=[definition()]))
    plan=av.plan(uid,{'version_ids':['chinese']})
    av.save(uid,dict(revision=1,versions=[definition(**{'assemble.subtitle_size':70})]))
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


def test_failed_replacement_keeps_previous_file_and_binding(make_project,monkeypatch):
    from dubforge.pipeline import step_assemble
    from dubforge.errors import DubForgeError
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    av.save(uid,dict(revision=0,versions=[definition()]))
    body={'version_ids':['chinese']};body['fingerprint']=av.plan(uid,body)['fingerprint']
    runner.start_versions(uid,body);wait(uid)
    before=(root/'output/final_version_chinese.mp4').read_bytes()
    binding=av.results(uid)['chinese'].fingerprint
    av.save(uid,dict(revision=1,versions=[definition(**{'assemble.subtitle_size':60})]))
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
    assert av.view(uid)['versions'][0]['fresh'] is False
    assert not list(root.rglob('.tmp*'))


def test_manifest_recovery_preserves_version_identity_and_missing_files(make_project,monkeypatch):
    import json
    from sqlmodel import select
    from dubforge.domain.models import Project, Artifact, Job, StepRun
    from dubforge.pipeline.assembly_models import AssemblyResult
    uid,root=_prepare_outputs_project(make_project,monkeypatch)
    av.save(uid,dict(revision=0,versions=[definition()]))
    plan=av.plan(uid,{'version_ids':['chinese']})
    runner.start_versions(uid,{'version_ids':['chinese'],'fingerprint':plan['fingerprint']});wait(uid)
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
