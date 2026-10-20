"""只用短测试 WAV 验证超长重试，不调用真实模型。"""
import io
import wave

import pytest

from conftest import synthesize
from test_dubbing_preparation import index_prepared, prepared as _prepared
from dubforge import config
from dubforge.domain import crud
from dubforge.errors import ConfigError, DubForgeError
from dubforge.pipeline import dubbing_edit, line_settings, tts_duration
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_tts import line_cache_valid, line_fingerprint
from dubforge.utils.fs import atomic_write_bytes


@pytest.fixture
def prepared(_prepared):
    # 这些用例专门验证单句缩时、临时因子和失败保留。新默认的先滑动再缩时
    # 由 test_voice_timing_integration 验证，不能让借用邻句空位改变此处预算。
    from test_configuration import save
    uid, project, calls = _prepared
    save('project:' + uid, {'tts.placement.enabled': False})
    return uid, project, calls


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
def test_retry_uses_temporary_factor_without_changing_settings_or_request_fingerprint(index_prepared, monkeypatch, level):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}'
    key = 'tts.indextts25.duration_factor'
    if level == 'global':
        config.set_key(key, .6)
        # 已有项目主动应用当前全局，恢复本项覆盖后才使用新的基础值。
        from dubforge import configuration
        plan = configuration.binding_plan([uid], '')
        configuration.bind(dict(uids=[uid], template_id='', fingerprint=plan['fingerprint'], confirmed=True))
        assert client.put(base + '/step_params', json={'updates': {}, 'reset': [key]}).status_code == 200
    elif level == 'step':
        assert client.put(base + '/step_params', json={'updates': {key: .6}}).status_code == 200
    else:
        assert client.put(base + '/workbench/lines/1/params', json={'updates': {key: .6}}).status_code == 200
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    before_overrides = line_settings.overrides(project, line)
    before_fingerprint = line_fingerprint(project, line)
    before_global = config.get(key)
    before_source = client.get(base + '/workbench/lines/1/params').json()['sources'][key]
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
    assert line_settings.effective_config(project, line, deps.cfg)(key) == .6
    assert line_settings.overrides(project, line) == before_overrides
    assert deps.cfg(key) == (.6 if level != 'line' else 1.1)
    assert config.get(key) == before_global
    assert client.get(base + '/workbench/lines/1/params').json()['sources'][key] == before_source
    assert line_fingerprint(project, line) == before_fingerprint
    data = dubbing_edit.read_take(crud.project_dir(uid), line, 1)
    assert data['settings']['duration_factor'] == .5
    assert data['fingerprint'] == before_fingerprint
    assert dubbing_edit.take_path(crud.project_dir(uid), 1).with_suffix('.input').read_text() == before_fingerprint
    assert data['duration_retry']['retries'] == 1 and data['duration_retry']['available'] == 3
    assert line_cache_valid(project, line, crud.project_dir(uid))
    current = client.get(base + '/workbench?probe=false').json()['lines'][0]['wav']
    assert current['duration_retry']['used_factor'] == .5 and not current['settings_changed']
    assert synthesize(client, base + '/workbench/lines/1/synthesize', json={}).status_code == 200
    assert calls == [.6, .5], '未修改设置时复用正式结果，不为临时因子重做'
    synthesize(client, base + '/workbench/lines/1/synthesize', json={'force': True})
    assert calls == [.6, .5, .6, .5], '强制重新合成仍从用户配置的初始因子开始'
    assert client.put(base + '/workbench/lines/1/params', json={'updates': {key: .65}}).status_code == 200
    project = crud.get_project(uid)
    assert not line_cache_valid(project, line, crud.project_dir(uid))
    assert client.get(base + '/workbench?probe=false').json()['lines'][0]['wav']['settings_changed']


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


