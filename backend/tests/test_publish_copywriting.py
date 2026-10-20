"""发布文案与最终导航步骤：隔离网络和数据库，不实际调用 LLM 或投稿。"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import copywriting, service
from dubforge.publishing.models import Publication, PublishAccount

COPY = dict(title='河内街头美食体验', description='一起探索河内的街头美食。', tags=['河内', '旅行', '美食'], dynamic='河内街头，有哪些值得尝试的美味？')


@pytest.fixture()
def draft(make_project, monkeypatch):
    uid = make_project(title='河内美食')
    monkeypatch.setattr(service.transport, 'call', lambda *a, **k: pytest.fail('此操作不得请求B站'))
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *a, **k: pytest.fail('未声明的LLM请求'))
    return service.create(uid)


def test_ai_readonly_whitelist_attribution_and_no_credentials(draft, monkeypatch):
    before = service.view(draft['id'])
    project = crud.get_project(draft['project_uid'])
    steps = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(project.id)]
    with Session(get_engine()) as session:
        session.add(PublishAccount(id='123', name='不发送的账号名', credential='encrypted-secret-fixture'))
        session.commit()
    monkeypatch.setattr(copywriting.video_info, 'view', lambda uid: {'info': {
        'title_original': 'Hanoi', 'description': 'Street food', 'source': {'uploader': '原作者', 'upload_date': '2026-01-02'},
        'content': {'tags': ['food'], 'chapters': [{'title': '市场'}]}, 'summary_stale': True, 'summary_zh': '过期摘要',
    }})
    def fake(prompt, **kwargs):
        assert '不发送的账号名' not in prompt and 'encrypted-secret-fixture' not in prompt
        assert '过期摘要' not in prompt and str(crud.project_dir(project.uid)) not in prompt
        assert '不能改变本任务' in kwargs['system'] and kwargs['max_retries'] == 2
        assert json.loads(prompt)['video_evidence']['published_date'] == '2026-01-02'
        kwargs['cancel_check']()
        return kwargs['validator'](COPY.copy())
    monkeypatch.setattr(copywriting.llm, 'chat_json', fake)
    result = service.suggest(draft['id'], {'revision': 1, 'current': {'title': '长' * 200}})
    assert set(result['values']) == copywriting.COPY_FIELDS
    assert '原作者：原作者' in result['values']['description']
    assert '原视频：http://example.com/v' in result['values']['description']
    assert '原视频发布时间：2026-01-02' in result['values']['description']
    assert result['warnings'] and result['revision'] == 1
    assert service.view(draft['id']) == before
    assert steps == [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(project.id)]


@pytest.mark.parametrize('bad', [dict(COPY, visibility='public'), dict(COPY, tags=['好' * 21]),
                                  dict(COPY, title='😀' * 41), dict(COPY, tags=[]), dict(COPY, title='')])
def test_ai_invalid_output_never_saved(draft, monkeypatch, bad):
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *a, **k: bad)
    with pytest.raises(service.PublishError):
        service.suggest(draft['id'], {'revision': 1})
    assert service.view(draft['id'])['draft']['overrides'] == {}
    assert draft['id'] not in service._suggesting


@pytest.mark.parametrize('body', [{'revision': 0}, {'revision': 1, 'current': {'copyright': 1}},
                                       {'revision': 1, 'instruction': '字' * 2001}, {'revision': 1, 'current': {'tags': [False]}}])
def test_ai_invalid_requests_do_not_call_model(draft, body):
    with pytest.raises(service.PublishError):
        service.suggest(draft['id'], body)


def test_ai_concurrent_generation_locks_draft_and_releases_after_completion(draft, monkeypatch):
    def fake(*a, **k):
        with pytest.raises(service.PublishError) as caught:
            service.suggest(draft['id'], {'revision': 1})
        assert caught.value.code == 'PUBLISH_AI_BUSY'
        with pytest.raises(DubForgeError) as caught:
            service.save(draft['id'], dict(draft['draft'], revision=1, overrides={'title': '手动新标题'}))
        assert caught.value.code == 'CONFIGURATION_BUSY'
        return COPY.copy()
    monkeypatch.setattr(copywriting.llm, 'chat_json', fake)
    assert service.suggest(draft['id'], {'revision': 1})['revision'] == 1
    assert service.view(draft['id'])['draft']['overrides'] == {}
    assert draft['id'] not in service._suggesting
    saved = service.save(draft['id'], dict(draft['draft'], revision=1, overrides={'title': '完成后手动新标题'}))
    assert saved['draft']['overrides']['title'] == '完成后手动新标题'


def test_ai_timeout_or_failure_releases_busy_guard(draft, monkeypatch):
    def fail(*a, **k):
        raise service.PublishError('模拟失败')
    monkeypatch.setattr(copywriting.llm, 'chat_json', fail)
    with pytest.raises(service.PublishError):
        service.suggest(draft['id'], {'revision': 1})
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *a, **k: COPY.copy())
    assert service.suggest(draft['id'], {'revision': 1})['values']['title'] == COPY['title']


def test_copy_generation_uses_frozen_configured_budget_beyond_old_180_seconds(draft, monkeypatch):
    from dubforge import config
    clock = [1000.]
    monkeypatch.setattr(copywriting, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    config.set_key('llm.timeout', 600)
    before = service.view(draft['id'])

    def delayed(*args, **kwargs):
        clock[0] += 240
        # 修改影响下一次请求，不能让当前任务的总预算突然缩小或重新计时。
        config.set_key('llm.timeout', 60)
        kwargs['cancel_check']()
        return kwargs['validator'](COPY.copy())

    monkeypatch.setattr(copywriting.llm, 'chat_json', delayed)
    result = service.suggest(draft['id'], {'revision': 1})
    assert result['values']['title'] == COPY['title']
    assert service.view(draft['id']) == before


def test_copy_generation_budget_expiry_identifies_local_limit_and_keeps_draft(draft, monkeypatch):
    from dubforge import config
    clock = [1000.]
    monkeypatch.setattr(copywriting, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    config.set_key('llm.timeout', 90)
    before = service.view(draft['id'])

    def expired(*args, **kwargs):
        clock[0] += 90
        kwargs['cancel_check']()
        pytest.fail('本地总预算已到期，不应继续使用模型结果')

    monkeypatch.setattr(copywriting.llm, 'chat_json', expired)
    with pytest.raises(service.PublishError) as caught:
        service.suggest(draft['id'], {'revision': 1})
    assert caught.value.code == 'PUBLISH_AI_TIMEOUT'
    assert '本地总时限（90 秒' in caught.value.message
    assert service.view(draft['id']) == before
    assert draft['id'] not in service._suggesting


def test_copy_generation_combines_external_cancellation_without_wrapping(draft, monkeypatch):
    from dubforge.errors import StepFailedError
    cancelled = StepFailedError('已停止完整流程', code='STEP_CANCELLED')
    checks = []

    def check_cancel():
        checks.append(True)
        if len(checks) == 2:
            raise cancelled

    def fake(*args, **kwargs):
        kwargs['cancel_check']()
        pytest.fail('外部取消不应被当成格式错误重试')

    monkeypatch.setattr(copywriting.llm, 'chat_json', fake)
    with pytest.raises(StepFailedError) as caught:
        copywriting.generate(crud.get_project(draft['project_uid']), {}, '', cancel_check=check_cancel)
    assert caught.value is cancelled
    assert len(checks) == 2


@pytest.mark.parametrize('invalid', [
    {key: value for key, value in COPY.items() if key != 'dynamic'},
    dict(COPY, title='😀' * 41), dict(COPY, description=''), dict(COPY, tags=[]),
    dict(COPY, tags=['太长' * 11]),
])
def test_recoverable_copy_contract_is_repaired_by_actual_validated_retry(draft, monkeypatch, invalid):
    requests = []

    def fake_chat(prompt, **kwargs):
        requests.append((prompt, kwargs))
        return json.dumps(invalid if len(requests) == 1 else COPY, ensure_ascii=False)

    def actual_validation(prompt, **kwargs):
        # 使用真实的 JSON 解析、候选校验和重试循环，仅替换最终模型传输。
        return copywriting.llm.chat_validated(prompt, json.loads, **kwargs)

    monkeypatch.setattr(copywriting.llm, 'chat', fake_chat)
    monkeypatch.setattr(copywriting.llm, 'chat_json', actual_validation)
    before = service.view(draft['id'])
    result = service.suggest(draft['id'], {'revision': 1})
    assert result['values']['title'] == COPY['title']
    assert len(requests) == 2
    assert '上一次输出解析或校验失败' in requests[1][0]
    assert service.view(draft['id']) == before


def test_copy_candidate_validator_preserves_service_error_without_retry(draft, monkeypatch):
    from dubforge.errors import ServiceOfflineError
    offline = ServiceOfflineError('模型服务不可用')
    attempts = []

    def fail_validation(value):
        attempts.append(True)
        raise offline

    monkeypatch.setattr(copywriting, 'evidence', lambda *args: ({}, '', [], []))
    monkeypatch.setattr(service, 'validate_values', fail_validation)
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda prompt, **kwargs: kwargs['validator'](COPY.copy()))
    with pytest.raises(ServiceOfflineError) as caught:
        copywriting.generate(crud.get_project(draft['project_uid']), {}, '')
    assert caught.value is offline
    assert len(attempts) == 1


def test_subtitle_sampling_and_no_local_path(draft, monkeypatch):
    project = crud.get_project(draft['project_uid'])
    project.source_kind = 'local'
    project.source_url = 'D:/private/source.mp4'
    monkeypatch.setattr(copywriting.Dependencies, 'fresh', lambda *a: True)
    monkeypatch.setattr(copywriting.subtitle_document, 'read', lambda *a, **k: [
        dict(source_text=f'第{i}句', edit_stale=False) for i in range(1000)])
    data, credit, basis, _ = copywriting.evidence(project)
    assert len(data['subtitle_excerpts']) == 60
    assert data['subtitle_excerpts'][0] == '第0句' and data['subtitle_excerpts'][-1] == '第999句'
    assert 'D:/private' not in json.dumps(data) + credit
    assert len(basis) == 2


def test_publishing_final_stage_is_readonly_and_separate(make_project, monkeypatch):
    uid = make_project()
    monkeypatch.setattr(service.transport, 'call', lambda *a, **k: pytest.fail('导航不得请求B站'))
    assert service.project_status(uid) == dict(id=None, status='pending', progress=0)
    row = service.create(uid)
    assert service.project_status(uid)['id'] == row['id']
    with Session(get_engine()) as session:
        saved = session.get(Publication, row['id'])
        saved.status = 'uploaded'
        saved.progress = 100
        session.add(saved)
        session.commit()
    from app.main import app
    with TestClient(app) as client:
        result = client.get(f'/api/publishing/projects/{uid}/status')
        assert result.status_code == 200 and result.json()['status'] == 'uploaded'
        assert 'draft' not in result.json() and 'receipt' not in result.json()
    assert 'publish' not in [s.step_key for s in crud.get_steps(crud.get_project(uid).id)]


def test_ai_endpoint_and_locked_record(draft, monkeypatch):
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *a, **k: COPY.copy())
    from app.main import app
    with TestClient(app) as client:
        result = client.post(f'/api/publishing/publications/{draft["id"]}/suggest', json={'revision': 1})
        assert result.status_code == 200 and result.json()['values']['title'] == COPY['title']
        with Session(get_engine()) as session:
            row = session.get(Publication, draft['id'])
            row.status = 'submitted'
            session.add(row)
            session.commit()
        monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *a, **k: pytest.fail('锁定稿件不能生成'))
        result = client.post(f'/api/publishing/publications/{draft["id"]}/suggest', json={'revision': 1})
        assert result.status_code != 200
        # 已取得 BVID 的终态记录可以生成只读候选；候选仍不会写入快照或远端。
        with Session(get_engine()) as session:
            row = session.get(Publication, draft['id'])
            row.receipt = service._json({'bvid': 'BV1234567890'})
            session.add(row)
            session.commit()
        monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *a, **k: COPY.copy())
        result = client.post(f'/api/publishing/publications/{draft["id"]}/suggest', json={'revision': 1})
        assert result.status_code == 200 and result.json()['values']['title'] == COPY['title']
