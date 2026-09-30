"""评论 AI 的可观测性、定向修复、断点缓存与凭据脱敏，全部使用伪模型。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio as cs, comment_diagnostics as cd, config
from dubforge.backends import llm
from dubforge.domain import crud
from dubforge.errors import DubForgeError, StepFailedError
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.logs import read_llm_trace
from test_comment_studio import uid, seed, fake_translation, request_payload, wire_response


def ratings(rows):
    return {'assessments': [dict(id=row['id'], eligible=True, score=80, reason='有共鸣') for row in rows]}


def model(monkeypatch, respond):
    calls = []
    def chat(prompt, **kwargs):
        payload = request_payload(prompt)
        calls.append(payload['comments'])
        trace = kwargs['trace']
        trace('request', prompt, request_id=str(len(calls)), role='user', model='fixture')
        result = respond(calls, payload)
        text = wire_response(result) if not isinstance(result, str) else result
        trace('assistant_response', text, request_id=str(len(calls)), model='fixture')
        return text
    monkeypatch.setattr(llm, 'chat', chat)
    return calls


def test_partial_repair_only_requests_missing_and_invalid_ids_and_saves_immediately(uid, monkeypatch):
    seed(uid, 10)
    def respond(calls, payload):
        result = ratings(payload['comments'])
        if len(calls) == 1:
            result['assessments'][-1]['score'] = 200
            result['assessments'].append(dict(result['assessments'][-2]))
        else:
            assert len(cs._read(crud.get_project(uid))['rating_cache']['assessments']) == 8
            assert [r['id'] for r in payload['comments']] == ['9', '10']
            result['assessments'][0].update(score='82', eligible='true')
        return result
    calls = model(monkeypatch, respond)
    state = cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 2 and state['job']['processed'] == 10
    assert len(state['filter']['assessments']) == 10
    trace = cs.diagnostics(uid, 'filter')
    assert {'validation_error', 'retry_strategy', 'assistant_response', 'run_end'} <= {e['event'] for e in trace['events']}
    assert any('已保存 8 条' in line for line in trace['lines'])
    assert state['overlay'] is None


def test_format_failure_splits_only_once_with_bounded_attempts(uid, monkeypatch):
    seed(uid, 10)
    calls = model(monkeypatch, lambda calls, payload: {'assessments': []} if len(calls) <= 3 else ratings(payload['comments']))
    state = cs.run(uid, 'filter', prompt='观众反馈')
    assert [len(call) for call in calls] == [10, 10, 10, 5, 5]
    assert state['job']['processed'] == 10
    assert any('缩小批次' in e['content'] for e in cs.diagnostics(uid, 'filter')['events'])


def test_unrecoverable_format_keeps_specific_reason_and_never_loops(uid, monkeypatch):
    calls = model(monkeypatch, lambda *_: {'assessments': []})
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'filter', prompt='观众反馈')
    assert len(calls) == 4  # 三轮纠错，首个小批次仍失败便停止，不继续消耗额度。
    assert error.value.code == 'LLM_OUTPUT_INVALID'
    assert '尚需补齐编号' in error.value.message
    assert cs.view(uid)['job']['code'] == 'LLM_OUTPUT_INVALID'


@pytest.mark.parametrize('code', ['LLM_RATE_LIMITED', 'LLM_AUTH', 'LLM_TIMEOUT', 'LLM_HTTP_503', 'SERVICE_OFFLINE',
                                'LLM_CONTENT_REJECTED', 'LLM_EMPTY_RESPONSE', 'LLM_OUTPUT_TRUNCATED', 'LLM_PROVIDERS_FAILED'])
def test_service_errors_are_not_retried_as_format_failures_and_survive_restart(uid, monkeypatch, code):
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise StepFailedError('明确的服务错误', code=code)
    monkeypatch.setattr(cs, 'chat_validated', fail)
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'filter', prompt='观众反馈', failure_policy='stop')
    assert error.value.code == code and len(calls) == 1
    assert '明确的服务错误' in error.value.message
    cs._jobs.pop(uid)
    restored = cs.view(uid)['job']
    assert restored['code'] == code and restored['status'] == 'failed'
    assert cs.diagnostics(uid, 'filter')['job']['code'] == code


def test_completed_items_survive_failed_repair_and_next_run_only_fills_missing(uid, monkeypatch):
    from dubforge import configuration
    configuration.save('project:'+uid, dict(revision=configuration.resolve(uid)['revision'], updates={'comments.prompt': '观众反馈'}))
    def respond(calls, payload):
        return ratings(payload['comments'][:1]) if len(calls) == 1 else {'assessments': []}
    model(monkeypatch, respond)
    with pytest.raises(cs.CommentError):
        cs.run(uid, 'filter', prompt='观众反馈', failure_policy='stop')
    assert len(cs._read(crud.get_project(uid))['rating_cache']['assessments']) == 1
    assert cs.view(uid)['rating_progress'] == dict(saved=1, total=3, remaining=2)
    calls = model(monkeypatch, lambda _, payload: ratings(payload['comments']))
    result = cs.run(uid, 'filter', prompt='观众反馈')
    assert [row['text'] for row in calls[0]] == ['Original opinion 1', 'Original opinion 2']
    assert result['job']['cached'] == 1 and result['job']['processed'] == 2
    assert result['rating_progress']['remaining'] == 0


def test_storage_error_and_cancellation_are_not_model_format_errors(uid, monkeypatch):
    calls = model(monkeypatch, lambda _, payload: ratings(payload['comments']))
    def fail(*_):
        raise cs.CommentError('磁盘已满', code='COMMENTS_SAVE_FAILED')
    monkeypatch.setattr(cs, '_write', fail)
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'filter', prompt='观众反馈')
    assert error.value.code == 'COMMENTS_SAVE_FAILED' and len(calls) == 1
    assert cs.view(uid)['job']['processed'] == 0


def test_diagnostics_redacts_all_sinks_and_separates_actions(uid, monkeypatch):
    secret = 'private-fixture-credential'
    config.set_key('llm.profiles', [dict(name='test', api_key=secret, model='fixture')])
    def fail(*args, **kwargs):
        kwargs['log']('Bearer sk-fixture-other ' + secret)
        kwargs['trace']('request', 'https://user:password@example.com?api_key=' + secret, secret=secret)
        raise StepFailedError('认证失败 ' + secret, code='LLM_AUTH', detail={'api_key': secret, 'body': secret})
    monkeypatch.setattr(cs, 'chat_validated', fail)
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'filter', prompt='观众反馈')
    assert secret not in json.dumps(error.value.to_dict())
    for path in cd.paths(crud.get_project(uid), 'filter'):
        text = path.read_text(encoding='utf-8')
        assert secret not in text and 'sk-fixture-other' not in text and 'user:password' not in text
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    cs.run(uid, 'translate')
    assert cs.diagnostics(uid, 'filter')['job']['status'] == 'failed'
    assert cs.diagnostics(uid, 'translate')['job']['status'] == 'done'


def test_read_trace_pages_half_line_generation_reset_and_action_api(uid):
    project = crud.get_project(uid)
    job = dict(id='a', action='filter', status='running', processed=0, total=3)
    diag = cd.Diagnostics(project, job)
    diag.trace('request', '中文提示')
    first = read_llm_trace(diag.trace_path, after=0, tail=1)
    assert first['reset'] and first['more']
    second = read_llm_trace(diag.trace_path, after=first['cursor'], trace_id=first['trace_id'])
    assert [e['content'] for e in second['events']] == ['中文提示']
    assert not second['reset']
    with diag.trace_path.open('ab') as stream:
        stream.write(b'{"event":"status"')
    partial = read_llm_trace(diag.trace_path, after=second['cursor'], trace_id=second['trace_id'])
    assert not partial['events'] and partial['cursor'] == second['cursor']
    cd.Diagnostics(project, {**job, 'id': 'b'})
    reset = read_llm_trace(diag.trace_path, after=second['cursor'], trace_id=second['trace_id'])
    assert reset['reset'] and reset['events'][0]['job_id'] == 'b'
    assert cs.diagnostics(uid, 'filter')['job']['status'] == 'interrupted'
    api = TestClient(app)
    response = api.get(f'/api/projects/{uid}/comments/diagnostics?action=filter')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert api.get(f'/api/projects/{uid}/comments/diagnostics?action=../../config').status_code == 422
    assert api.get(f'/api/projects/{uid}/comments/diagnostics?after=-1').status_code == 422
    assert api.get('/api/projects/missing/comments/diagnostics').status_code == 400
    assert not crud.latest_job(project.id, 'assemble')


def test_business_cancel_after_response_discards_results_without_retry(uid, monkeypatch):
    calls = model(monkeypatch, lambda _, payload: ratings(payload['comments']))
    def cancelled():
        if calls:
            raise DubForgeError('全流程已停止', code='WORKFLOW_STOPPED')
    with pytest.raises(cs.CommentError) as error:
        cs.run(uid, 'filter', prompt='观众反馈', cancel_check=cancelled)
    assert error.value.code == 'WORKFLOW_STOPPED'
    assert len(calls) == 1 and cs.view(uid)['job']['status'] == 'cancelled'
    assert not cs._read(crud.get_project(uid)).get('rating_cache')
