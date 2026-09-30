"""只用短测试 WAV 验证超长重试，不调用真实模型。"""
import io
import wave

import pytest

from conftest import synthesize
from test_dubbing_preparation import index_prepared, prepared
from dubforge import config
from dubforge.domain import crud
from dubforge.errors import ConfigError, DubForgeError
from dubforge.pipeline import dubbing_edit, line_settings, tts_duration
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_tts import line_cache_valid, line_fingerprint
from dubforge.utils.fs import atomic_write_bytes


def wav(path, seconds):
    data = io.BytesIO()
    with wave.open(data, 'wb') as stream:
        stream.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * round(32000 * seconds))
    atomic_write_bytes(path, data.getvalue())


@pytest.mark.parametrize('initial,value,expected', [
    (1, '0.7, 0.5', [1, .7, .5]), (.7, '0.7, 0.5', [.7, .5]),
    (.6, '0.7, 0.5', [.6, .5]), (.5, '0.7, 0.5', [.5]), (.4, '0.7, 0.5', [.4]),
    (1, '0.5, 0.7, 0.5', [1, .5]), (1, '0.7，0.7；0.6 0.5', [1, .7, .6, .5]),
    (1, '', [1]), (1, None, [1]), (1, '  ', [1]),
])
def test_only_strictly_smaller_factors_are_attempted(initial, value, expected):
    assert tts_duration.attempt_factors(initial, value) == expected


@pytest.mark.parametrize('value', ['0.4', '2.1', 'nan', 'inf', 'abc', '0.7,no', True, [.7, .5]])
def test_invalid_factors_are_rejected(value):
    with pytest.raises(ConfigError):
        tts_duration.parse_factors(value)


def render(tmp_path, durations, *, line=None, factors=None, can_retry=lambda: True):
    calls, phases, logs = [], [], []
    output = tmp_path / '1.wav'

    def generate(path, factor):
        value = durations[len(calls)]
        calls.append(factor)
        if isinstance(value, Exception):
            raise value
        wav(path, value)

    result = tts_duration.synthesize(generate, output,
        line or {'id': 1, 'start': 1, 'end': 3}, factors or [1, .7, .5],
        log=logs.append, on_attempt=lambda *args: phases.append(args), can_retry=can_retry)
    assert not list(tmp_path.glob('*.tmp'))
    return result, calls, phases, output


@pytest.mark.parametrize('durations,expected', [([1], [1]), ([2.04], [1]),
    ([4, 1.8], [1, .7]), ([4, 3, 1.8], [1, .7, .5])])
def test_stop_as_soon_as_audio_fits(tmp_path, durations, expected):
    result, calls, phases, out = render(tmp_path, durations)
    assert calls == expected and not result['overrun']
    assert result['used_factor'] == expected[-1]
    assert result['retries'] == len(expected) - 1
    assert tts_duration.audio_seconds(out) == durations[-1]
    assert phases == [(i, 3, factor) for i, factor in enumerate(expected)]


def test_gap_is_available_and_overlapping_next_start_is_respected(tmp_path):
    result, calls, _, _ = render(tmp_path, [2.8], line={'id': 1, 'start': 1, 'end': 3, 'gap': 1})
    assert calls == [1] and result['available'] == 3
    result, calls, _, _ = render(tmp_path, [2.8, 1.4],
        line={'id': 1, 'start': 1, 'end': 3, 'gap': 0, 'available_duration': 1.5})
    assert calls == [1, .7] and result['available'] == 1.5


def test_all_overlong_keeps_shortest_even_if_smaller_factor_is_slower(tmp_path):
    result, calls, _, out = render(tmp_path, [5, 3, 4])
    assert calls == [1, .7, .5] and result['overrun']
    assert result['used_factor'] == .7 and tts_duration.audio_seconds(out) == 3


def test_retry_does_not_replace_playable_version_until_line_is_finished(tmp_path):
    output = tmp_path / '1.wav'
    wav(output, 1)
    previous = output.read_bytes()
    calls = []

    def generate(path, factor):
        assert output.read_bytes() == previous, '生成和重试期间应保留上一次完整配音'
        calls.append(factor)
        wav(path, {1: 5, .7: 3, .5: 1.8}[factor])

    tts_duration.synthesize(generate, output, {'id': 1, 'start': 0, 'end': 2}, [1, .7, .5],
        log=lambda _: None, on_attempt=lambda *args: None, can_retry=lambda: True)
    assert calls == [1, .7, .5]
    assert tts_duration.audio_seconds(output) == 1.8
    assert not list(tmp_path.glob('*.tmp'))