def test_batch_records_best_temporary_factor_without_changing_any_line_settings(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    key = 'tts.indextts25.duration_factor'
    other = 'tts.indextts25.temperature'
    client.put(base + '/lines/1/params', json={'updates': {other: .55}})
    calls = []

    def generate(url, text, out, lang, **kwargs):
        # 最后一次更慢，版本快照必须记录所保留音频的 .7，而非最后尝试的 .5。
        calls.append((text, kwargs['duration_factor']))
        wav(out, {1.1: 5, .7: 3.5, .5: 4}[kwargs['duration_factor']])
        return out

    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    synthesize(client, base + '/synthesize', json={'line_ids': [1, 2], 'force': True})
    project = crud.get_project(uid)
    deps = Dependencies(project)
    lines = deps.dubbing_lines()
    assert calls == [('你好', 1.1), ('你好', .7), ('你好', .5),
                     ('世界', 1.1), ('世界', .7), ('世界', .5)]
    assert line_settings.overrides(project, lines[0]) == {other: .55}
    assert line_settings.overrides(project, lines[1]) == {}
    for line in lines:
        assert line_cache_valid(project, line, crud.project_dir(uid))
        data = dubbing_edit.read_take(crud.project_dir(uid), line, 1)
        assert data['settings']['duration_factor'] == .7
        assert data['duration_retry']['used_factor'] == .7
        assert data['duration_retry']['attempts'][0]['factor'] == 1.1
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


def test_keep_existing_with_empty_history_retains_shorter_audio_without_inventing_factor(tmp_path):
    output = tmp_path / '1.wav'
    wav(output, 2)
    previous = output.read_bytes()
    result = tts_duration.synthesize(lambda path, factor: wav(path, 3), output,
        {'id': 1, 'start': 0, 'end': 1}, [.7], existing={},
        log=lambda _: None, on_attempt=lambda *args: None, can_retry=lambda: True)
    assert output.read_bytes() == previous
    assert result['seconds'] == 2 and result['used_factor'] is None
    assert result['attempts'] == [{'factor': .7, 'seconds': 3}]


@pytest.mark.parametrize('candidate,expected_seconds,expected_factor', [(3., 2., 1.1), (1.3, 1.3, .7)])
def test_legacy_voice_without_retry_history_uses_actual_snapshot_factor_and_keeps_shortest(
        index_prepared, monkeypatch, candidate, expected_seconds, expected_factor):
    import json
    from dubforge.backends import tts_indextts25
    from dubforge.pipeline import timing_repair
    from dubforge.pipeline.step_tts import synth_line
    client, uid, _ = index_prepared
    calls = []

    def generate(url, text, output, lang, **kwargs):
        factor = kwargs['duration_factor']
        calls.append(factor)
        wav(output, 2. if len(calls) == 1 else candidate)
        return output

    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    assert synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={}).status_code == 200
    project, root = crud.get_project(uid), crud.project_dir(uid)
    line = Dependencies(project).dubbing_lines()[0]
    output = dubbing_edit.take_path(root, 1, 1)
    record = dubbing_edit.read_take(root, line, 1)
    record.pop('duration_retry')
    atomic_write_bytes(output.with_suffix('.take.json'), json.dumps(record).encode())
    original_audio = output.read_bytes()
    assert client.put(f'/api/projects/{uid}/step_params', json={
        'updates': {'tts.indextts25.duration_factor': 1.6}}).status_code == 200
    project = crud.get_project(uid)
    synthetic, get = timing_repair.context(project, line, root, 1, Dependencies(project).cfg)
    synth_line(synthetic, line, root, force=True, cfg_get=get,
               timing={'available': 1.4, 'factors': [.7], 'keep_existing': True})
    current = dubbing_edit.read_take(root, line, 1)
    assert calls == [1.1, .7]
    assert tts_duration.audio_seconds(output) == pytest.approx(expected_seconds)
    assert current['settings']['duration_factor'] == expected_factor
    assert current['duration_retry']['used_factor'] == expected_factor
    assert current['duration_retry']['attempts'] == [{'factor': .7, 'seconds': candidate}]
    assert Dependencies(crud.get_project(uid)).cfg('tts.indextts25.duration_factor') == 1.6
    if candidate > 2:
        assert output.read_bytes() == original_audio


def test_keep_existing_rejects_unknown_historical_factor_instead_of_using_current_setting(index_prepared, monkeypatch):
    import json
    from dubforge.backends import tts_indextts25
    from dubforge.pipeline.step_tts import synth_line
    client, uid, _ = index_prepared
    assert synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={}).status_code == 200
    project, root = crud.get_project(uid), crud.project_dir(uid)
    line = Dependencies(project).dubbing_lines()[0]
    output = dubbing_edit.take_path(root, 1, 1)
    record = dubbing_edit.read_take(root, line, 1)
    record.pop('duration_retry')
    record['settings'].pop('duration_factor')
    atomic_write_bytes(output.with_suffix('.take.json'), json.dumps(record).encode())
    original_audio = output.read_bytes()
    monkeypatch.setattr(tts_indextts25, 'synth', lambda *args, **kwargs: pytest.fail('历史因子不明时不得请求模型'))
    with pytest.raises(DubForgeError) as caught:
        synth_line(project, line, root, force=True, cfg_get=Dependencies(project).cfg,
                   timing={'available': .5, 'factors': [.7], 'keep_existing': True})
    assert caught.value.code == 'TTS_TIMING_REPAIR_UNAVAILABLE'
    assert output.read_bytes() == original_audio
