"""最终请求预览、显式流式开关和不含鉴权信息的参数校验。"""
from contextlib import contextmanager
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import app
from dubforge import config
from dubforge.backends import llm
from dubforge.errors import ConfigError
from dubforge.llm_request_preview import preview_request, validate_profiles


@pytest.fixture(autouse=True)
def isolated_source(isolated_env):
    return isolated_env


def profile(**updates):
    return dict(name='预览模型', model='glm-5.3-flash',
                base_url='https://open.bigmodel.cn/api/paas/v4',
                params=updates)


def test_preview_uses_shared_real_payload_without_default_sampling_or_network(monkeypatch):
    monkeypatch.setattr(llm, '_client', lambda *args: pytest.fail('预览不允许访问网络'))
    original = profile(temperature=1, top_p=.95, thinking={'type': 'enabled', 'clear_thinking': False},
                       reasoning_effort='max', vendor={'options': [1, {'future': True}]})
    original['api_key'] = 'fixture-never-preview-this'
    frozen = deepcopy(original)
    result = preview_request(original)
    assert result['body'] == llm._transport_payload(llm._payload(original, result['body']['messages'], None, False))
    assert result['body']['vendor'] == original['params']['vendor']
    assert original == frozen
    assert 'fixture-never-preview-this' not in json.dumps(result)
    defaults = preview_request(profile())['body']
    assert set(defaults) == {'model', 'messages', 'stream'}
    assert defaults['stream'] is True


@pytest.mark.parametrize('mode', ['split', 'translate'])
def test_glm_low_preview_reflects_real_task_override_and_preserves_thinking(mode):
    original = profile(reasoning_effort='max', thinking={'type': 'enabled', 'clear_thinking': False})
    frozen = deepcopy(original)
    result = preview_request(original, mode=mode, thinking_mode='low')
    assert result['body']['reasoning_effort'] == 'low'
    assert result['body']['thinking'] == original['params']['thinking']
    assert {'path': 'reasoning_effort', 'kind': 'override', 'before': 'max', 'after': 'low'} in result['changes']
    assert original == frozen
    assert preview_request(original, mode=mode, thinking_mode='follow_profile')['body']['reasoning_effort'] == 'max'


def test_preview_uses_current_global_step_mode_only_when_unspecified():
    config.set_key('translate.thinking_mode', 'follow_profile')
    result = preview_request(profile(reasoning_effort='max'), mode='translate')
    assert result['body']['reasoning_effort'] == 'max'
    assert preview_request(profile(reasoning_effort='max'), mode='translate', thinking_mode='low')['body']['reasoning_effort'] == 'low'


def test_ark_preview_removes_thinking_for_verified_plan_route():
    original = dict(model='ark-code-latest', base_url='https://ark.cn-beijing.volces.com/api/plan/v3',
                    params={'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'})
    result = preview_request(original, mode='split')
    assert result['body']['reasoning_effort'] == 'low'
    assert 'thinking' not in result['body']
    assert any(item['path'] == 'thinking' and item['kind'] == 'remove' for item in result['changes'])


def test_unverified_proxy_preview_does_not_guess_low_compatibility():
    original = profile(reasoning_effort='max')
    original['base_url'] = 'https://proxy.example/api/paas/v4'
    result = preview_request(original, mode='translate')
    assert result['body']['reasoning_effort'] == 'max'
    assert any('未确认支持' in note for note in result['notes'])


def test_non_streaming_preview_matches_first_transport_body_and_removes_stream_only_fields():
    original = profile(stream=False, tool_stream=True, stream_options={'include_usage': True}, max_tokens=4000)
    result = preview_request(original)
    assert result['body']['stream'] is False
    assert result['body']['max_tokens'] == 4000
    assert 'tool_stream' not in result['body'] and 'stream_options' not in result['body']
    assert {item['path'] for item in result['changes']} == {'tool_stream', 'stream_options'}
    assert llm._transport_payload(result['body'], stream=False).get('stream') is None


def test_nested_authentication_is_redacted_but_output_token_limit_is_preserved():
    original = profile(max_tokens=4000, vendor={'api_key': 'secret-marker', 'items': [{'authorization': 'hidden-marker'}]})
    result = preview_request(original)
    assert result['body']['max_tokens'] == 4000
    assert result['body']['vendor']['api_key'] == '***'
    encoded = json.dumps(result)
    assert 'secret-marker' not in encoded and 'hidden-marker' not in encoded
    assert set(result['redacted_paths']) == {'body.vendor.api_key', 'body.vendor.items[0].authorization'}


