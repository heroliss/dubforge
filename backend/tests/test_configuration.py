"""统一配置跨入口契约；所有数据库和媒体目录均隔离。"""
import json
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from app.main import app
from dubforge import configuration as cfg
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline import assembly_versions
from dubforge.errors import DubForgeError


def save(key, updates=None, reset=None):
    data = cfg.resolve(key=key)
    return cfg.save(key, dict(revision=data['revision'], fingerprint=data['fingerprint'], updates=updates or {}, reset=reset or []))


def bind(uids, template):
    plan = cfg.binding_plan(uids, template['key'][7:])
    return cfg.bind(dict(uids=uids, template_id=template['key'][7:], fingerprint=plan['fingerprint'], confirmed=True))


def test_same_values_for_steps_versions_and_run_plan(make_project):
    uid = make_project()
    template = cfg.create_template('旅行系列')
    save(template['key'], {'assemble.subtitle_color':'#FFE082','comments.position':'top_left','publish.season_id':42})
    bind([uid], template)
    project = crud.get_project(uid)
    data = cfg.resolve(uid)
    assert data['origins']['assemble.subtitle_color'] == '旅行系列'
    assert Dependencies(project).cfg('assemble.subtitle_color') == '#FFE082'
    assert assembly_versions.view(uid)['defaults']['assemble.subtitle_color'] == '#FFE082'
    assert cfg.run_spec(uid)['overrides']['season_id'] == 42
    api = TestClient(app).get(f'/api/projects/{uid}/step_params').json()
    assert api['steps']['assemble']['values']['assemble.subtitle_color'] == '#FFE082'
    assert api['parent_origins']['assemble.subtitle_color'] == '旅行系列'


def test_source_kind_is_not_guessed_from_template_name_and_is_shared_by_views(make_project):
    from dubforge import comment_studio
    uid = make_project()
    template = cfg.create_template('全局默认')
    versions = cfg.resolve(uid)['values']['assembly.versions']
    save(template['key'], {'assemble.subtitle_color': '#FFE082', 'comments.outline': 0,
                           'assembly.versions': versions})
    bind([uid], template)
    save('project:' + uid, {'assemble.subtitle_color': '#FFFFFF'})
    data = cfg.resolve(uid)
    assert data['sources']['assemble.voice_volume'] == {'kind': 'global', 'frozen': True}
    assert data['sources']['assemble.subtitle_color'] == {'kind': 'project'}
    assert data['parent_sources']['assemble.subtitle_color'] == {'kind': 'series', 'name': '全局默认', 'frozen': True}
    assert data['sources']['comments.outline'] == {'kind': 'series', 'name': '全局默认', 'frozen': True}
    steps = TestClient(app).get(f'/api/projects/{uid}/step_params').json()
    assert steps['sources'] == data['sources']
    assert steps['parent_sources'] == data['parent_sources']
    assert comment_studio.view(uid)['configuration']['sources'] == data['sources']
    versions_view = assembly_versions.view(uid)
    assert versions_view['source'] == {'kind': 'series', 'name': '全局默认', 'frozen': True}
    assert versions_view['sources']['assemble.subtitle_color'] == {'kind': 'project'}
    current = cfg.resolve(key=template['key'])
    cfg.save(template['key'], {'revision': current['revision'], 'updates': {}, 'name': '项目设置'})
    assert cfg.resolve(uid)['sources']['comments.outline'] == {'kind': 'series', 'name': '全局默认', 'frozen': True}
    save('project:' + uid, reset=['assemble.subtitle_color'])
    assert cfg.resolve(uid)['sources']['assemble.subtitle_color'] == {'kind': 'series', 'name': '全局默认', 'frozen': True}
    bind([uid], template)
    assert cfg.resolve(uid)['sources']['assemble.subtitle_color'] == {'kind': 'series', 'name': '项目设置', 'frozen': True}