def test_empty_setting_disables_retries(tmp_path):
    result, calls, _, out = render(tmp_path, [5], factors=tts_duration.attempt_factors(1, ''))
    assert calls == [1] and result['retries'] == 0 and result['overrun']
    assert tts_duration.audio_seconds(out) == 5


def test_failed_retry_keeps_valid_audio_and_explains_failure(tmp_path):
    result, calls, _, out = render(tmp_path, [5, DubForgeError('服务断开', code='TTS_FAILED')])
    assert calls == [1, .7] and result['retries'] == 1
    assert result['error'] and result['used_factor'] == 1
    assert tts_duration.audio_seconds(out) == 5


def test_cancellation_prevents_new_attempt_and_keeps_current_audio(tmp_path):
    result, calls, _, out = render(tmp_path, [5], can_retry=lambda: False)
    assert calls == [1] and result['retries'] == 0
    assert tts_duration.audio_seconds(out) == 5


def test_initial_service_failure_does_not_trigger_duration_retries(tmp_path):
    with pytest.raises(DubForgeError, match='服务断开'):
        render(tmp_path, [DubForgeError('服务断开', code='TTS_FAILED')])


@pytest.mark.parametrize('level', ['global', 'step', 'line'])
def test_retry_saves_actual_factor_only_to_line_and_keeps_fingerprint_consistent(index_prepared, monkeypatch, level):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}'
    key = 'tts.indextts25.duration_factor'
    if level == 'global':
        config.set_key(key, .6)
        assert client.put(base + '/step_params', json={'updates': {}, 'reset': [key]}).status_code == 200
    elif level == 'step':
        assert client.put(base + '/step_params', json={'updates': {key: .6}}).status_code == 200
    else:
        assert client.put(base + '/workbench/lines/1/params', json={'updates': {key: .6}}).status_code == 200
    calls = []
    def generate(url, text, out, lang, **kwargs):
        factor = kwargs['duration_factor']
        calls.append(factor)
        wav(out, 5 if factor == .6 else 2.8)
        return out
    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    synthesize(client, base + '/workbench/lines/1/synthesize', json={'force': True})
    assert calls == [.6, .5]
    project = crud.get_project(uid)
    deps = Dependencies(project)
    line = deps.dubbing_lines()[0]
    assert line_settings.effective_config(project, line, deps.cfg)(key) == .5
    assert line_settings.overrides(project, line)[key] == .5
    assert deps.cfg(key) == (.6 if level != 'line' else 1.1)
    data = dubbing_edit.read_take(crud.project_dir(uid), line, 1)
    assert data['settings']['duration_factor'] == .5
    assert data['duration_retry']['retries'] == 1 and data['duration_retry']['available'] == 3
    assert line_cache_valid(project, line, crud.project_dir(uid))
    current = client.get(base + '/workbench?probe=false').json()['lines'][0]['wav']
    assert current['duration_retry']['used_factor'] == .5 and not current['settings_changed']
    synthesize(client, base + '/workbench/lines/1/synthesize', json={'force': True})
    assert calls == [.6, .5, .5], '下一次直接沿用本句已保存的因子'


def test_per_line_empty_list_disables_step_retry_and_invalid_input_does_not_save(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {tts_duration.KEY: ''}}).status_code == 200
    assert client.put(base + '/lines/1/params', json={'updates': {tts_duration.KEY: 'oops'}}).status_code == 400
    calls = []
    def generate(url, text, out, lang, **kwargs):
        calls.append(kwargs['duration_factor']); wav(out, 5); return out
    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    synthesize(client, base + '/lines/1/synthesize', json={'force': True})
    assert calls == [1.1]


