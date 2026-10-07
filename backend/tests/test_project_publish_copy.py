"""项目发布文案：隔离数据库、网络与模型，只生成候选，不创建稿件。"""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from dubforge import configuration
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import copywriting, project_copy, service
from dubforge.publishing.models import Publication, PublishAccount


COPY = dict(title='中国高铁与北京美食', description='跟随旅程探索高铁服务与北京美食。',
            tags=['旅行', '高铁', '北京'], dynamic='一起探索旅行途中的新体验')


@pytest.fixture
def context(make_project, monkeypatch):
    uid = make_project(title='原项目标题')
    info = dict(title_original='Original title', title_zh='自动中文标题',
                description='Platform description', summary_zh='已有中文视频概要', summary_stale=False,
                source=dict(uploader='原作者', release_date='2026-10-01', upload_date='2026-09-30'),
                content=dict(tags=['travel'], chapters=[dict(title='Train')], thumbnail=''))
    monkeypatch.setattr(service.video_info, 'view', lambda _: dict(info=copy.deepcopy(info)))
    monkeypatch.setattr(service.transport, 'call', lambda *_a, **_kw: pytest.fail('文案操作不得请求B站'))
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *_a, **_kw: pytest.fail('未显式声明的模型调用'))
    return SimpleNamespace(uid=uid, info=info, make_project=make_project)


def request(uid, **extra):
    state = service.copy_preview(uid)
    return dict(revision=state['revision'], fingerprint=state['fingerprint'],
                content_fingerprint=state['content_fingerprint'], current=COPY.copy(), **extra)


def save(key, updates=None, reset=None):
    state = configuration.resolve(key=key)
    return configuration.save(key, dict(revision=state['revision'], fingerprint=state['fingerprint'],
                                        updates=updates or {}, reset=reset or []))


def persisted():
    with Session(get_engine()) as session:
        configurations = [(r.key, r.values, r.revision, r.parent)
                          for r in session.exec(select(ConfigurationScope)).all()]
        publications = [(r.id, r.draft, r.revision, r.snapshot)
                        for r in session.exec(select(Publication)).all()]
    return configurations, publications


def external_configuration_change(uid, title):
    """模拟应用之外的写入，保留候选对资料变化的防御检查。"""
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + uid) or ConfigurationScope(key='project:' + uid)
        row.values = json.dumps({**json.loads(row.values), 'publish.title': title})
        row.revision += 1
        session.add(row)
        session.commit()


def test_preview_is_light_readonly_and_matches_new_draft_base(context, monkeypatch):
    monkeypatch.setattr(copywriting, 'evidence', lambda _: pytest.fail('GET不得读取字幕证据'))
    before = persisted()
    state = service.copy_preview(context.uid)
    assert persisted() == before
    assert state['project_uid'] == context.uid and state['revision'] == 0
    assert state['configured'] == state['parent_configured'] == []
    assert state['content']['title'] == state['values']['title'] == '自动中文标题'
    assert state['content']['source'] == 'http://example.com/v'
    assert state['content']['description'].startswith('已有中文视频概要')
    assert '原作者：原作者' in state['content']['description']
    assert '原视频发布时间：2026-10-01' in state['content']['description']
    assert state['source_published_date'] == '2026-10-01'
    assert state['sources']['title'] == state['content_sources']['title'] == {'kind': 'content'}
    assert state['values']['tags'] == [] and 'tags' not in state['content']
    assert set(state['values']) == set(service.DEFAULTS)
    assert len(state['fingerprint']) == len(state['content_fingerprint']) == 64
    draft = service.create(context.uid)
    assert draft['draft']['content'] == state['content']
    assert service.view(draft['id'])['settings']['title'] == state['values']['title']


def test_parent_explicit_empty_survives_project_overlap_and_reset(context):
    save('global', {'publish.title': '', 'publish.tags': [], 'publish.dynamic': ''})
    template = configuration.create_template('系列设置')
    save(template['key'], {'publish.description': '系列简介', 'publish.title': '系列标题'})
    plan = configuration.binding_plan([context.uid], template['key'][7:])
    configuration.bind(dict(uids=[context.uid], template_id=template['key'][7:],
                            fingerprint=plan['fingerprint'], confirmed=True))
    save('project:' + context.uid, {'publish.title': '', 'publish.description': '项目简介'})
    state = service.copy_preview(context.uid)
    assert state['values']['title'] == '' and state['sources']['title']['kind'] == 'project'
    assert state['values']['tags'] == [] and state['sources']['tags']['kind'] == 'global'
    assert state['values']['description'] == '项目简介'
    assert {'publish.title', 'publish.description', 'publish.tags', 'publish.dynamic'} <= set(state['parent_configured'])
    assert state['content']['title'] == '自动中文标题'  # 手动空值不改自动基础。
    save('project:' + context.uid, reset=['publish.title', 'publish.description'])
    inherited = service.copy_preview(context.uid)
    assert inherited['values']['title'] == '系列标题'
    assert inherited['values']['description'] == '系列简介'
    assert inherited['sources']['title'] == {'kind': 'series', 'name': '系列设置'}
    save(template['key'], reset=['publish.title'])
    assert service.copy_preview(context.uid)['values']['title'] == ''