def test_explicit_equal_false_zero_and_null_are_not_inheritance(make_project):
    uid=make_project(); template=cfg.create_template('模板');bind([uid],template)
    save('project:'+uid, {'assemble.voice_volume':0, 'assemble.loudnorm':False, 'publish.season_id':None})
    save(template['key'], {'assemble.voice_volume':.75,'assemble.loudnorm':True,'publish.season_id':7})
    result=cfg.resolve(uid)
    assert result['values']['assemble.voice_volume']==0
    assert result['values']['assemble.loudnorm'] is False
    assert result['values']['publish.season_id'] is None
    assert result['origins']['assemble.loudnorm']=='本项目'
    save('project:'+uid, reset=['assemble.voice_volume','assemble.loudnorm','publish.season_id'])
    assert cfg.resolve(uid)['values']['publish.season_id'] is None
    bind([uid], template)
    assert cfg.resolve(uid)['values']['publish.season_id']==7


def test_existing_projects_keep_template_baseline_until_explicitly_applied(make_project):
    first,second=make_project(),make_project(); template=cfg.create_template('同系列');bind([first,second],template)
    version=dict(id='original',name='原声',dubbing=False,burn_subtitles=False,include_comments=False,overrides={})
    save(template['key'],{'assembly.versions':[version]})
    assert assembly_versions.catalog(first)['versions'][0]['id'] == 'dub'
    assert assembly_versions.catalog(second)['versions'][0]['id'] == 'dub'
    bind([first,second], template)
    assert assembly_versions.catalog(first)['versions']==[version]
    assert assembly_versions.catalog(second)['versions']==[version]
    save('project:'+first,{'assembly.versions':[{**version,'name':'本片原声'}]})
    save(template['key'],{'assembly.versions':[{**version,'name':'系列原声'}]})
    assert assembly_versions.catalog(first)['versions'][0]['name']=='本片原声'
    assert assembly_versions.catalog(second)['versions'][0]['name']=='原声'
    bind([first,second], template)
    assert assembly_versions.catalog(first)['versions'][0]['name']=='本片原声'
    assert assembly_versions.catalog(second)['versions'][0]['name']=='系列原声'


def test_binding_is_confirmed_atomic_and_preserves_overrides(make_project):
    uid=make_project();template=cfg.create_template('A');save('project:'+uid,{'assemble.crf':19})
    plan=cfg.binding_plan([uid],template['key'][7:])
    save(template['key'],{'assemble.crf':22})
    with pytest.raises(DubForgeError):cfg.bind(dict(uids=[uid],template_id=template['key'][7:],confirmed=True,fingerprint=plan['fingerprint']))
    assert cfg.resolve(uid)['parent']==''
    bind([uid],template)
    assert cfg.resolve(uid)['values']['assemble.crf']==19


def test_archived_template_keeps_existing_bindings_but_rejects_new(make_project):
    a,b=make_project(),make_project();template=cfg.create_template('A');bind([a],template)
    cfg.archive(template['key'],dict(revision=0,archived=True))
    assert cfg.resolve(a)['parent']==template['key']
    with pytest.raises(DubForgeError):bind([b],template)


@pytest.mark.parametrize('updates',[{'llm.api_key':'not-allowed'},{'assemble.loudnorm':'false'},
    {'comments.duration':60,'comments.interval':1},{'publish.version_ids':['missing']},{'publish.season_id':-1}])
def test_invalid_scope_edits_do_not_partially_save(make_project,updates):
    uid=make_project();before=cfg.resolve(uid)
    with pytest.raises(DubForgeError):save('project:'+uid,updates)
    assert cfg.resolve(uid)==before


def test_comment_style_inherits_without_copying_content(make_project):
    from dubforge import comment_studio
    uid=make_project();template=cfg.create_template('评论系列')
    save(template['key'],{'comments.color':'#FFCA28','comments.start':20})
    bind([uid],template)
    result=comment_studio.view(uid)
    assert result['defaults']['color']=='#FFCA28' and result['defaults']['start']==20
    assert result['overlay'] is None and result['rows']==[]