def test_retry_policy_does_not_invalidate_previous_voice_fingerprint(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    before = line_fingerprint(project, line)
    config.set_key(tts_duration.KEY, '')
    assert line_fingerprint(project, line) == before
    from dubforge.runner import compute_fingerprint
    step_before = compute_fingerprint(project, 'tts')
    config.set_key('tts.indextts25.request_timeout_seconds', 600)
    assert line_fingerprint(project, line) == before
    assert compute_fingerprint(project, 'tts') == step_before


@pytest.mark.parametrize('available,seconds,retries', [(2, 2.08, 0), (10, 10.14, 0),
    (2, 2.2, 1), (10, 10.2, 1), (.5, .6, 1)])
def test_minor_overrun_is_not_recomputed_but_stays_visible(tmp_path, available, seconds, retries):
    result, calls, _, out = render(tmp_path, [seconds, available],
        line={'id': 1, 'start': 0, 'end': available})
    assert len(calls) == retries + 1
    if not retries:
        assert result['overrun'] and result['retry_skipped'] == 'minor_overrun'
        assert tts_duration.audio_seconds(out) == pytest.approx(seconds)


@pytest.mark.parametrize('code', ['STEP_TIMEOUT', 'STEP_CANCELLED', 'TTS_REQUEST_TIMEOUT', 'TTS_SERVICE_DRAINING'])
def test_retry_cannot_swallow_stop_or_service_timeout(tmp_path, code):
    output = tmp_path / '1.wav'
    wav(output, 1)
    before = output.read_bytes()
    with pytest.raises(DubForgeError) as caught:
        render(tmp_path, [4, DubForgeError('停止等待', code=code)])
    assert caught.value.code == code
    assert output.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


def test_batch_saves_best_factor_preserving_other_lines_and_parameters(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    key = 'tts.indextts25.duration_factor'
    other = 'tts.indextts25.temperature'
    client.put(base + '/lines/1/params', json={'updates': {other: .55}})

    def generate(url, text, out, lang, **kwargs):
        # 最后一次更慢，必须保存保留音频的 .7，而非最后尝试的 .5。
        wav(out, {1.1: 5, .7: 3.5, .5: 4}[kwargs['duration_factor']])
        return out

    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    synthesize(client, base + '/synthesize', json={'line_ids': [1, 2], 'force': True})
    project = crud.get_project(uid)
    deps = Dependencies(project)
    lines = deps.dubbing_lines()
    assert line_settings.overrides(project, lines[0]) == {key: .7, other: .55}
    assert line_settings.overrides(project, lines[1]) == {key: .7}
    for line in lines:
        assert line_cache_valid(project, line, crud.project_dir(uid))
    assert deps.cfg(key) == 1.1 and config.get(key) == 1.0


def test_invalid_initial_audio_does_not_replace_previous_version(tmp_path):
    output = tmp_path / '1.wav'
    wav(output, 1)
    before = output.read_bytes()
    with pytest.raises(DubForgeError, match='有效时长'):
        tts_duration.synthesize(lambda path, factor: atomic_write_bytes(path, b'invalid'),
            output, {'id': 1, 'start': 0, 'end': 2}, [1, .7],
            log=lambda _: None, on_attempt=lambda *args: None, can_retry=lambda: True)
    assert output.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


def test_task_reports_retry_progress_and_ignores_previous_attempt_callback(index_prepared, monkeypatch):
    from threading import Event
    from dubforge.backends import tts_indextts25
    from dubforge.runner import runner
    from test_dubbing_tasks import finished
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    entered, release = Event(), Event()
    callbacks = []

    def generate(url, text, out, lang, **kwargs):
        callbacks.append(kwargs['progress'])
        if len(callbacks) == 1:
            kwargs['progress'](.9)
            wav(out, 4)
        else:
            kwargs['progress'](.2)
            callbacks[0](1)  # 上一次尝试迟到的进度不能改变本次的 20%。
            entered.set()
            assert release.wait(10)
            wav(out, 2.5)
        return out

    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    runner.start_dubbing(uid, [1])
    try:
        assert entered.wait(10)
        state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
        assert state['active_attempt'] == 1 and state['active_factor'] == .7
        assert state['active_progress'] == .2
        assert crud.get_step(project.id, 'tts').progress == pytest.approx(39.6)
    finally:
        release.set()
    assert finished(project).status == 'done'
    assert len(callbacks) == 2


def test_cancelled_task_keeps_first_audio_without_starting_retry(index_prepared, monkeypatch):
    from threading import Event
    from dubforge.backends import tts_indextts25
    from dubforge.runner import runner
    from test_dubbing_tasks import finished
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    entered, release = Event(), Event()
    calls = []
    def generate(url, text, out, lang, **kwargs):
        calls.append(kwargs['duration_factor'])
        entered.set()
        assert release.wait(10)
        wav(out, 5)
        return out
    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    runner.start_dubbing(uid, [1])
    try:
        assert entered.wait(10)
        runner.cancel_step(uid, 'tts')
    finally:
        release.set()
    assert finished(project).status == 'cancelled' and calls == [1.1]
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    assert state['lines'][0]['wav']['exists']
    assert state['lines'][0]['wav']['duration_retry']['retries'] == 0


def test_gptsovits_does_not_use_index_specific_retry_policy(prepared, monkeypatch):
    from app.main import app
    from fastapi.testclient import TestClient
    from dubforge.backends import tts_gptsovits
    uid, project, _ = prepared
    calls = []
    def generate(url, text, out, lang, **kwargs):
        calls.append(text); wav(out, 5); return out
    monkeypatch.setattr(tts_gptsovits, 'synth', generate)
    client = TestClient(app)
    synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={'force': True})
    assert calls == ['你好']
    line = Dependencies(crud.get_project(uid)).dubbing_lines()[0]
    assert 'duration_retry' not in dubbing_edit.read_take(crud.project_dir(uid), line, 1)
    client.close()
