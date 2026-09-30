"""创作管理只用隔离账号与伪网络；修改、导入都需明确确认。"""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from dubforge import db, comment_studio
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import credentials, imports, service, studio, transport
from dubforge.publishing.models import Publication, PublishAccount, PublishChange

BV = 'BV1234567890'


@pytest.fixture
def env(make_project, monkeypatch):
    uid = make_project()
    with Session(db.get_engine()) as s:
        s.add(PublishAccount(id='123', name='测试账号', credential='fixture'))
        s.add(PublishAccount(id='456', name='另一个账号', credential='fixture'))
        s.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda _: {'fixture': True})
    detail = dict(bvid=BV, aid=10, mid='123', version='version-1', collection=None,
                  settings=dict(title='原标题', description='原简介', tags=['旅行'], dynamic='', copyright=3,
                                source='来源', tid=21, human_type2=1001, declaration_id=1),
                  videos=[dict(cid=1, title='原声', filename='original-file'), dict(cid=2, title='配音', filename='dub-file')])
    calls = []
    def remote(operation, payload=None, **kwargs):
        calls.append((operation, copy.deepcopy(payload)))
        if operation == 'studio_detail':
            return copy.deepcopy(detail)
        if operation == 'studio_write':
            return dict(bvid=BV, aid=10, accepted=True)
        if operation == 'metadata':
            return dict(declarations=[dict(value=1, label='含AI生成内容')],
                        seasons=[dict(id=4, title='旅行合集', sections=[dict(id=5, title='默认')])])
        if operation == 'studio_templates':
            return dict(items=[dict(id=1, name='旅行', values=dict(title='模板标题', description='模板简介', tags=['旅行']))])
        if operation == 'studio_comments':
            return dict(bvid=BV, items=[dict(id='bili:'+BV+':1',text='旅行真棒！',author='观众',like_count=10)], total=1)
        pytest.fail(f'未声明网络请求：{operation}')
    monkeypatch.setattr(transport, 'call', remote)
    return SimpleNamespace(uid=uid, calls=calls, detail=detail, remote=remote)


def plan_edit(env):
    return studio.prepare('123', dict(bvid=BV, kind='edit', values={'description': '新简介'}, order=[2, 1]))


def test_preview_is_read_only_and_confirmation_consumes_frozen_plan_once(env):
    preview = plan_edit(env)
    assert [op for op, _ in env.calls] == ['studio_detail']
    assert 'filename' not in json.dumps(preview)
    assert [v['cid'] for v in preview['preview']['after']['videos']] == [2, 1]
    for body in ({}, {'confirmed': 1}, {'confirmed': True, 'values': {'title': '未预览'}}):
        with pytest.raises(DubForgeError):
            studio.commit('123', preview['id'], body)
    with pytest.raises(DubForgeError):
        studio.commit('456', preview['id'], {'confirmed': True})
    assert studio.commit('123', preview['id'], {'confirmed': True})['status'] == 'done'
    assert studio.commit('123', preview['id'], {'confirmed': True})['status'] == 'done'
    writes = [p for op,p in env.calls if op == 'studio_write']
    assert len(writes) == 1 and writes[0]['version'] == 'version-1'
    assert writes[0]['values'] == {'description': '新简介'}


@pytest.mark.parametrize('change', [dict(kind='delete'),dict(values={'copyright':1}),dict(order=[1]),
    dict(order=[1,1]), dict(order=[True,2]), dict(order=[3,2]), dict(values={'tags': []}), dict(values={'title': ''}),
    dict(publication_id='other'), dict(bvid='../config.yaml'), dict(kind={}), dict(kind=[])])
def test_invalid_edits_and_deleting_or_replacing_parts_rejected(env, change):
    with pytest.raises(DubForgeError):
        studio.prepare('123', {**dict(bvid=BV, kind='edit', values={'title':'新标题'}), **change})
    assert not any(op == 'studio_write' for op,_ in env.calls)


def test_ownership_and_noop_checked_before_plan(env):
    with pytest.raises(DubForgeError, match='没有需要'):
        studio.prepare('123', dict(bvid=BV,kind='edit',values={}))
    env.detail['mid'] = '456'
    with pytest.raises(DubForgeError, match='归属'):
        plan_edit(env)


def test_timeout_durable_no_replay_and_read_only_reconciliation(env, monkeypatch):
    plan = plan_edit(env)
    def timeout(op, payload, **kwargs):
        if op == 'studio_write':
            raise transport.TransportError('网络中断')
        return env.remote(op,payload,**kwargs)
    monkeypatch.setattr(transport,'call',timeout)
    with pytest.raises(DubForgeError) as e:
        studio.commit('123',plan['id'],{'confirmed':True})
    assert e.value.code == 'PUBLISH_WRITE_UNKNOWN'
    db.reset_engine()
    assert studio.changes('123')[0]['status'] == 'unknown'
    with pytest.raises(DubForgeError):
        plan_edit(env)
    with pytest.raises(DubForgeError):
        studio.commit('123',plan['id'],{'confirmed':True})
    assert studio.reconcile('123',plan['id'],{})['status'] == 'unknown'
    env.detail['settings']['description'] = '新简介'
    env.detail['videos'].reverse()
    assert studio.reconcile('123',plan['id'],{})['status'] == 'done'


