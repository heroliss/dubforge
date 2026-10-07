"""空参考在本地拒绝；只在收到服务明确终态时释放占用，不重复提交模型请求。"""
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import soundfile as sf

from dubforge import tts_requests
from dubforge.backends import tts_indextts25
from dubforge.pipeline import step_tts
from test_dubbing_preparation import prepared


def reference_file(path, frames=3200):
    sf.write(path, [.1] * frames, 32000)
    return path


@pytest.mark.parametrize('start,end', [(327.13, 325.64), (2, 2), (-1, 2),
                                       (float('nan'), 2), (1, float('inf'))])
def test_invalid_sentence_time_does_not_cut_audio(tmp_path, monkeypatch, start, end):
    cut = Mock()
    monkeypatch.setattr(step_tts, '_cut', cut)
    with pytest.raises(step_tts.DubbingTimingError, match='句 114 时间轴无效'):
        step_tts._resolve_ref(None, 'dynamic', '', {'id': 114, 'start': start, 'end': end},
                              tmp_path / 'vocal.wav', tmp_path / 'refs', 'timbre')
    cut.assert_not_called()


def test_batch_rejects_later_invalid_time_before_first_model_request(prepared, monkeypatch):
    from conftest import wait_status
    from dubforge.domain import crud
    from dubforge.pipeline import step_dubtask
    from dubforge.runner import runner
    uid, project, calls = prepared
    build = step_dubtask.build_task

    def with_invalid_second_line(ctx):
        task = build(ctx)
        task['lines'][1]['end'] = 2
        return task

    monkeypatch.setattr(step_dubtask, 'build_task', with_invalid_second_line)
    runner.start_step(uid, 'tts')
    assert wait_status(uid, 'tts', {'failed'}) == 'failed'
    step = crud.get_step(project.id, 'tts')
    assert step.error_code == 'TTS_TIMING_INVALID'
    assert '句 2' in step.error_message
    assert calls == []


def test_empty_slice_beyond_source_is_not_saved(tmp_path):
    source = reference_file(tmp_path / 'source.wav')
    target = tmp_path / 'slice.wav'
    with pytest.raises(tts_indextts25.ReferenceAudioError, match='没有音频帧'):
        step_tts._cut(source, target, 10, 1)
    assert not target.exists()
    assert not target.with_suffix('.wav.tmp').exists()


def test_invalid_large_cached_reference_is_regenerated_atomically(tmp_path):
    source = reference_file(tmp_path / 'source.wav')
    target = reference_file(tmp_path / 'slice.wav', frames=0)
    # 文件尺寸不能代替波形检查；残留大文件也可能没有音频帧。
    target.write_bytes(target.read_bytes() + b'\0' * 2048)
    assert sf.info(target).frames == 0
    step_tts._cut(source, target, 0, .05)
    assert sf.info(target).frames == 1600
    assert not target.with_suffix('.wav.tmp').exists()


def test_reference_slice_keeps_subcentisecond_precision(tmp_path):
    source = reference_file(tmp_path / 'source.wav')
    target = step_tts._cut(source, tmp_path / 'slice.wav', .010125, .003)
    assert sf.info(target).frames == 96


@pytest.mark.parametrize('part', ['音色', '情感'])
@pytest.mark.parametrize('frames', [1, 96, 799])
def test_too_short_model_reference_is_rejected_before_connection(isolated_env, monkeypatch, tmp_path, part, frames):
    import gradio_client
    client = Mock()
    monkeypatch.setattr(gradio_client, 'Client', client)
    valid = reference_file(tmp_path / 'valid.wav')
    short = reference_file(tmp_path / 'short.wav', frames=frames)
    output = tmp_path / 'out.wav'
    output.write_bytes(b'previous audio')
    with pytest.raises(tts_indextts25.ReferenceAudioError, match=part + '参考音频过短') as caught:
        tts_indextts25.synth('http://localhost:7860', '你好', output, 'ZH',
            timbre_ref=short if part == '音色' else valid, emo_ref=short if part == '情感' else None)
    assert caught.value.detail['duration_seconds'] == frames / 32000
    assert caught.value.detail['minimum_seconds'] == .025
    assert f'{frames / 32000:.6f} 秒' in caught.value.message
    client.assert_not_called()
    assert not tts_requests._path('http://localhost:7860').exists()
    assert output.read_bytes() == b'previous audio'


def test_exact_feature_frame_duration_is_accepted(tmp_path):
    source = reference_file(tmp_path / 'frame.wav', frames=800)
    tts_indextts25.validate_reference(source, '音色', minimum_seconds=.025)


@pytest.mark.parametrize('decoded_samples,accepted', [(0, False), (1, False), (399, False),
                                                     (400, True), (1600, True)])
def test_ffmpeg_preset_validation_requires_same_minimum_decoded_duration(monkeypatch, tmp_path, decoded_samples, accepted):
    source = tmp_path / 'reference.m4a'
    source.write_bytes(b'unsupported-container-fixture')
    monkeypatch.setattr(tts_indextts25.sf, 'info', Mock(side_effect=RuntimeError('容器不受 libsndfile 支持')))
    decoded = Mock(return_value=SimpleNamespace(stdout=b'\0\0' * decoded_samples))
    monkeypatch.setattr(tts_indextts25.subprocess, 'run', decoded)
    if accepted:
        tts_indextts25.validate_reference(source, '音色', minimum_seconds=.025)
    else:
        with pytest.raises(tts_indextts25.ReferenceAudioError):
            tts_indextts25.validate_reference(source, '音色', minimum_seconds=.025)
    args = decoded.call_args.args[0]
    assert args[args.index('-ar') + 1] == '16000'
    assert args[args.index('-t') + 1] == '0.1'
    assert decoded.call_args.kwargs['timeout'] == 15