def test_platform_refresh_does_not_modify_explicit_fields_or_existing_draft(context):
    save('project:' + context.uid, {'publish.title': '手写项目标题', 'publish.tags': []})
    draft = service.create(context.uid)
    before = service.view(draft['id'])
    old = service.copy_preview(context.uid)
    context.info.update(title_zh='刷新后自动标题', summary_zh='刷新后的概要')
    new = service.copy_preview(context.uid)
    assert new['revision'] == old['revision'] and new['fingerprint'] == old['fingerprint']
    assert new['content_fingerprint'] != old['content_fingerprint']
    assert new['content']['title'] == '刷新后自动标题'
    assert new['values']['title'] == '手写项目标题' and new['values']['tags'] == []
    assert service.view(draft['id']) == before


def test_content_fingerprint_ignores_statistics_and_cover_url(context):
    old = service.copy_preview(context.uid)
    context.info['source']['view_count'] = 999
    context.info['content']['thumbnail'] = 'https://fixture.invalid/changed.jpg'
    assert service.copy_preview(context.uid)['content_fingerprint'] == old['content_fingerprint']
    context.info['content']['chapters'][0]['title'] = 'Beijing'
    assert service.copy_preview(context.uid)['content_fingerprint'] != old['content_fingerprint']


def test_stale_summary_is_excluded_from_automatic_description(context):
    context.info['summary_stale'] = True
    state = service.copy_preview(context.uid)
    assert '已有中文视频概要' not in state['content']['description']
    assert '原作者：原作者' in state['content']['description']


def test_project_candidate_is_whitelisted_attributed_and_never_saved(context, monkeypatch):
    with Session(get_engine()) as session:
        session.add(PublishAccount(id='private', name='私密账号', credential='private-credential-fixture'))
        session.commit()
    before = persisted()
    project = crud.get_project(context.uid)
    steps = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(project.id)]
    calls = []

    def model(prompt, **kwargs):
        calls.append(json.loads(prompt))
        assert 'private-credential-fixture' not in prompt and '私密账号' not in prompt
        assert str(crud.project_dir(context.uid)) not in prompt
        assert kwargs['max_retries'] == 2
        kwargs['cancel_check']()
        return kwargs['validator'](COPY.copy())

    monkeypatch.setattr(copywriting.llm, 'chat_json', model)
    result = service.suggest_project(context.uid, request(context.uid, instruction='简洁自然'))
    assert len(calls) == 1 and calls[0]['writing_request'] == '简洁自然'
    assert set(result['values']) == copywriting.COPY_FIELDS
    assert result['values']['title'] == COPY['title']
    assert result['values']['description'].count('原作者：原作者') == 1
    assert result['values']['description'].count('原视频发布时间：2026-10-01') == 1
    assert result['revision'] == service.copy_preview(context.uid)['revision']
    assert result['warnings'] and result['basis']
    assert persisted() == before
    assert steps == [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(project.id)]
    assert context.uid not in project_copy._suggesting


@pytest.mark.parametrize('bad', [
    {'current': {'copyright': 1}}, {'current': {'title': False}},
    {'current': {'tags': [False]}}, {'current': {'tags': ['a' * 101]}},
    {'current': {'title': 'a' * 10001}}, {'instruction': 'a' * 1001},
    {'instruction': 1}, {'account_id': 'private'}, {'current': []},
])
def test_invalid_requests_never_call_model(context, bad):
    body = request(context.uid)
    body.update(bad)
    with pytest.raises(service.PublishError):
        service.suggest_project(context.uid, body)
    assert context.uid not in project_copy._suggesting


@pytest.mark.parametrize('field,value', [('revision', True), ('revision', -1),
                                        ('fingerprint', 'old'), ('content_fingerprint', 'old')])
def test_stale_or_invalid_revision_rejected_before_model(context, field, value):
    body = request(context.uid)
    body[field] = value
    with pytest.raises(service.PublishError) as caught:
        service.suggest_project(context.uid, body)
    assert caught.value.code == 'PUBLISH_CONFLICT'


