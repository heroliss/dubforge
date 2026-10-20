"""评论局部失败不丢原文、不伪造评分；全部使用隔离项目与伪模型。"""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio as cs, configuration, config
from dubforge.domain import crud
from dubforge.errors import StepFailedError
from test_comment_studio import uid, seed
from test_comment_diagnostics import model, ratings


@pytest.fixture(autouse=True)
def scoring_rule(uid):
    configuration.save('project:'+uid, dict(revision=configuration.resolve(uid)['revision'], updates={'comments.prompt': '观众反馈'}))


def refusal():
    return StepFailedError('服务商审核拒绝', code='LLM_CONTENT_REJECTED')


def block_one(calls, payload):
    if any(row['text'] == 'Original opinion 1' for row in payload['comments']):
        raise refusal()
    return ratings(payload['comments'])


def test_one_blocked_score_isolated_other_scores_saved_and_bad_cache_not_resent(uid, monkeypatch):
    original = seed(uid).read_bytes()
    calls = model(monkeypatch, block_one)
    state = cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 5 and state['job']['status'] == 'partial'
    assert state['job']['processed'] == 2 and state['job']['skipped'] == 1 and state['job']['deferred'] == 0
    assert {row['id'] for row in state['filter']['assessments']} == {'0', '2'}
    assert state['rows'][1]['ai_issues']['filter']['status'] == 'blocked'
    assert 'score' not in state['rows'][1]['ai_issues']['filter']
    assert (crud.project_dir(uid) / 'artifacts/download/video_info.json').read_bytes() == original
    cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 5  # 相同输入与配置下，跳过已经明确失败的项。
    cs.run(uid, 'filter', prompt='观众反馈', retry_failed=True)
    assert len(calls) == 5  # 普通重试不能绕过同一请求依据的明确审核拦截。
    state = cs.view(uid)
    assert state['job']['blocked_cached'] == 1 and state['job']['cached'] == 2
    assert state['rows'][1]['ai_issues']['filter']['retryable'] is False
    assert state['rows'][1]['ai_issues']['filter']['request_scope'] == 'item'


def test_change_provider_retries_failure_but_keeps_successful_scores(uid, monkeypatch):
    model(monkeypatch, block_one)
    cs.run(uid, 'filter', prompt='观众反馈')
    config.set_key('llm.profiles', [{'name': '新模型', 'model': 'fixture', 'api_key': 'private-fixture'}])
    calls = model(monkeypatch, lambda _, payload: ratings(payload['comments']))
    state = cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 1 and len(calls[0]) == 1
    assert state['job']['status'] == 'done' and state['job']['cached'] == 2
    assert not state['rows'][1].get('ai_issues')
    assert 'private-fixture' not in cs._path(crud.get_project(uid)).read_text(encoding='utf-8')


def test_all_comments_translated_for_reading_except_one_rejected(uid, monkeypatch):
    def respond(calls, payload):
        if any(row['text'] == 'Original opinion 1' for row in payload['comments']):
            raise refusal()
        return {'translations': [dict(id=row['id'], text_zh='读得懂的完整中文') for row in payload['comments']]}
    calls = model(monkeypatch, respond)
    state = cs.run(uid, 'translate')
    assert len(calls) == 5 and state['job']['status'] == 'partial'
    assert [bool(row['text_zh']) for row in state['rows']] == [True, False, True]
    assert state['rows'][1]['ai_issues']['translate']['status'] == 'blocked'
    assert not state['filter'] and state['overlay'] is None  # 翻译不依赖上屏选择。


def test_strict_policy_stops_without_isolation(uid, monkeypatch):
    configuration.save('project:'+uid, dict(revision=configuration.resolve(uid)['revision'], updates={'comments.failure_policy':'stop'}))
    calls = model(monkeypatch, block_one)
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'filter', prompt='观众反馈')
    assert error.value.code == 'LLM_CONTENT_REJECTED' and len(calls) == 1


def test_repeated_single_rejections_stop_and_do_not_label_untested_rows_blocked(uid, monkeypatch):
    seed(uid, 12)
    calls = model(monkeypatch, lambda *_: (_ for _ in ()).throw(refusal()))
    with pytest.raises(cs.CommentError):
        cs.run(uid, 'filter', prompt='观众反馈')
    state = cs.view(uid)
    assert len(calls) < 12 and state['job']['skipped'] == 3 and state['job']['deferred'] == 9
    assert [row['ai_issues']['filter']['status'] for row in state['rows']] == ['blocked']*3 + ['deferred']*9
    assert state['job']['status'] == 'failed'


