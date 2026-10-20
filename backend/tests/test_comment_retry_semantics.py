"""只补普通失败与暂缓，审核缓存的归因和依据明确；全部使用隔离项目与伪模型。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio as cs, config, configuration
from dubforge.domain import crud
from dubforge.errors import StepFailedError
from dubforge.utils.fs import atomic_write_bytes
from test_comment_studio import uid, seed, fake_translation
from test_comment_diagnostics import model, ratings


def record(uid, *, translations=(), issues=None):
    project = crud.get_project(uid)
    rows = {row['id']: row for row in cs.view(uid)['rows']}
    fingerprint = cs._processing_key('translate', '', cs._context(project), cs._profiles())
    doc = {'translations': {ident: dict(source_hash=rows[ident]['source_hash'], text_zh='已有完整中文')
                            for ident in translations}, 'ai_issues': {'translate': {}}}
    for ident, status in (issues or {}).items():
        doc['ai_issues']['translate'][ident] = dict(source_hash=rows[ident]['source_hash'], fingerprint=fingerprint,
            status=status, code='LLM_CONTENT_REJECTED' if status == 'blocked' else 'LLM_OUTPUT_INVALID',
            message='原始服务商或生成错误')
    cs._write(project, doc)
    return doc


def test_legacy_translation_fingerprint_and_missing_scope_remain_current(uid, monkeypatch):
    profiles = [{'name': 'fixture', 'model': 'fixture'}]
    monkeypatch.setattr(cs, '_profiles', lambda: profiles)
    doc = record(uid, issues={'1': 'blocked'})
    # 已发布 comment-lines-v1 的固定样本，防止无意改协议使旧审核记录全量失效。
    legacy_key = 'f623672b2be03ed8ba917fa9c05bce913b8db6a97fabe95132ad517eab44e905'
    doc['ai_issues']['translate']['1']['fingerprint'] = legacy_key
    cs._write(crud.get_project(uid), doc)
    before = cs._path(crud.get_project(uid)).read_bytes()
    assert cs._processing_key('translate', '无关评分要求', {'title': '未用于翻译的背景'}, profiles) == legacy_key
    issue = cs.view(uid)['rows'][1]['ai_issues']['translate']
    assert issue['status'] == 'blocked' and issue['request_scope'] == 'unknown'
    assert cs._path(crud.get_project(uid)).read_bytes() == before


def test_retry_only_fills_failed_and_deferred_and_reports_blocked_separately(uid, monkeypatch):
    seed(uid, 5)
    record(uid, translations=['0'], issues={'1': 'failed', '2': 'blocked', '3': 'deferred'})
    calls = []
    def translate(prompt, **kwargs):
        from test_comment_studio import request_payload
        calls.extend(row['text'] for row in request_payload(prompt)['comments'])
        return fake_translation(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', translate)
    state = cs.run(uid, 'translate', retry_failed=True, ids=['0', '1', '2', '3', '4'])
    assert calls == ['Original opinion 1', 'Original opinion 3', 'Original opinion 4']
    assert {key: state['job'][key] for key in ('processed', 'cached', 'blocked', 'blocked_cached', 'failed', 'deferred', 'retryable')} == {
        'processed': 3, 'cached': 1, 'blocked': 1, 'blocked_cached': 1, 'failed': 0, 'deferred': 0, 'retryable': 0}
    assert state['job']['status'] == 'partial' and '1 条所在请求被审核拦截' in state['job']['warning']
    issue = state['rows'][2]['ai_issues']['translate']
    assert issue['request_scope'] == 'unknown' and issue['retryable'] is False
    assert 'request_scope' not in cs._read(crud.get_project(uid))['ai_issues']['translate']['2']
    assert state['rows'][0]['text_zh'] == '已有完整中文' and state['rows'][2]['text_zh'] == ''


def test_normal_run_keeps_failed_cached_but_explicit_api_retry_fills_only_missing(uid, monkeypatch):
    record(uid, translations=['0'], issues={'1': 'failed', '2': 'deferred'})
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    state = cs.run(uid, 'translate')
    assert state['job']['processed'] == 1 and state['job']['failed'] == 1 and state['job']['retryable'] == 1
    assert state['rows'][1]['ai_issues']['translate']['retryable'] is True
    client = TestClient(app)
    response = client.post(f'/api/projects/{uid}/comments/translate', json={'retry_failed': True, 'ids': ['0', '1', '2']})
    assert response.status_code == 200
    job = response.json()['job']
    assert job['status'] == 'done' and job['processed'] == 1 and job['cached'] == 2


def test_all_known_blocked_retry_has_no_requests_and_explicit_error(uid, monkeypatch):
    record(uid, issues={str(i): 'blocked' for i in range(3)})
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('已知审核拦截不得泛化重试'))
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'translate', retry_failed=True)
    assert error.value.code == 'COMMENTS_BLOCKED'
    assert '没有重新提交' in error.value.message and '修改模型配置' in error.value.suggestion
    job = cs.view(uid)['job']
    assert job['cached'] == 0 and job['blocked_cached'] == 3 and job['status'] == 'failed'
    state = cs.run(uid, 'translate', retry_failed=True, ids=['1'])
    assert state['job']['status'] == 'partial' and state['job']['blocked'] == 1 and state['job']['cached'] == 0


def test_strict_known_blocked_stops_before_sending_other_pending_comments(uid, monkeypatch):
    record(uid, translations=['0'], issues={'1': 'blocked'})
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('严格模式遇已有拒绝必须先停止'))
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'translate', retry_failed=True, failure_policy='stop')
    assert error.value.code == 'COMMENTS_BLOCKED'
    job = cs.view(uid)['job']
    assert job['cached'] == 1 and job['blocked_cached'] == 1 and job['deferred'] == 1 and job['retryable'] == 1


def test_model_change_rechecks_blocked_without_regenerating_completed_translation(uid, monkeypatch):
    record(uid, translations=['0'], issues={'1': 'blocked', '2': 'blocked'})
    config.set_key('llm.profiles', [{'name': '更换模型', 'model': 'new-fixture', 'api_key': 'private-fixture'}])
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    state = cs.run(uid, 'translate', retry_failed=True)
    assert state['job']['processed'] == 2 and state['job']['cached'] == 1 and state['job']['status'] == 'done'
    assert state['rows'][0]['text_zh'] == '已有完整中文'
    assert 'private-fixture' not in cs._path(crud.get_project(uid)).read_text(encoding='utf-8')


def test_strict_batch_rejection_is_bound_to_all_request_sources_and_not_individual(uid, monkeypatch):
    configuration.save('project:'+uid, dict(revision=configuration.resolve(uid)['revision'],
        updates={'comments.prompt': '观众反馈'}))
    calls = model(monkeypatch, lambda *_: (_ for _ in ()).throw(StepFailedError('审核拒绝', code='LLM_CONTENT_REJECTED',
        detail={'providers': [{'profile': '伪模型', 'code': 'LLM_CONTENT_REJECTED', 'detail': {'finish_reason': 'sensitive'}}]})))
    with pytest.raises(cs.CommentError):
        cs.run(uid, 'filter', prompt='观众反馈', failure_policy='stop')
    state = cs.view(uid)
    assert len(calls) == 1
    assert all(row['ai_issues']['filter']['request_scope'] == 'batch' for row in state['rows'])
    issue = state['rows'][0]['ai_issues']['filter']
    assert set(issue['request_sources']) == {'0', '1', '2'}
    assert issue['cause'][0]['finish_reason'] == 'sensitive'
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'filter', prompt='观众反馈', failure_policy='stop', retry_failed=True)
    assert error.value.code == 'COMMENTS_BLOCKED' and len(calls) == 1
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    info = json.loads(path.read_text(encoding='utf-8'))
    info['comments']['items'][1]['text'] = 'Changed legitimate original'
    atomic_write_bytes(path, json.dumps(info).encode())
    calls = model(monkeypatch, lambda _, payload: ratings(payload['comments']))
    state = cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 1 and len(calls[0]) == 3 and state['job']['status'] == 'done'


def test_issue_cause_survives_zero_request_run_without_saving_uncontrolled_details(uid, monkeypatch):
    config.set_key('llm.profiles', [dict(name='伪模型', model='fixture', api_key='private-fixture-secret')])
    def rejected(*args, **kwargs):
        raise StepFailedError('本条服务商审核拒绝', code='LLM_CONTENT_REJECTED', detail={
            'code': 'SensitiveContentDetected', 'finish_reason': 'sensitive',
            'request': '必须忽略的请求正文', 'api_key': 'private-fixture-secret'})
    monkeypatch.setattr(cs, 'chat_validated', rejected)
    state = cs.run(uid, 'translate', ids=['1'])
    expected = state['rows'][1]['ai_issues']['translate']['cause']
    assert expected == [{'code': 'LLM_CONTENT_REJECTED', 'provider_code': 'SensitiveContentDetected', 'finish_reason': 'sensitive'}]
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('重试不能再次请求'))
    state = cs.run(uid, 'translate', ids=['1'], retry_failed=True)
    assert state['rows'][1]['ai_issues']['translate']['cause'] == expected
    saved = cs._path(crud.get_project(uid)).read_text(encoding='utf-8')
    assert '必须忽略的请求正文' not in saved and 'private-fixture-secret' not in saved


@pytest.mark.parametrize('updates', [{'request_scope': []}, {'request_scope': {}}, {'request_sources': []},
                                   {'request_sources': {'1': []}}])
def test_invalid_issue_scope_and_sources_are_document_error(uid, updates):
    doc = record(uid, issues={'1': 'blocked'})
    doc['ai_issues']['translate']['1'].update(updates)
    cs._write(crud.get_project(uid), doc)
    with pytest.raises(cs.CommentError) as error:
        cs.view(uid)
    assert error.value.code == 'COMMENTS_DOCUMENT_INVALID'
