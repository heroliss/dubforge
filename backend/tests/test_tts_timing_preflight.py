"""文字估时只提示；真实任务仍按有效时间轴和完整 WAV 检查，不连接外部模型。"""
import time
from pathlib import Path

import pytest
import soundfile as sf

from test_configuration import save
from test_dubbing_preparation import index_prepared, prepared
from test_voice_timing_integration import fake_index, report, start_selected, timeline
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import step_dubtask, voice_timing
from dubforge.pipeline.translation_alignment import audit, blocking_issues
from dubforge.runner import runner
from dubforge.utils.fs import atomic_write_bytes


LONG_TEXT = '这是一句比较长但实际配音仍可能自然放下的译文'


def long_translation(uid):
    timeline(uid, [(1, 2), (2, 4)])
    project, root = crud.get_project(uid), crud.project_dir(uid)
    path = root / 'artifacts/translate/translate.txt'
    atomic_write_bytes(path, f'1. {LONG_TEXT}\n2. 好\n'.encode())
    crud.add_artifact(project.id, 'translate', 'text', path.name,
                      path, str(path.relative_to(root)))


def finish(uid):
    result = runner.start_step(uid, 'tts', dubbing={'mode': 'fill'})
    assert result['status'] == 'started', result
    deadline = time.monotonic() + 15
    while runner.current_job_id(crud.get_project(uid).id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    return crud.latest_job(crud.get_project(uid).id, 'tts')


@pytest.mark.parametrize('scope', ['fill', 'selected'])
@pytest.mark.parametrize('auto_repair', [False, True])
def test_estimated_overrun_is_advisory_and_actual_audio_can_slide(
        index_prepared, monkeypatch, scope, auto_repair):
    client, uid, _ = index_prepared
    monkeypatch.setattr(runner, '_prepare_gpu', lambda *_: None)
    long_translation(uid)
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    save('project:' + uid, {'tts.placement.auto_repair': auto_repair})
    # 预检同时含单句严重估时偏长和连续句长短差异；均不构成错位的证据。
    estimate = audit([{'id': 1, 'start': 1, 'end': 2, 'text': LONG_TEXT},
                      {'id': 2, 'start': 2, 'end': 4, 'text': '好'}])
    assert {row['kind'] for row in blocking_issues(estimate)} == {
        'target_duration_overrun', 'sequence_shift_suspect'}
    calls = fake_index(monkeypatch, lambda text, factor: 1.5 if text == LONG_TEXT else .5)
    job = finish(uid) if scope == 'fill' else start_selected(client, uid)
    assert job.status == 'done', job.error_detail
    assert calls == [(LONG_TEXT, 1.1), ('好', 1.1)]
    timing = report(uid)
    assert timing['ready'] and timing['shifted'] > 0
    assert not timing['unresolved_ids']
    log = Path(job.log_path).read_text(encoding='utf-8')
    assert '第 1 句估算超出相邻起点间隔' in log
    assert '不是实际配音时长' in log and '不能据此认定跨行错位' in log
    assert '尚未开始批量合成' not in log


def test_invalid_time_still_fails_before_first_model_request(index_prepared, monkeypatch):
    _, uid, _ = index_prepared
    monkeypatch.setattr(runner, '_prepare_gpu', lambda *_: None)
    long_translation(uid)
    build = step_dubtask.build_task

    def invalid_second_line(ctx):
        task = build(ctx)
        task['lines'][1]['end'] = task['lines'][1]['start']
        return task

    monkeypatch.setattr(step_dubtask, 'build_task', invalid_second_line)
    calls = fake_index(monkeypatch, lambda *_: .5)
    job = finish(uid)
    assert job.status == 'failed'
    assert crud.get_step(crud.get_project(uid).id, 'tts').error_code == 'TTS_TIMING_INVALID'
    assert calls == []


@pytest.mark.parametrize('auto_repair', [False, True])
def test_actual_audio_conflict_still_fails_and_keeps_complete_voices(
        index_prepared, monkeypatch, auto_repair):
    _, uid, _ = index_prepared
    monkeypatch.setattr(runner, '_prepare_gpu', lambda *_: None)
    long_translation(uid)
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 4)
    save('project:' + uid, {'tts.placement.auto_repair': auto_repair,
                           'tts.indextts25.duration_retry_factors': '.7,.5'})
    calls = fake_index(monkeypatch, lambda *_: 3.)
    job = finish(uid)
    assert job.status == 'failed'
    assert crud.get_step(crud.get_project(uid).id, 'tts').error_code == 'DUB_TIMING_UNRESOLVED'
    assert calls[:2] == [(LONG_TEXT, 1.1), ('好', 1.1)]
    if auto_repair:
        assert len(calls) > 2
        assert len(calls) == len(set(calls))  # 同句同因子有限尝试，不无限重试。
    else:
        assert len(calls) == 2
        assert '自动缩时修复已关闭' in job.error_detail
    assert not report(uid)['ready'] and report(uid)['unresolved_ids']
    for line_id in (1, 2):
        assert sf.info(crud.project_dir(uid) / f'artifacts/tts/{line_id}.wav').duration == 3.


def test_failed_first_request_reports_waiting_audio_not_zero_conflicts(
        index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    _, uid, _ = index_prepared
    monkeypatch.setattr(runner, '_prepare_gpu', lambda *_: None)
    long_translation(uid)
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    calls = []

    def fail(*args, **kwargs):
        calls.append(args[1])
        raise DubForgeError('测试请求超时', code='TTS_REQUEST_TIMEOUT')

    monkeypatch.setattr(tts_indextts25, 'synth', fail)
    job = finish(uid)
    assert job.status == 'failed' and calls == [LONG_TEXT]
    payload = report(uid)
    assert payload['missing'] == 2 and not payload['ready']
    assert payload['unresolved'] == 0
    log = Path(job.log_path).read_text(encoding='utf-8')
    assert '配音排布尚未完成：2 句所选音频缺失或不可用' in log
    assert '等待有效音频后重新排布' in log
    assert '仍有 0 句需缩短' not in log