def test_auth_failure_mixed_with_rejection_is_not_treated_as_item_failure(uid, monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise StepFailedError('一组拒绝、一组密钥错误', code='LLM_PROVIDERS_FAILED', detail={'providers': [
            {'code':'LLM_CONTENT_REJECTED'}, {'code':'LLM_AUTH'}]})
    monkeypatch.setattr(cs, 'chat_validated', fail)
    with pytest.raises(cs.CommentError):
        cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 1 and not cs._read(crud.get_project(uid)).get('ai_issues')


def test_use_cached_scores_is_readonly_to_model_and_preserves_pending_status(uid, monkeypatch):
    model(monkeypatch, block_one)
    cs.run(uid, 'filter', prompt='观众反馈')
    seed(uid, 4)
    calls = model(monkeypatch, lambda *_: pytest.fail('仅用已有评分不得调用模型'))
    state = cs.run(uid, 'filter', prompt='观众反馈', only_cached=True, limit=1)
    assert not calls and len(state['filter']['items']) == 1
    assert state['job']['deferred'] == 1 and state['job']['blocked'] == 1
    assert state['job']['cached'] == 2 and state['job']['status'] == 'partial'
    assert len(state['available_assessments']) == 2 and state['overlay'] is None


def test_failures_do_not_reuse_after_source_change(uid, monkeypatch):
    from dubforge.utils.fs import atomic_write_bytes
    model(monkeypatch, block_one)
    cs.run(uid, 'filter', prompt='观众反馈')
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    doc = json.loads(path.read_text()); doc['comments']['items'][1]['text'] = 'Changed original'
    atomic_write_bytes(path, json.dumps(doc).encode())
    calls = model(monkeypatch, lambda _, payload: ratings(payload['comments']))
    state = cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 1 and state['job']['status'] == 'done' and len(state['available_assessments']) == 3


def test_api_validates_recovery_flags(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **k: pytest.fail('非法请求不得运行'))
    client = TestClient(app)
    assert client.post(f'/api/projects/{uid}/comments/translate', json={'retry_failed':'yes'}).status_code == 400
    assert client.post(f'/api/projects/{uid}/comments/filter', json={'prompt':'观众反馈','only_cached':True,'force':True}).status_code == 400


@pytest.mark.parametrize('doc', [{'rating_cache':{'assessments':[{'id':'x', 'eligible':True, 'score':200, 'reason':'坏分数'}]}},
                                {'ai_issues':{'filter':{'x':{'status':'blocked'}}}}])
def test_invalid_persisted_records_rejected(uid, doc):
    cs._write(crud.get_project(uid), doc)
    with pytest.raises(cs.CommentError) as error:
        cs.view(uid)
    assert error.value.code == 'COMMENTS_DOCUMENT_INVALID'


def test_failure_policy_inherits_global_series_project_and_freezes_recipe(uid):
    from test_configuration import save, bind
    save('global', {'comments.failure_policy':'stop'})
    assert configuration.resolve(uid)['values']['comments.failure_policy'] == 'skip'
    plan = configuration.binding_plan([uid], '')
    configuration.bind(dict(uids=[uid], template_id='', fingerprint=plan['fingerprint'], confirmed=True))
    assert configuration.resolve(uid)['values']['comments.failure_policy'] == 'stop'
    template = configuration.create_template('评论宽容处理')
    save(template['key'], {'comments.failure_policy':'skip'})
    bind([uid], template)
    assert configuration.run_spec(uid)['comment_failure_policy'] == 'skip'
    save('project:'+uid, {'comments.failure_policy':'stop'})
    assert configuration.run_spec(uid)['comment_failure_policy'] == 'stop'
    with pytest.raises(configuration.ConfigurationError):
        save('project:'+uid, {'comments.failure_policy':'invalid'})


def test_recovery_budget_is_bounded_and_cancellation_is_not_swallowed():
    from dubforge.comment_recovery import Recovery
    rows = [dict(id=str(i)) for i in range(100)]
    completed, skipped, events, calls = set(), set(), [], []
    def remaining(group):
        return [r for r in group if r['id'] not in completed | skipped]
    def execute(group, **kwargs):
        calls.append(group)
        if len(group)>1 or int(group[0]['id'])%2:
            raise StepFailedError('空正文', code='LLM_EMPTY_RESPONSE')
        completed.add(group[0]['id'])
    recovery = Recovery(policy='skip', check=lambda:None, remaining=remaining, execute=execute,
        failed=lambda group,*_: skipped.update(r['id'] for r in group), trace=lambda *a:events.append(a))
    recovery.process(rows)
    assert recovery.stopped and recovery.extra == 24 and len(calls) == 25
    assert remaining(rows) and any(e[0]=='recovery_stop' for e in events)
    def cancelled():
        raise cs.CommentError('已停止', code='COMMENTS_CANCELLED')
    recovery.check = cancelled
    with pytest.raises(cs.CommentError, match='已停止'):
        recovery.process(rows)
