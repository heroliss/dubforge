"""统一对话只操作隔离数据，HTTP/模型均为替身。"""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from dubforge import config, llm_conversations as journal
from dubforge.backends import llm
from dubforge.errors import DubForgeError, StepFailedError


@pytest.fixture(autouse=True)
def isolated_journal(isolated_env, monkeypatch):
    providers = [dict(name='测试配置', base_url='https://model.invalid/v1', model='测试模型',
                      api_key='opaque-provider-key', params=dict(stream=False))]
    monkeypatch.setattr(llm, '_profiles', lambda profile=None: copy.deepcopy(providers))
    with llm._rate_limit_lock:
        llm._rate_limit_until.clear()
    return providers


def client(monkeypatch, replies):
    calls = []

    class Client:
        def __init__(self, base):
            self.base = base

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, json, **kwargs):
            calls.append((self.base, copy.deepcopy(json)))
            value = replies(len(calls), self.base, json) if callable(replies) else replies[len(calls) - 1]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json=dict(choices=[dict(message=dict(content=value), finish_reason='stop')]),
                                  request=httpx.Request('POST', self.base + url))

    monkeypatch.setattr(llm, '_client', Client)
    return calls


def only_conversation(**filters):
    rows = journal.list_conversations(**filters)['items']
    assert len(rows) == 1
    return rows[0]


def all_events(identity):
    events, cursor, generation = [], 0, ''
    for _ in range(100):
        value = journal.read_conversation(identity, after=cursor, trace_id=generation)
        events.extend(value['events'])
        cursor, generation = value['cursor'], value['trace_id']
        if not value['more']:
            return events
    pytest.fail('对话分页不能无进展循环')


def message_text(events, event, *, role=None):
    return ''.join(item['content'] for item in events if item['event'] == event
                   and (role is None or item.get('role') == role))


def test_context_marks_ownership_without_creating_an_empty_conversation():
    with journal.context(project_uid='abc', operation='chapters', label='章节整理', owner_id='pub-1'):
        assert journal.list_conversations()['items'] == []
    assert not (config.data_root() / 'llm_conversations').exists()


def test_unscoped_chat_is_automatically_recorded_and_return_is_unchanged(monkeypatch):
    client(monkeypatch, ['准确回复'])
    assert llm.chat('用户问题', system='任务规则') == '准确回复'
    row = only_conversation()
    assert row['operation'] == 'unclassified' and row['status'] == 'completed'
    assert row['label'] == '未归类模型调用' and not row['active']
    events = all_events(row['id'])
    assert message_text(events, 'request', role='system') == '任务规则'
    assert message_text(events, 'request', role='user') == '用户问题'
    assert message_text(events, 'assistant_response') == '准确回复'
    assert row['model'] == '测试模型'


def test_json_retry_and_nested_chat_share_one_journal_and_keep_validation_events(monkeypatch, make_project):
    uid = make_project(title='模型过程测试')
    client(monkeypatch, ['{"value":0}', '{"value":1}'])
    legacy = []

    def validate(value):
        if value['value'] != 1:
            raise ValueError('期望值为1')
        return value

    with journal.context(project_uid=uid, operation='chapters', label='整理章节', owner_id='pub-1'):
        result = llm.chat_json('整理数据', system='输出JSON', validator=validate,
                               trace=lambda event, content='', **meta: legacy.append((event, content, meta)))
    assert result == {'value': 1}
    row = only_conversation(project_uid=uid, operation='chapters', owner_id='pub-1')
    assert row['project_title'] == '模型过程测试'
    events = all_events(row['id'])
    assert len([event for event in events if event['event'] == 'assistant_response']) == 2
    assert len([event for event in events if event['event'] == 'run_start']) == 1
    assert len({event['request_id'] for event in events if event['event'] == 'request'}) == 2
    assert any(event['event'] == 'validation_error' for event in events)
    assert any(event['event'] == 'validation_success' for event in events)
    assert len([event for event, _, _ in legacy if event == 'assistant_response']) == 2
    assert [event['retry_attempt'] for event in events if event['event'] == 'assistant_response'] == [1, 2]


