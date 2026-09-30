"""转发服务阶段和实际迭代次数，不用队列 ETA 或等待时长填充百分比。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dubforge.backends import tts_indextts25


@pytest.mark.parametrize('values,expected', [
    ([None, 0, .1, .2, .2, .9], [0, .1, .2, .9]),
    ([.2, .65, .2, .6, .9], [.2, .65, .9]),
    ([None, None, None], []),
    ([-1, 2, float('nan'), float('inf'), '20%'], []),
])
def test_adapter_forwards_only_reported_progress(isolated_env, monkeypatch, tmp_path, values, expected):
    import gradio_client

    source = tmp_path / 'service.wav'
    source.write_bytes(b'audio')
    target = tmp_path / 'result.wav'
    job = Mock()
    job.status.side_effect = [SimpleNamespace(
        progress_data=None if value is None else [SimpleNamespace(progress=value)], eta=10
    ) for value in values]
    job.result.side_effect = [TimeoutError()] * (len(values) - 1) + [str(source)]
    job.done.return_value = False
    client = Mock()
    client.submit.return_value = job
    monkeypatch.setattr(gradio_client, 'Client', Mock(return_value=client))
    monkeypatch.setattr(gradio_client, 'handle_file', lambda path: path)
    monkeypatch.setattr(tts_indextts25, 'is_online', lambda url: True)
    monkeypatch.setattr(tts_indextts25.subprocess, 'run',
                        lambda command, **kwargs: Path(command[-1]).write_bytes(b'converted'))
    updates = []
    tts_indextts25.synth('http://localhost:7860', '你好', target, 'ZH',
                        timbre_ref=source, progress=updates.append)
    assert updates == expected
    assert target.read_bytes() == b'converted'
    assert not target.with_suffix('.wav.tmp').exists()
    assert client.submit.call_args.kwargs['api_name'] == '/gen_single'
    client.predict.assert_not_called()


@pytest.mark.parametrize('error', [TimeoutError('服务超时'), RuntimeError('服务断开')])
def test_service_errors_propagate_instead_of_polling_forever(error):
    job = Mock()
    job.status.return_value = SimpleNamespace(progress_data=None)
    job.result.side_effect = error
    job.done.return_value = True
    updates = []
    with pytest.raises(type(error), match=str(error)):
        tts_indextts25._result_with_progress(job, updates.append)
    assert updates == []
    assert job.result.call_count <= 2


def unit(*, progress=None, index=None, length=None, desc=None):
    return SimpleNamespace(progress=progress, index=index, length=length, desc=desc)


def test_diffusion_steps_refine_single_and_multiple_segments():
    read = tts_indextts25._reported_progress
    assert read([unit(progress=.2), unit(index=10, length=25, desc='DubForge speech 1/1')]) == pytest.approx(.48)
    assert read([unit(index=25, length=25, desc='DubForge speech 1/1')]) == pytest.approx(.9)
    assert read([unit(index=25, length=25, desc='DubForge speech 1/2')]) == pytest.approx(.55)
    # 每条迭代消息自带分段，轮询错过上一阶段消息也不影响计算。
    assert read([unit(index=10, length=25, desc='DubForge speech 2/2')]) == pytest.approx(.69)
    assert read([unit(index=25, length=25, desc='DubForge speech 2/2')]) == pytest.approx(.9)


@pytest.mark.parametrize('item', [
    unit(index=5, length=None, desc='DubForge speech 1/1'),
    unit(index=0, length=0, desc='DubForge speech 1/1'),
    unit(index=26, length=25, desc='DubForge speech 1/1'),
    unit(index=-1, length=25, desc='DubForge speech 1/1'),
    unit(index=True, length=25, desc='DubForge speech 1/1'),
    unit(index=float('nan'), length=25, desc='DubForge speech 1/1'),
    unit(index=1, length=float('inf'), desc='DubForge speech 1/1'),
    unit(index=1, length=25, desc='DubForge speech 0/1'),
    unit(index=1, length=25, desc='DubForge speech 2/1'),
    unit(index=1, length=25, desc='DubForge speech 1/0'),
    unit(index=10, length=25, desc='模型下载'),
])
def test_invalid_or_unrelated_iterations_do_not_invent_progress(item):
    assert tts_indextts25._reported_progress([item]) is None
    assert tts_indextts25._reported_progress([unit(progress=.2), item]) == .2


def test_progress_bridge_keeps_gradio_class_and_request_state_separate():
    from dubforge.backends.indextts_runner import enable_progress

    class Progress:
        def __init__(self, track_tqdm=False):
            self.track_tqdm = track_tqdm
        def __call__(self, progress, desc=None, *args, **kwargs):
            return progress, desc, args, kwargs
        def tqdm(self, iterable=None, desc=None, *args, **kwargs):
            return iterable, desc, args, kwargs

    enable_progress(Progress)
    init = Progress.__init__
    enable_progress(Progress)
    assert Progress.__init__ is init
    first, second = Progress(), Progress()
    assert isinstance(first, Progress) and first.track_tqdm
    assert not Progress(track_tqdm=False).track_tqdm
    assert first(.2, desc='speech synthesis 1/2...')[0] == .2
    assert first.tqdm(range(25), None, 25, 'steps', _tqdm='original')[1:] == (
        'DubForge speech 1/2', (25, 'steps'), {'_tqdm': 'original'})
    assert second.tqdm(range(25))[1] is None
    second(.2, desc='speech synthesis 1/1...')
    first(.55, desc='speech synthesis 2/2...')
    assert first.tqdm(range(25))[1] == 'DubForge speech 2/2'
    assert second.tqdm(range(25))[1] == 'DubForge speech 1/1'
    assert first.tqdm(range(2), '其他已有说明')[1] == '其他已有说明'
    first(.9, desc='saving audio...')
    assert first.tqdm(range(25))[1] is None


def test_keyword_only_default_progress_binds_a_fresh_tracker_per_request():
    from contextvars import Context, ContextVar
    from dubforge.backends.indextts_runner import enable_progress
    context = SimpleNamespace(event_id=ContextVar('event', default=None),
                              progress=ContextVar('tracker', default=None))

    class Progress:
        def __init__(self, track_tqdm=False):
            self.track_tqdm = track_tqdm
        def __call__(self, value, desc=None):
            return self, value, desc
        def tqdm(self, iterable=None, desc=None, *args, **kwargs):
            return desc

    enable_progress(Progress, context)
    default = Progress()

    def request(event, segment):
        context.event_id.set(event)
        tracker, value, _ = default(.2, desc=f'speech synthesis {segment}/2...')
        assert value == .2 and tracker is context.progress.get() and tracker is not default
        assert tracker.tqdm(range(25)) == f'DubForge speech {segment}/2'
        assert default(.3, desc=f'speech synthesis {segment}/2...')[0] is tracker
        return tracker

    first = Context().run(request, 'first', 1)
    second = Context().run(request, 'second', 2)
    assert first is not second and context.progress.get() is None
    assert default._dubforge_segment is None