@pytest.mark.parametrize('code',['PUBLISH_WRITE_NOT_SENT','PUBLISH_WRITE_REJECTED','PUBLISH_REMOTE_CHANGED'])
def test_definite_failure_requires_new_preview_not_replay(env,monkeypatch,code):
    plan=plan_edit(env)
    monkeypatch.setattr(transport,'call',lambda *a,**k: (_ for _ in ()).throw(transport.TransportError('明确失败',code=code)))
    with pytest.raises(DubForgeError):
        studio.commit('123',plan['id'],{'confirmed':True})
    assert studio.changes('123')[0]['status'] == 'failed'
    with pytest.raises(DubForgeError):
        studio.commit('123',plan['id'],{'confirmed':True})


def test_inflight_blocks_parallel_commit_plan_and_manual_release(env,monkeypatch):
    plan=plan_edit(env); started=threading.Event();finish=threading.Event()
    def remote(op,payload,**kwargs):
        if op=='studio_write':
            started.set();assert finish.wait(5)
        return env.remote(op,payload,**kwargs)
    monkeypatch.setattr(transport,'call',remote)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(studio.commit,'123',plan['id'],{'confirmed':True})
        assert started.wait(5)
        try:
            for action in [lambda:studio.commit('123',plan['id'],{'confirmed':True}),lambda:plan_edit(env),
                           lambda:studio.reconcile('123',plan['id'],{'confirmed_not_applied':True})]:
                with pytest.raises(DubForgeError):action()
        finally:finish.set()
        assert future.result()['status']=='done'


def uploaded(env):
    record=Publication(project_uid=env.uid,status='uploaded',snapshot=service._json(dict(account_id='123',origins={},
        settings={**service.DEFAULTS,'declaration_id':1},parts=[dict(artifact_id=3,title='新配音版',has_dubbing=True)])),
        transfers=service._json({'3':dict(cid=3,filename='new-upload')}))
    with Session(db.get_engine()) as session:
        session.add(record);session.commit();session.refresh(record);return record.id


def test_append_uses_own_uploaded_receipts_and_consumes_publication(env):
    key=uploaded(env)
    plan=studio.prepare('123',dict(bvid=BV,kind='append',publication_id=key))
    assert len(plan['preview']['after']['videos']) == 3
    studio.commit('123',plan['id'],{'confirmed':True})
    row=service.view(key)
    assert row['receipt']['bvid']==BV and row['status']=='submitted'
    assert row['snapshot']['update_target']==BV
    assert row['snapshot']['settings']['season_id'] is None
    with pytest.raises(DubForgeError):
        studio.prepare('123',dict(bvid=BV,kind='append',publication_id=key))


@pytest.mark.parametrize('copyright,declaration', [(3, -1), (2, -1), (3, 1)])
def test_append_preserves_target_declaration_and_allows_confirmation_after_advice(env, copyright, declaration):
    env.detail['settings'].update(copyright=copyright, declaration_id=declaration)
    key = uploaded(env)
    plan = studio.prepare('123', dict(bvid=BV, kind='append', publication_id=key))
    assert plan['preview']['after']['settings'] == env.detail['settings']
    assert bool(plan['preview']['warnings']) is (declaration != 1)
    assert not any(op == 'studio_write' for op, _ in env.calls)
    assert studio.commit('123', plan['id'], {'confirmed': True})['status'] == 'done'
    payload = next(p for op, p in env.calls if op == 'studio_write')
    assert payload['values'] == {}