def test_provider_fallback_is_one_conversation_with_separate_request_ids(monkeypatch, isolated_journal):
    isolated_journal.append(dict(name='备用配置', base_url='https://fallback.invalid/v1', model='备用模型',
                                api_key='fallback-key', params=dict(stream=False)))

    def reply(index, base, body):
        return httpx.Response(401, json={'error': {'message': '认证无效'}},
                              request=httpx.Request('POST', base + '/chat/completions')) if index == 1 else '备用回复'

    calls = client(monkeypatch, reply)
    with journal.context(operation='summary', label='生成概要'):
        assert llm.chat('概要素材') == '备用回复'
    assert len(calls) == 2
    row = only_conversation(operation='summary')
    events = all_events(row['id'])
    assert row['status'] == 'completed'
    assert len({event['request_id'] for event in events if event['event'] == 'request'}) == 2
    assert any(event['event'] == 'provider_error' for event in events)
    assert message_text(events, 'assistant_response') == '备用回复'


def test_cancellation_keeps_the_same_business_error_and_records_terminal_state():
    from dubforge.runner import StepCancelled

    stopped = StepCancelled('用户已停止')

    def cancel():
        raise stopped

    with pytest.raises(StepCancelled) as error:
        llm.chat('不用发送', cancel_check=cancel)
    assert error.value is stopped
    row = only_conversation()
    assert row['status'] == 'cancelled' and not row['active']
    assert any(event['event'] == 'error' and event['code'] == 'STEP_CANCELLED' for event in all_events(row['id']))


def test_failed_output_validation_is_recorded_without_changing_retry_budget(monkeypatch):
    calls = client(monkeypatch, ['not JSON', 'still not JSON'])
    with pytest.raises(StepFailedError) as error:
        llm.chat_json('必须JSON', max_retries=2)
    assert error.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 2
    row = only_conversation()
    assert row['status'] == 'failed'
    assert len([event for event in all_events(row['id']) if event['event'] == 'validation_error']) == 2


def test_provider_test_is_captured_even_though_it_calls_post_directly(monkeypatch, isolated_journal):
    client(monkeypatch, ['OK'])
    result = llm.test_provider(isolated_journal[0])
    assert result['ok'] is True and result['reply'] == 'OK'
    row = only_conversation(operation='provider_test')
    assert row['status'] == 'completed'
    assert message_text(all_events(row['id']), 'assistant_response') == 'OK'


def test_provider_test_failure_is_not_mislabelled_completed(monkeypatch, isolated_journal):
    client(monkeypatch, [httpx.Response(401, json={'error': {'message': '认证失败'}},
                                      request=httpx.Request('POST', 'https://model.invalid'))])
    result = llm.test_provider(isolated_journal[0])
    assert result['ok'] is False
    row = only_conversation(operation='provider_test')
    assert row['status'] == 'failed'
    assert any(event['event'] == 'error' for event in all_events(row['id']))


def test_explicit_unified_trace_callback_does_not_record_every_event_twice(monkeypatch):
    client(monkeypatch, ['回复'])
    assert llm.chat('问题', trace=journal.trace) == '回复'
    events = all_events(only_conversation()['id'])
    assert len([event for event in events if event['event'] == 'request']) == 1
    assert len([event for event in events if event['event'] == 'assistant_response']) == 1


def test_legacy_trace_disk_failure_is_only_a_warning(monkeypatch):
    calls = client(monkeypatch, ['回复'])

    def broken(*args, **kwargs):
        raise OSError('旧日志目录不可写')

    assert llm.chat('问题', trace=broken) == '回复'
    row = only_conversation()
    assert row['status'] == 'completed' and row['warning'] and len(calls) == 1


def test_process_interruption_does_not_leave_an_active_capture():
    @journal.capture
    def interrupted(trace=None):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        interrupted()
    row = only_conversation()
    assert row['status'] == 'interrupted' and not row['active']


def test_actual_provider_key_and_nested_credentials_never_enter_journal_or_old_trace(monkeypatch):
    secret = 'opaque-provider-key'
    output = '完成 sk-exampleCredential12345 Bearer token-is-private'
    client(monkeypatch, [output])
    legacy = []
    prompt = f'请隐藏 {secret} 与 https://name:pass@example.test/a?api_key=querysecret&ok=1'
    assert llm.chat(prompt, trace=lambda event, content='', **meta: legacy.append(content)) == output
    row = only_conversation()
    events = all_events(row['id'])
    dumped = json.dumps(events, ensure_ascii=False)
    for value in (secret, 'sk-exampleCredential12345', 'token-is-private', 'name:pass', 'querysecret'):
        assert value not in dumped
        assert value not in '\n'.join(legacy)
    assert '[已隐藏]' in dumped
    assert journal.redact({'nested': {'api_key': 'secret', 'password': 'secret', 'max_tokens': 42}}) == {'nested': {'max_tokens': 42}}