@pytest.mark.parametrize('mutation', ['configuration', 'metadata', 'subtitles', 'final_evidence_read'])
def test_late_candidates_rejected_and_new_edits_preserved(context, monkeypatch, mutation):
    body = request(context.uid)
    evidence_version = [0]
    evidence_calls = []

    def evidence(_):
        evidence_calls.append(1)
        if mutation == 'final_evidence_read' and len(evidence_calls) == 2:
            external_configuration_change(context.uid, '末次核验期间的新标题')
        return {'subtitle_excerpts': [str(evidence_version[0])]}, 'credit', ['字幕'], []

    def generate(*_args):
        if mutation == 'configuration':
            external_configuration_change(context.uid, '生成期间新编辑')
        elif mutation == 'metadata':
            context.info['title_zh'] = '生成期间新资料'
        elif mutation == 'subtitles':
            evidence_version[0] += 1
        return dict(values=COPY.copy(), basis=['资料'], warnings=[])

    monkeypatch.setattr(copywriting, 'evidence', evidence)
    monkeypatch.setattr(copywriting, 'generate', generate)
    with pytest.raises(service.PublishError) as caught:
        service.suggest_project(context.uid, body)
    assert caught.value.code == 'PUBLISH_CONFLICT'
    assert context.uid not in project_copy._suggesting
    assert persisted()[1] == []
    if mutation == 'configuration':
        assert configuration.resolve(context.uid)['values']['publish.title'] == '生成期间新编辑'
    elif mutation == 'final_evidence_read':
        assert configuration.resolve(context.uid)['values']['publish.title'] == '末次核验期间的新标题'


def test_generation_locks_its_project_but_does_not_hold_global_execution_gate(context, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    other = context.make_project(title='其他项目')

    def generate(project, *_args):
        if project.uid == context.uid:
            entered.set()
            assert release.wait(5)
        return dict(values=COPY.copy(), basis=['资料'], warnings=[])

    monkeypatch.setattr(copywriting, 'generate', generate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.suggest_project, context.uid, request(context.uid))
        try:
            assert entered.wait(5)
            with pytest.raises(service.PublishError) as caught:
                service.suggest_project(context.uid, request(context.uid))
            assert caught.value.code == 'PUBLISH_AI_BUSY'
            assert service.suggest_project(other, request(other))['project_uid'] == other
            with pytest.raises(DubForgeError) as caught:
                save('project:' + context.uid, {'publish.title': '并发保存的手工标题'})
            assert caught.value.code == 'CONFIGURATION_BUSY'
            save('project:' + other, {'publish.title': '其他项目可正常保存'})
        finally:
            release.set()
        assert future.result(timeout=5)['project_uid'] == context.uid
    assert not project_copy._suggesting


@pytest.mark.parametrize('code', ['PUBLISH_AI_TIMEOUT', 'PUBLISH_AI_INVALID'])
def test_model_failure_releases_project_guard_without_changing_values(context, monkeypatch, code):
    before = persisted()

    def fail(*_args):
        raise service.PublishError('模拟生成失败', code=code)

    monkeypatch.setattr(copywriting, 'generate', fail)
    with pytest.raises(service.PublishError) as caught:
        service.suggest_project(context.uid, request(context.uid))
    assert caught.value.code == code and context.uid not in project_copy._suggesting
    assert persisted() == before
    monkeypatch.setattr(copywriting, 'generate', lambda *_args: dict(values=COPY.copy(), basis=[], warnings=[]))
    assert service.suggest_project(context.uid, request(context.uid))['values']['title'] == COPY['title']


@pytest.mark.parametrize('bad', [dict(COPY, copyright=1), dict(COPY, title=''),
                                  dict(COPY, title='😀' * 41), dict(COPY, tags=[])])
def test_actual_generation_rejects_invalid_output_without_saving(context, monkeypatch, bad):
    before = persisted()
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *_args, **_kwargs: bad)
    with pytest.raises(service.PublishError):
        service.suggest_project(context.uid, request(context.uid))
    assert persisted() == before and context.uid not in project_copy._suggesting


def test_api_preview_and_candidate_no_store_no_new_publication(context, monkeypatch):
    from app.main import app

    before = persisted()
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *_args, **_kwargs: COPY.copy())
    with TestClient(app) as client:
        response = client.get(f'/api/publishing/projects/{context.uid}/copy-preview')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        preview = response.json()
        response = client.post(f'/api/publishing/projects/{context.uid}/suggest', json=dict(
            revision=preview['revision'], fingerprint=preview['fingerprint'],
            content_fingerprint=preview['content_fingerprint'], current=COPY.copy()))
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert response.json()['values']['title'] == COPY['title']
        assert response.json()['project_uid'] == context.uid
        stale = client.post(f'/api/publishing/projects/{context.uid}/suggest', json=dict(
            revision=preview['revision'], fingerprint='old'))
        assert stale.status_code == 400 and stale.json()['code'] == 'PUBLISH_CONFLICT'
    assert persisted() == before


def test_optional_content_fingerprint_is_compatible_but_end_check_still_applies(context, monkeypatch):
    body = request(context.uid)
    body.pop('content_fingerprint')
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *_args, **_kwargs: COPY.copy())
    assert service.suggest_project(context.uid, body)['content_fingerprint']
