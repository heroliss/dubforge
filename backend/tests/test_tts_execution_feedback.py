"""只用真实进度协议和本地音频替身验证逐句反馈，不请求配音模型。"""
from types import SimpleNamespace

import pytest

from dubforge.execution_progress import Recorder, normalize
from dubforge.pipeline.step_tts import _SentenceExecution
from test_dubbing_preparation import index_prepared, prepared  # noqa: F401


def sentence_context(clock):
    recorder = Recorder(clock=lambda: clock[0])

    def report(code, label, *, detail='', state='running', completed=None, total=None, unit=None, **metrics):
        return recorder.phase(code, label, detail=detail, state=state,
                              completed=completed, total=total, unit=unit, metrics=metrics)

    return SimpleNamespace(report_execution=report), recorder


def test_real_sentence_updates_refresh_activity_without_completing_an_extra_sentence():
    clock = [1000.]
    ctx, recorder = sentence_context(clock)
    sentence = _SentenceExecution(ctx, 'synthesizing', '处理第 70 句配音',
                                  completed=69, total=305, current_id=70)
    clock[0] += 31
    assert recorder.snapshot()['inactive_seconds'] == 31
    assert sentence.update(.48)
    value = recorder.snapshot()
    assert value['inactive_seconds'] == 0
    assert (value['completed'], value['total'], value['unit']) == (69, 305, '句')
    assert value['metrics']['sentence_progress'] == .48
    assert value['metrics']['current_id'] == 70
    assert value['state'] == 'running'


@pytest.mark.parametrize('value', [None, True, '50%', -.1, 1.1, float('nan'), float('inf'), .2, .1])
def test_invalid_duplicate_and_regressing_updates_do_not_look_like_fresh_activity(value):
    clock = [1000.]
    ctx, recorder = sentence_context(clock)
    sentence = _SentenceExecution(ctx, 'synthesizing', '处理第 1 句配音', current_id=1)
    assert sentence.update(.2)
    clock[0] += 10
    assert sentence.update(value) is False
    assert recorder.snapshot()['inactive_seconds'] == 10
    assert recorder.snapshot()['metrics']['sentence_progress'] == .2


def test_attempt_reset_and_new_sentence_clear_the_previous_fraction_and_factor():
    clock = [1000.]
    ctx, recorder = sentence_context(clock)
    first = _SentenceExecution(ctx, 'synthesizing', '处理第 1 句配音', completed=0, total=2, current_id=1)
    first.attempt(0, 3, 1)
    first.update(.9)
    first.attempt(1, 3, .7)
    snapshot = recorder.snapshot()
    assert snapshot['metrics']['sentence_progress'] is None
    assert snapshot['metrics']['sentence_attempt'] == 2
    assert snapshot['metrics']['sentence_attempts'] == 3
    assert first.update(.2)
    first.close()
    second = _SentenceExecution(ctx, 'synthesizing', '处理第 2 句配音', completed=1, total=2, current_id=2)
    assert not first.update(1)
    snapshot = recorder.snapshot()
    assert snapshot['completed'] == 1
    assert snapshot['metrics']['current_id'] == 2
    assert snapshot['metrics']['sentence_progress'] is None
    assert snapshot['metrics']['sentence_attempt'] is None
    assert snapshot['metrics']['duration_factor'] is None
    assert second.update(.1)


def test_sentence_clock_is_calculated_on_read_without_refreshing_activity_and_freezes_on_stop():
    source = dict(state='running', phase='synthesizing', started_at=1000, updated_at=1010,
                  metrics=dict(sentence_started_at='1970-01-01T00:16:45+00:00', sentence_progress=.2))
    early = normalize(source, now=1040)
    late = normalize(source, now=1050)
    assert early['metrics']['sentence_elapsed_seconds'] == 35
    assert late['metrics']['sentence_elapsed_seconds'] == 45
    assert late['inactive_seconds'] == 40
    assert late['updated_at'] == early['updated_at']
    stopped = normalize(source, state='interrupted', finished_at=1020, now=2000)
    assert stopped['metrics']['sentence_elapsed_seconds'] == 15
    source['metrics']['sentence_started_at'] = 'invalid'
    source['metrics']['sentence_elapsed_seconds'] = 999
    assert 'sentence_elapsed_seconds' not in normalize(source, now=1050)['metrics']


def test_pipeline_progress_reaches_execution_before_audio_is_saved(index_prepared, monkeypatch):
    from threading import Event

    from conftest import wait_status
    from dubforge.backends import tts_indextts25
    from dubforge.domain import crud
    from dubforge.runner import runner

    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    entered, release = Event(), Event()
    original = tts_indextts25.synth
    callbacks = []

    def synth(*args, **kwargs):
        callbacks.append(kwargs['progress'])
        kwargs['progress'](.48)
        if len(callbacks) == 1:
            entered.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    runner.start_step(uid, 'tts')
    try:
        assert entered.wait(10)
        value = runner.execution_details(project.id, crud.get_steps(project.id))['tts']
        assert value['phase'] == 'synthesizing'
        assert (value['completed'], value['total']) == (0, 2)
        assert value['metrics']['sentence_progress'] == .48
        assert value['metrics']['current_id'] == 1
        assert value['metrics']['sentence_attempt'] == 1
        assert value['metrics']['sentence_elapsed_seconds'] >= 0
        assert not (crud.project_dir(uid) / 'artifacts/tts/1.wav').exists()
        saved = value['updated_at']
        callbacks[0](.48)
        assert runner.execution_details(project.id, crud.get_steps(project.id))['tts']['updated_at'] == saved
    finally:
        release.set()
    wait_status(uid, 'tts', {'done'})
    final = runner.execution_details(project.id, crud.get_steps(project.id))['tts']
    assert final['state'] == 'completed'
    callbacks[0](1)
    assert runner.execution_details(project.id, crud.get_steps(project.id))['tts'] == final