def test_nested_request_parameters_are_redacted_only_in_text_logs(monkeypatch, isolated_journal):
    params = dict(stream=False, max_tokens=4096, vendor=dict(api_key='nested-private-credential',
        items=[dict(Authorization='Bearer nested-private-auth', token='nested-private-token',
                    text='opaque-provider-key', max_tokens=123)]),
        response_format=dict(type='json_schema', schema=dict(api_key='schema-private-credential',
                                                            description='opaque-provider-key')))
    isolated_journal[0]['params'] = copy.deepcopy(params)
    before = copy.deepcopy(isolated_journal)
    calls = client(monkeypatch, ['完整业务回复'])
    logs = []

    assert llm.chat('问题', log=logs.append) == '完整业务回复'

    body = calls[0][1]
    for key, value in params.items():
        assert body[key] == value
    assert isolated_journal == before
    text = '\n'.join(logs)
    for secret in ('opaque-provider-key', 'nested-private-credential', 'nested-private-auth',
                   'nested-private-token', 'schema-private-credential'):
        assert secret not in text
    assert 'max_tokens=4096' in text and "'max_tokens': 123" in text
    assert '[已隐藏]' in text


def test_secret_split_between_stream_events_is_removed_from_persisted_conversation():
    secret = 'opaque-long-private-credential-0123456789'

    @journal.capture
    def generate(trace=None):
        journal.protect({'api_key': secret})
        trace('request', '提问', request_id='r1', role='user')
        trace('reasoning_delta', '开始 ' + secret[:18], request_id='r1')
        trace('reasoning_delta', secret[18:] + ' 结束', request_id='r1')
        trace('response_metadata', '结束', request_id='r1')
        return '原业务结果'

    assert generate() == '原业务结果'
    events = all_events(only_conversation()['id'])
    reasoning = message_text(events, 'reasoning_delta')
    assert reasoning == '开始 [已隐藏] 结束'
    assert secret not in json.dumps(events)


def test_index_failure_warns_but_does_not_repeat_or_fail_model(monkeypatch):
    calls = client(monkeypatch, ['可用回复'])
    monkeypatch.setattr(journal, '_write_index', lambda *args: (_ for _ in ()).throw(OSError('模拟索引故障')))
    assert llm.chat('问题') == '可用回复'
    row = only_conversation()
    assert len(calls) == 1 and row['warning'] and row['status'] == 'completed'
    assert message_text(all_events(row['id']), 'assistant_response') == '可用回复'


def test_event_file_failure_does_not_change_model_result(monkeypatch):
    calls = client(monkeypatch, ['仍然可用'])
    original = Path.open

    def fail_events(path, *args, **kwargs):
        if path.name.endswith('.jsonl'):
            raise OSError('模拟磁盘失败')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', fail_events)
    assert llm.chat('问题') == '仍然可用'
    row = only_conversation()
    assert len(calls) == 1 and row['warning'] and not row['active']


def test_long_messages_are_read_in_bounded_pages_and_reconstruct_without_loss(monkeypatch):
    prompt, answer = '长输入😀\n' * 50000, '长输出\n' * 14000
    client(monkeypatch, [answer])
    assert llm.chat(prompt) == answer
    identity = only_conversation()['id']
    events, cursor, generation, pages = [], 0, '', 0
    while True:
        value = journal.read_conversation(identity, after=cursor, trace_id=generation, limit=500)
        assert len(json.dumps(value, ensure_ascii=False).encode()) <= journal._READ_BUDGET
        assert value['cursor'] > cursor
        events.extend(value['events'])
        cursor, generation, pages = value['cursor'], value['trace_id'], pages + 1
        if not value['more']:
            break
        assert pages < 20
    assert pages > 1
    assert message_text(events, 'request', role='user') == prompt
    assert message_text(events, 'assistant_response') == answer
    pieces = [event for event in events if event['event'] == 'request']
    assert [event['content_part'] for event in pieces] == list(range(len(pieces)))
    assert len({event['message_id'] for event in pieces}) == 1
    assert all(event['content_parts'] == len(pieces) for event in pieces)