def test_global_or_series_publish_changes_do_not_change_media_fingerprints(make_project):
    uid=make_project();template=cfg.create_template('A');bind([uid],template)
    spec=assembly_versions.catalog(uid)['versions'][0]
    before=assembly_versions.fingerprint(Dependencies(crud.get_project(uid)),assembly_versions.resolve(Dependencies(crud.get_project(uid)),spec))
    save(template['key'],{'publish.season_id':123})
    after=assembly_versions.fingerprint(Dependencies(crud.get_project(uid)),assembly_versions.resolve(Dependencies(crud.get_project(uid)),spec))
    assert before==after


def test_new_api_has_no_recipe_write_endpoint(make_project):
    uid=make_project();client=TestClient(app)
    assert client.put(f'/api/projects/{uid}/workflow/recipe',json={}).status_code in {404,405}
    assert client.get('/api/configuration/project:'+uid).status_code==200
    assert client.get('/api/configuration/global').json()['sections']['comments']['label']=='上屏评论'


def test_migration_moves_values_once_and_leaves_files_and_receipts(isolated_env,make_project):
    from dubforge.db import get_engine
    from dubforge.configuration_migration import migrate
    uid=make_project()
    with get_engine().begin() as conn:
        conn.exec_driver_sql('CREATE TABLE workflowrecipe (key TEXT PRIMARY KEY, "values" TEXT, revision INTEGER)')
        conn.exec_driver_sql('INSERT INTO workflowrecipe VALUES (?,?,0)',(uid,json.dumps({'comment_settings':{'color':'#FFCA28'},'overrides':{'season_id':91}})))
        conn.exec_driver_sql('DELETE FROM configurationmigration')
    migrate(get_engine())
    data=cfg.resolve(uid)
    assert data['values']['comments.color']=='#FFCA28'
    assert data['values']['publish.season_id']==91
    assert crud.project_dir(uid).exists()
    migrate(get_engine())
    assert cfg.resolve(uid)==data


def test_step_context_prefix_and_scalar_use_same_fixed_values(make_project):
    import threading
    from dubforge.runner import StepContext
    uid=make_project();template=cfg.create_template('配音参数')
    save(template['key'],{'tts.indextts25.num_beams':1,'tts.indextts25.emo_weight':0})
    bind([uid],template)
    project=crud.get_project(uid);root=crud.project_dir(uid)
    ctx=StepContext(project,'tts',root,root/'logs/fixture.log',threading.Event())
    assert ctx.cfg('tts.indextts25')['emo_weight']==ctx.cfg('tts.indextts25.emo_weight')==0
    save(template['key'],{'tts.indextts25.num_beams':3})
    assert ctx.cfg('tts.indextts25')['num_beams']==1
    altered=ctx.cfg('tts.indextts25');altered['num_beams']=9
    assert ctx.cfg('tts.indextts25.num_beams')==1