def test_append_metadata_unavailable_is_advisory_and_does_not_edit_declaration(env, monkeypatch):
    key = uploaded(env)
    def unavailable(op, payload, **kwargs):
        if op == 'metadata':
            raise transport.TransportError('账号选项暂时读取失败')
        return env.remote(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', unavailable)
    plan = studio.prepare('123', dict(bvid=BV, kind='append', publication_id=key))
    assert len(plan['preview']['warnings']) == 1
    assert plan['preview']['after']['settings']['declaration_id'] == 1
    assert studio.commit('123', plan['id'], {'confirmed': True})['status'] == 'done'


def test_unknown_append_blocks_other_submit_copy_and_save(env,monkeypatch):
    key=uploaded(env)
    plan=studio.prepare('123',dict(bvid=BV,kind='append',publication_id=key))
    monkeypatch.setattr(transport,'call',lambda *a,**k: (_ for _ in ()).throw(transport.TransportError('超时')))
    with pytest.raises(DubForgeError):studio.commit('123',plan['id'],{'confirmed':True})
    for action in [lambda:service.enqueue(key,{'action':'submit'}),lambda:service.copy_draft(key),
                   lambda:service.save(key,{'overrides':{},'parts':[]})]:
        with pytest.raises(DubForgeError) as e:action()
        assert e.value.code=='PUBLISH_WRITE_UNKNOWN'


def test_collection_preview_and_template_import_preserve_other_settings(env):
    plan=studio.prepare('123',dict(bvid=BV,kind='collection',values={'season_id':4,'section_id':5}))
    assert plan['preview']['after']['collection']['title']=='旅行合集'
    with pytest.raises(DubForgeError):
        studio.prepare('123',dict(bvid=BV,kind='collection',values={'season_id':4,'section_id':99}))
    result=studio.import_template('123',dict(id=1,fields=['tags'],confirmed=True))
    from dubforge.configuration import resolve
    assert resolve(key=result['templates'][0]['key'])['overrides']=={'publish.tags':['旅行'],'publish.account_id':'123'}
    with pytest.raises(DubForgeError,match='已导入'):
        studio.import_template('123',dict(id=1,fields=['title'],confirmed=True))


@pytest.mark.parametrize('action,body',[('archives',{'page':0}),('archives',{'status':'bad'}),('archives',{'status':[]}),
    ('comments',{'bvid':BV,'limit':201}),('comments',{'bvid':BV,'limit':True}),
    ('subtitle',{'bvid':BV,'cid':1,'subtitle_id':'../x'}),('delete',{}),('tags',{'credential':'secret'})])
def test_read_whitelist_bounded_before_network(env,action,body):
    with pytest.raises(DubForgeError):studio.read('123',action,body)
    assert not env.calls


def test_malformed_template_fields_and_reconciliation_body_have_domain_errors(env):
    for body in (None, [], {'id':1,'fields':[{}],'confirmed':True}):
        with pytest.raises(DubForgeError):
            studio.import_template('123',body)
    for body in (None, [], {'confirmed_not_applied':1}):
        with pytest.raises(DubForgeError):
            studio.reconcile('123','unused',body)
    assert not env.calls


def test_comment_import_is_account_scoped_merge_and_no_pipeline_invalidation(env):
    data=studio.read('123','comments',{'bvid':BV,'limit':20})
    body=dict(import_key=data['import_key'],project_uid=env.uid,confirmed=True)
    with pytest.raises(DubForgeError):imports.apply('456',body)
    before=[s.model_dump() for s in crud.get_steps(crud.get_project(env.uid).id)]
    imports.apply('123',body);imports.apply('123',body)
    state=comment_studio.view(env.uid)
    assert len(state['rows'])==1 and state['rows'][0]['text']=='旅行真棒！'
    assert before==[s.model_dump() for s in crud.get_steps(crud.get_project(env.uid).id)]


def test_subtitle_import_keeps_products_until_selected_and_no_asr(env):
    data=dict(bvid=BV,cid=1,language='en',lines=[dict(start=0,end=2,text='Hello')])
    staged=imports.stage('123','subtitle',data)
    root=crud.project_dir(env.uid)
    imports.apply('123',dict(import_key=staged['import_key'],project_uid=env.uid,confirmed=True))
    assert not (root/'srt/src.srt').exists()
    from dubforge.pipeline.step_transcribe import run
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import compute_fingerprint
    project=crud.get_project(env.uid)
    deps=Dependencies(project);deps.overrides['asr.input_mode']='platform_subtitle'
    assert deps.inputs('transcribe')==[]
    fingerprint=compute_fingerprint(project,'transcribe',dependencies=deps)
    writes={}
    ctx=SimpleNamespace(pdir=root,cfg=lambda k,d=None:'platform_subtitle' if k=='asr.input_mode' else d,
        check_cancel=lambda:None,register_file=lambda *a:None,write_json=lambda k,v:writes.update({k:v}),log=lambda _:None)
    run(ctx)
    assert 'Hello' in (root/'srt/src.srt').read_text(encoding='utf-8')
    assert writes['words.json']==[] and writes['transcribe.json']['model']=='platform_subtitle'
    data['lines'][0]['text']='Different'
    staged=imports.stage('123','subtitle',data)
    imports.apply('123',dict(import_key=staged['import_key'],project_uid=env.uid,confirmed=True))
    assert fingerprint!=compute_fingerprint(project,'transcribe',dependencies=deps)


@pytest.mark.parametrize('line',[dict(start=-1,end=2,text='x'),dict(start=2,end=1,text='x'),
    dict(start=0,end=float('nan'),text='x'),dict(start=True,end=2,text='x'),dict(start=0,end=2,text='')])
def test_bad_subtitles_never_replace_import(env,line):
    data=imports.stage('123','subtitle',{'lines':[line]})
    with pytest.raises(DubForgeError):
        imports.apply('123',dict(import_key=data['import_key'],project_uid=env.uid,confirmed=True))
    assert not (crud.project_dir(env.uid)/'imports/bilibili_subtitle.json').exists()


def test_studio_api_same_origin_and_no_secret(env):
    from app.main import app
    with TestClient(app) as client:
        r=client.post('/api/publishing/accounts/123/studio/changes',json=dict(bvid=BV,kind='edit',values={'title':'新标题'}))
        assert r.status_code==200 and 'credential' not in r.text and 'filename' not in r.text
        r=client.post('/api/publishing/accounts/123/studio/changes/'+r.json()['id']+'/commit',
                      json={'confirmed':True},headers={'sec-fetch-site':'cross-site'})
        assert r.status_code==400
    assert not any(op=='studio_write' for op,_ in env.calls)