def test_incremental_read_honours_trace_identity_and_ignores_unfinished_last_line(monkeypatch):
    client(monkeypatch, ['回复'])
    llm.chat('问题')
    identity = only_conversation()['id']
    first = journal.read_conversation(identity)
    end = first['cursor']
    assert journal.read_conversation(identity, after=end, trace_id=identity)['events'] == []
    reset = journal.read_conversation(identity, after=end, trace_id='another-generation')
    assert reset['reset'] and reset['events'] == first['events']
    with (config.data_root() / 'llm_conversations' / (identity + '.jsonl')).open('ab') as stream:
        stream.write(b'{"event":"broken')
    partial = journal.read_conversation(identity, after=end, trace_id=identity)
    assert partial['events'] == [] and partial['cursor'] == end


@pytest.mark.parametrize('identity', ['../config.yaml', 'a' * 31, 'A' * 32, 'x' * 32])
def test_read_rejects_non_generated_ids(identity):
    with pytest.raises(DubForgeError) as error:
        journal.read_conversation(identity)
    assert error.value.code == 'LLM_CONVERSATION_NOT_FOUND'


def test_concurrent_threads_keep_project_and_operation_ownership_separate(monkeypatch):
    client(monkeypatch, lambda index, base, body: body['messages'][-1]['content'])

    def run(index):
        with journal.context(project_uid=f'project-{index}', operation='translate', owner_id=f'job-{index}'):
            return llm.chat(f'内容{index}')

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, range(2))) == ['内容0', '内容1']
    for index in range(2):
        row = only_conversation(project_uid=f'project-{index}', owner_id=f'job-{index}')
        assert message_text(all_events(row['id']), 'request', role='user') == f'内容{index}'
    assert journal.list_conversations(project_uid="' OR 1=1 --")['items'] == []


def test_running_conversation_is_visible_before_response_finishes(monkeypatch):
    def reply(index, base, body):
        row = only_conversation(operation='publish_copy')
        assert row['status'] == 'running' and row['active']
        assert message_text(all_events(row['id']), 'request', role='user') == '资料'
        return '文案'

    client(monkeypatch, reply)
    with journal.context(operation='publish_copy', owner_id='pub'):
        assert llm.chat('资料') == '文案'


def test_completed_records_survive_memory_reset_and_lists_have_cursor_pagination(monkeypatch):
    client(monkeypatch, ['一', '二', '三'])
    for _ in range(3):
        llm.chat('问题')
    with journal._gate:
        journal._recent.clear()
    first = journal.list_conversations(limit=2)
    assert len(first['items']) == 2 and first['more'] and first['next_cursor']
    second = journal.list_conversations(limit=2, before=first['next_cursor'])
    assert len(second['items']) == 1 and not second['more']
    assert len({row['id'] for row in first['items'] + second['items']}) == 3
    assert all_events(first['items'][0]['id'])


def test_unclosed_capture_from_previous_process_is_projected_interrupted(monkeypatch):
    recording = journal._Capture()
    with journal._gate:
        journal._active.pop((str(recording.root), recording.data['id']))
        journal._recent.clear()
    row = only_conversation()
    assert row['status'] == 'interrupted' and not row['active'] and row['warning']


def test_read_only_api_filters_and_cursor_alias_do_not_expose_paths(monkeypatch):
    from app.main import app

    client(monkeypatch, ['回复'])
    with journal.context(project_uid='test-project', operation='chapters', owner_id='pub'):
        llm.chat('问题')
    api = TestClient(app)
    response = api.get('/api/llm/conversations?project_uid=test-project&operation=chapters&owner_id=pub')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    identity = response.json()['items'][0]['id']
    detail = api.get(f'/api/llm/conversations/{identity}?limit=2')
    assert detail.status_code == 200 and len(detail.json()['events']) == 2
    value = detail.json()
    following = api.get(f'/api/llm/conversations/{identity}', params=dict(cursor=value['cursor'], trace_id=value['trace_id']))
    assert following.status_code == 200 and following.json()['cursor'] > value['cursor']
    assert 'path' not in following.json()
    assert api.get(f'/api/llm/conversations/{identity}?after=-1').status_code == 422