def test_full_migration_preserves_accounts_templates_versions_and_receipts(isolated_env,make_project):
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    from dubforge.db import get_engine
    from dubforge.configuration_migration import migrate
    from dubforge.publishing.models import PublishAccount, Publication
    from dubforge.publishing import service
    uid=make_project();engine=get_engine()
    receipt={'bvid':'BVfixture'}
    with Session(engine) as session:
        session.add(PublishAccount(id='a',name='旅行账号',credential='encrypted-fixture'))
        session.add(Publication(id='pub',project_uid=uid,draft=json.dumps(dict(account_id='a',template_id='travel',overrides={'watermark':False})),snapshot=json.dumps({'settings':{'title':'已冻结'}}),receipt=json.dumps(receipt)))
        session.commit()
    version=dict(id='custom',name='原声',dubbing=False,burn_subtitles=True,include_comments=True,overrides={})
    with engine.begin() as conn:
        conn.exec_driver_sql('DELETE FROM configurationmigration')
        conn.exec_driver_sql('DELETE FROM configurationscope WHERE key=?', ('project:' + uid,))
        conn.exec_driver_sql('ALTER TABLE publishaccount ADD COLUMN defaults TEXT')
        conn.exec_driver_sql('UPDATE publishaccount SET defaults=?',(json.dumps({'watermark':True,'visibility':'self'}),))
        conn.exec_driver_sql('CREATE TABLE publishtemplate (id TEXT, name TEXT, account_id TEXT, "values" TEXT, archived INTEGER)')
        conn.exec_driver_sql('INSERT INTO publishtemplate VALUES (?,?,?,?,0)',('travel','旅行','a',json.dumps({'season_id':91})))
        conn.exec_driver_sql('CREATE TABLE workflowrecipe (key TEXT, "values" TEXT)')
        conn.exec_driver_sql('INSERT INTO workflowrecipe VALUES (?,?)',(uid,json.dumps({'template_id':'travel','account_id':'a','variants':['src_hardsub_comments'],'part_titles':{'src_hardsub_comments':'P1 原声'},'comment_settings':{'color':'#FFCA28'}})))
        conn.exec_driver_sql('CREATE TABLE assemblycatalog (key TEXT, versions TEXT)')
        conn.exec_driver_sql('INSERT INTO assemblycatalog VALUES (?,?)',(uid,json.dumps([version])))
        conn.exec_driver_sql('UPDATE project SET params=? WHERE uid=?',(json.dumps({'step_params':{'assemble.voice_volume':0,'assemble.loudnorm':False},'auto_steps':['download']}),uid))
    migrate(engine)
    data=cfg.resolve(uid)
    assert data['parent']=='series:travel'
    assert data['values']['assemble.voice_volume']==0 and data['values']['assemble.loudnorm'] is False
    assert data['values']['publish.visibility']=='self' and data['values']['publish.watermark'] is True
    assert data['values']['publish.version_ids']==['custom']
    assert data['values']['publish.part_titles']=={'custom':'P1 原声'}
    assert cfg.resolve(key='series:account-a')['values']['publish.watermark'] is True
    assert cfg.run_spec(uid)['overrides']['visibility']=='self'
    assert 'step_params' not in crud.project_params(crud.get_project(uid))
    with Session(engine) as session:
        pub=session.get(Publication,'pub')
        assert json.loads(pub.receipt)==receipt and json.loads(pub.snapshot)=={'settings':{'title':'已冻结'}}
        assert json.loads(pub.draft)['overrides']=={'watermark':False,'visibility':'self','season_id':91}
        assert session.get(PublishAccount,'a').credential=='encrypted-fixture'
    with engine.connect() as conn:
        tables={r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {'workflowrecipe','assemblycatalog','publishtemplate'}
        assert 'defaults' not in [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(publishaccount)')]
    backup=Path(engine.url.database).with_name(Path(engine.url.database).stem+'.before-unified-configuration.sqlite3')
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute('SELECT defaults FROM publishaccount').fetchone()[0]
        assert connection.execute('SELECT count(*) FROM workflowrecipe').fetchone()[0]==1
    migrate(engine)
    assert cfg.resolve(uid)==data
    assert 'defaults' not in service.catalog()['accounts'][0]


def test_failed_migration_rolls_back_legacy_settings(make_project):
    from dubforge.db import get_engine
    from dubforge.configuration_migration import migrate
    uid=make_project();engine=get_engine()
    raw=json.dumps({'step_params':{'assemble.crf':18}})
    with engine.begin() as conn:
        conn.exec_driver_sql('DELETE FROM configurationmigration')
        conn.exec_driver_sql('CREATE TABLE workflowrecipe (key TEXT, "values" TEXT)')
        conn.exec_driver_sql('INSERT INTO workflowrecipe VALUES (?,?)',(uid,json.dumps({'version_ids':['missing']})))
        conn.exec_driver_sql('UPDATE project SET params=? WHERE uid=?',(raw,uid))
    with pytest.raises(DubForgeError):migrate(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT params FROM project WHERE uid=?',(uid,)).first()[0]==raw
        assert conn.exec_driver_sql('SELECT count(*) FROM workflowrecipe').first()[0]==1
        assert conn.exec_driver_sql('SELECT count(*) FROM configurationmigration').first()[0]==0


def test_migration_finishes_existing_scopes_and_updates_recovery_manifest(make_project):
    from dubforge import db
    from dubforge.publishing.models import PublishAccount
    uid=make_project();save('project:'+uid,{'publish.account_id':'a','assemble.voice_volume':0})
    with Session(db.get_engine()) as session:
        session.add(PublishAccount(id='a',name='账号',credential='encrypted-fixture'));session.commit()
    with db.get_engine().begin() as conn:
        conn.exec_driver_sql('DELETE FROM configurationmigration')
        conn.exec_driver_sql('UPDATE configurationscope SET baseline=?', ('{}',))
        conn.exec_driver_sql('INSERT INTO configurationmigration VALUES (1)')
        conn.exec_driver_sql('ALTER TABLE publishaccount ADD COLUMN defaults TEXT')
        conn.exec_driver_sql('UPDATE publishaccount SET defaults=?',(json.dumps({'watermark':True}),))
        conn.exec_driver_sql('CREATE TABLE workflowrecipe (key TEXT, "values" TEXT)')
        conn.exec_driver_sql('INSERT INTO workflowrecipe VALUES (?,?)',(uid,json.dumps({'overrides':{'season_id':42}})))
    db.init_db()
    data=cfg.resolve(uid)
    assert data['values']['assemble.voice_volume']==0
    assert data['values']['publish.season_id']==42
    assert data['values']['publish.watermark'] is True
    assert data['parent']=='series:account-a'
    manifest=json.loads((crud.project_dir(uid)/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['configuration']['parent']==data['parent']
    assert manifest['configuration']['values']['assemble.voice_volume']==0
    db.init_db()
    assert cfg.resolve(uid)==data


def test_parent_can_change_versions_without_altering_fixed_project_but_apply_is_validated(make_project):
    uid=make_project();template=cfg.create_template('系列');bind([uid],template)
    save('project:'+uid, {'publish.version_ids':['dub']})
    before=cfg.resolve(key=template['key'])
    save(template['key'],{'assembly.versions':[before['values']['assembly.versions'][1]]})
    assert cfg.resolve(uid)['values']['assembly.versions'][0]['id'] == 'dub'
    with pytest.raises(DubForgeError):
        cfg.binding_plan([uid], template['key'][7:])


def test_edit_and_binding_blocked_while_dependent_project_runs(make_project):
    """运行中的步骤只冻结相关设置；范围修改仍须等待任务结束。"""
    uid=make_project();template=cfg.create_template('系列');bind([uid],template)
    step=crud.get_step(crud.get_project(uid).id,'download');step.status='running';crud.save_step(step)
    for key in ['global',template['key'],'project:'+uid]:
        save(key,{'assemble.crf':19})
        assert cfg.resolve(key=key)['values']['assemble.crf'] == 19
    plan=cfg.binding_plan([uid],'')
    with pytest.raises(DubForgeError) as caught:
        cfg.bind(dict(uids=[uid],template_id='',confirmed=True,fingerprint=plan['fingerprint']))
    assert caught.value.code == 'CONFIGURATION_BUSY'
    assert cfg.resolve(uid)['parent']==template['key']


def test_batch_binding_rejects_queued_project_without_partially_binding(make_project):
    first,second=make_project(),make_project();template=cfg.create_template('系列')
    plan=cfg.binding_plan([first,second],template['key'][7:])
    step=crud.get_step(crud.get_project(second).id,'download');step.status='queued';crud.save_step(step)
    with pytest.raises(DubForgeError) as caught:
        cfg.bind(dict(uids=[first,second],template_id=template['key'][7:],confirmed=True,fingerprint=plan['fingerprint']))
    assert caught.value.code == 'CONFIGURATION_BUSY'
    assert cfg.resolve(first)['parent']==cfg.resolve(second)['parent']==''


def test_batch_plan_has_individual_blockers_and_start_has_stable_ids(make_project,monkeypatch):
    from dubforge import workflows
    first,second=make_project(),make_project()
    def preview(uid, intent):
        assert intent == {'mode': None}
        if uid==second:raise DubForgeError('本地源文件丢失')
        return dict(blockers=[],recipe={'mode':'produce'},fingerprint='fixture')
    monkeypatch.setattr(workflows,'preview',preview)
    plans=cfg.batch_plan({'uids':[first,second]})['items']
    assert plans[0]['blockers']==[] and plans[1]['blockers']==['本地源文件丢失']
    captured=[]
    def start(uid,body):captured.append((uid,body));return {'id':'test-run'}
    monkeypatch.setattr(workflows,'start',start)
    body={'request_id':'12345678-1234-1234-1234-123456789abc','confirmed':True,'items':[{'uid':first,'recipe':{},'fingerprint':'fixture'}]}
    cfg.batch_start(body);cfg.batch_start(body)
    assert captured[0]==captured[1]
    assert captured[0][1]['confirmed'] is True
    with pytest.raises(DubForgeError):cfg.batch_start({**body,'confirmed':False})
    assert len(captured)==2


@pytest.mark.parametrize('updates', [
    {'tts.backend':{}}, {'publish.visibility':{}}, {'translate.target_language':[]},
    {'separate.overlap':8}, {'separate.shifts':1.5}, {'split.max_split_length':0},
    {'runner.step_timeout_seconds.download':-1}, {'publish.version_ids':[]},
    {'publish.part_titles':{'missing':'无效分P'}}, {'publish.part_titles':{'dub':'😀'*41}},
    {'comments.prompt':'\ud800'}, {'split.max_split_length':10**400},
    {'tts.indextts25.emo_vector':[10**400,0,0,0,0,0,0,0]},
])
def test_invalid_values_report_business_errors_without_saving(make_project,updates):
    uid = make_project();before = cfg.resolve(uid)
    response = TestClient(app).put('/api/configuration/project:'+uid,
        content=json.dumps({'revision':before['revision'], 'updates':updates}),headers={'Content-Type':'application/json'})
    assert response.status_code == 400, response.text
    assert response.json()['code'] and response.json()['message']
    assert cfg.resolve(uid) == before


def test_batch_start_unexpected_failure_is_per_project_and_retry_id_is_stable(monkeypatch):
    from dubforge import workflows
    calls = []
    def start(uid, body):
        calls.append((uid,body['request_id']))
        if uid == 'broken':
            raise RuntimeError('模拟临时错误')
        return {'id':'run-'+uid}
    monkeypatch.setattr(workflows, 'start', start)
    body = {'confirmed':True, 'request_id':'12345678-1234-1234-1234-123456789abc',
            'items':[{'uid':'broken'}, {'uid':'good'}]}
    result = cfg.batch_start(body)
    assert result['items'][0]['code'] == 'BATCH_START_FAILED' and result['items'][1]['run_id'] == 'run-good'
    cfg.batch_start({**body, 'items':[{'uid':'broken'}]})
    assert calls[0] == calls[-1]
    with pytest.raises(DubForgeError):
        cfg.batch_start({**body, 'request_id':{}})


def test_manifest_restores_template_and_project_values_without_old_tables(make_project):
    from dubforge.configuration_models import ConfigurationScope
    uid=make_project();template=cfg.create_template('旅行系列')
    save(template['key'],{'assemble.subtitle_color':'#FFCC44'});bind([uid],template)
    save('project:'+uid,{'assemble.voice_volume':0})
    with crud.session() as session:
        from sqlmodel import select
        from dubforge.domain.models import StepRun
        for step in session.exec(select(StepRun).where(StepRun.project_id==crud.get_project(uid).id)).all():session.delete(step)
        session.flush()
        for key in ['project:'+uid,template['key']]:session.delete(session.get(ConfigurationScope,key))
        session.delete(session.get(type(crud.get_project(uid)),crud.get_project(uid).id));session.commit()
    crud.rebuild_from_dirs()
    assert cfg.resolve(uid)['values']['assemble.subtitle_color']=='#FFCC44'
    assert cfg.resolve(uid)['values']['assemble.voice_volume']==0


@pytest.mark.parametrize('body',[{'uids':[{}]}, {'uids':['a','a']}, {'uids':[]}])
def test_malformed_batch_is_domain_error(body):
    with pytest.raises(DubForgeError):cfg.batch_plan(body)