def test_valid_ffmpeg_supported_preset_is_accepted_when_soundfile_cannot_read_it(tmp_path):
    import subprocess
    source = reference_file(tmp_path / 'source.wav')
    preset = tmp_path / 'reference.m4a'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), str(preset)],
                   check=True, capture_output=True)
    with pytest.raises(sf.LibsndfileError):
        sf.info(preset)
    tts_indextts25.validate_reference(preset, '音色')


@pytest.mark.parametrize('invalid', ['empty', 'unreadable', 'missing'])
@pytest.mark.parametrize('part', ['音色', '情感'])
def test_invalid_reference_never_opens_model_connection(isolated_env, monkeypatch, tmp_path, invalid, part):
    import gradio_client
    valid = reference_file(tmp_path / 'valid.wav')
    bad = tmp_path / 'invalid.wav'
    if invalid == 'empty':
        reference_file(bad, frames=0)
    elif invalid == 'unreadable':
        bad.write_bytes(b'not audio')
    client = Mock()
    monkeypatch.setattr(gradio_client, 'Client', client)
    with pytest.raises(tts_indextts25.ReferenceAudioError, match=part + '参考音频无效'):
        tts_indextts25.synth('http://localhost:7860', '你好', tmp_path / 'out.wav', 'ZH',
            timbre_ref=bad if part == '音色' else valid, emo_ref=bad if part == '情感' else None)
    client.assert_not_called()
    assert not tts_requests._path('http://localhost:7860').exists()


@pytest.mark.parametrize('hidden', [False, True])
def test_server_failure_releases_request_and_keeps_diagnostic(isolated_env, monkeypatch, tmp_path, hidden):
    import gradio_client
    from gradio_client.exceptions import AppError
    reason = ('The upstream Gradio app has raised an exception but has not enabled verbose error reporting.'
              if hidden else 'cannot reshape tensor of 0 elements into shape [-1, 0]')
    client = Mock()
    client.submit.return_value.status.return_value = SimpleNamespace(progress_data=None)
    client.submit.return_value.result.side_effect = AppError(reason)
    connect = Mock(return_value=client)
    monkeypatch.setattr(gradio_client, 'Client', connect)
    monkeypatch.setattr(gradio_client, 'handle_file', lambda path: path)
    monkeypatch.setattr(tts_indextts25, 'is_online', lambda url: True)
    detach = Mock()
    monkeypatch.setattr(tts_requests.Request, 'detach', detach)
    output = tmp_path / 'out.wav'
    output.write_bytes(b'previous audio')
    with pytest.raises(tts_indextts25.IndexTTSRequestError) as caught:
        tts_indextts25.synth('http://localhost:7860', '你好', output, 'ZH',
                            timbre_ref=reference_file(tmp_path / 'reference.wav'))
    error = caught.value
    assert error.code == 'TTS_REQUEST_FAILED' and error.detail == reason
    assert '服务端合成失败' in error.message
    assert ('服务未返回原因' in error.message) == hidden
    assert '无需为此重启' in error.suggestion
    detach.assert_not_called()
    client.submit.assert_called_once()
    client.close.assert_called_once()
    assert not tts_requests._path('http://localhost:7860').exists()
    assert output.read_bytes() == b'previous audio'
    assert connect.call_args.kwargs['httpx_kwargs']['trust_env'] is False


def test_transport_failure_keeps_unknown_remote_request_even_if_client_future_done(isolated_env, monkeypatch, tmp_path):
    import gradio_client
    client = Mock()
    job = client.submit.return_value
    job.status.return_value = SimpleNamespace(progress_data=None)
    job.done.return_value = True
    job.result.side_effect = ConnectionError('连接中断')
    monkeypatch.setattr(gradio_client, 'Client', Mock(return_value=client))
    monkeypatch.setattr(gradio_client, 'handle_file', lambda path: path)
    monkeypatch.setattr(tts_indextts25, 'is_online', lambda url: True)
    observers = []
    monkeypatch.setattr(tts_requests.threading, 'Thread', lambda **kwargs:
                        SimpleNamespace(start=lambda: observers.append(kwargs['target'])))
    with pytest.raises(tts_indextts25.IndexTTSRequestError):
        tts_indextts25.synth('http://localhost:7860', '你好', tmp_path / 'out.wav', 'ZH',
                            timbre_ref=reference_file(tmp_path / 'reference.wav'))
    assert tts_requests.needs_recovery('http://localhost:7860')
    assert len(observers) == 1
    observers[0]()
    assert tts_requests.needs_recovery('http://localhost:7860')
    tts_requests.reset('http://localhost:7860')


def test_detached_request_releases_on_server_failure_receipt(isolated_env):
    from gradio_client.exceptions import AppError
    url = 'http://localhost:7860'
    request = tts_requests.Request(url)
    job, closed = Future(), threading.Event()
    request.detach(job, SimpleNamespace(close=closed.set), lambda message: None)
    job.set_exception(AppError('参考音频无效'))
    assert closed.wait(2)
    assert not tts_requests.needs_recovery(url)
    assert not request.path.exists()


def test_managed_runner_enables_error_receipts_without_changing_explicit_launch_settings():
    from dubforge.backends.indextts_runner import enable_error_details

    class Blocks:
        def launch(self, *args, **kwargs):
            return kwargs

    enable_error_details(Blocks)
    launch = Blocks.launch
    enable_error_details(Blocks)
    assert Blocks.launch is launch
    assert Blocks().launch(server_port=7860) == {'server_port': 7860, 'show_error': True}
    assert Blocks().launch(show_error=False)['show_error'] is False