def test_header_auth_suffixes_and_known_credential_values_are_redacted_without_token_limit_loss():
    original = profile(max_tokens=4000, max_output_tokens=5000,
                       vendor={'headers': {'X-API-Key': 'header-private-marker',
                                           'proxy_authorization': 'auth-private-marker'},
                               'label': 'sk-' + 'isolatedfictional' * 2,
                               'access_hint': 'ghp_' + 'fictionalnotreal' * 2,
                               'other': 'a' * 32 + '.Abc123XY'})
    result = preview_request(original)
    encoded = json.dumps(result)
    assert all(value not in encoded for value in ('header-private-marker', 'auth-private-marker',
                                                  'isolatedfictional', 'fictionalnotreal', 'Abc123XY'))
    assert result['body']['max_tokens'] == 4000 and result['body']['max_output_tokens'] == 5000
    assert len(result['redacted_paths']) == 5


@pytest.mark.parametrize('params', [[], None, 'temperature=1', 1, False, {'stream': 'false'}, {'tool_stream': 1}])
def test_invalid_params_rejected_by_preview_save_and_connectivity_before_any_side_effect(params, monkeypatch):
    monkeypatch.setattr(llm, 'test_provider', lambda *args: pytest.fail('无效参数不允许联网'))
    original = profile()
    original['params'] = params
    client = TestClient(app)
    before = config.load_config()
    responses = [client.post('/api/settings/preview_llm', json={'profile': original}),
                 client.post('/api/settings/test_llm', json={'profiles': [original]}),
                 client.post('/api/settings', json={'updates': {'llm.profiles': [original]}})]
    assert all(response.status_code == 400 for response in responses)
    assert all(response.json()['code'] == 'LLM_PARAMS_INVALID' for response in responses)
    assert config.load_config() == before


@pytest.mark.parametrize('profiles', [{}, 'profile', [None]])
def test_profile_list_shape_is_validated_with_stable_error(profiles):
    with pytest.raises(ConfigError):
        validate_profiles(profiles)


@pytest.mark.parametrize('body,code', [
    ({'profile': profile(), 'mode': []}, 'LLM_PREVIEW_MODE_INVALID'),
    ({'profile': profile(), 'thinking_mode': {}}, 'LLM_PREVIEW_THINKING_INVALID'),
    ({}, 'LLM_PROFILE_INVALID'),
])
def test_preview_bad_input_returns_domain_error(body, code):
    response = TestClient(app).post('/api/settings/preview_llm', json=body)
    assert response.status_code == 400
    assert response.json()['code'] == code


def _install_transport(monkeypatch, calls, *, reject_stream=False):
    class Client:
        @contextmanager
        def stream(self, method, url, **options):
            calls.append(('stream', options['json']))
            if reject_stream:
                response = httpx.Response(400, json={'error': {'message': 'Unsupported parameter: stream'}})
            else:
                frame = {'choices': [{'delta': {'content': '完成'}, 'finish_reason': 'stop'}]}
                response = httpx.Response(200, text='data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n',
                                          headers={'content-type': 'text/event-stream'})
            yield response

        def post(self, url, **options):
            calls.append(('post', options['json']))
            return httpx.Response(200, json={'choices': [{'message': {'content': '完成'}, 'finish_reason': 'stop'}]})

    @contextmanager
    def guarded(*args, **kwargs):
        yield Client(), None, None

    monkeypatch.setattr(llm, '_guarded_client', guarded)


@pytest.mark.parametrize('stream,kind', [(None, 'stream'), (True, 'stream'), (False, 'post')])
def test_actual_first_request_matches_preview_for_default_true_and_explicit_false(monkeypatch, stream, kind):
    calls = []
    _install_transport(monkeypatch, calls)
    original = profile(**({} if stream is None else {'stream': stream}), tool_stream=True, max_tokens=8000)
    preview = preview_request(original)
    logs = []
    assert llm._post(original, llm._payload(original, preview['body']['messages'], None, False), log=logs.append) == '完成'
    assert calls == [(kind, preview['body'])]
    assert ('普通响应(stream=false)' if stream is False else 'SSE流式') in logs[0]


def test_explicit_stream_true_is_removed_during_rejected_stream_compatibility_fallback(monkeypatch):
    calls = []
    _install_transport(monkeypatch, calls, reject_stream=True)
    original = profile(stream=True, tool_stream=True, stream_options={'include_usage': True}, max_tokens=8000)
    preview = preview_request(original)
    assert llm._post(original, llm._payload(original, preview['body']['messages'], None, False)) == '完成'
    assert calls[0] == ('stream', preview['body'])
    assert calls[1][0] == 'post'
    assert all(key not in calls[1][1] for key in ('stream', 'tool_stream', 'stream_options'))
    assert calls[1][1]['max_tokens'] == 8000
