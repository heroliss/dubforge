"""真实切片验证：短句动态参考不能悄悄借用视频开头的声音。"""
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import threading

import numpy as np
import pytest
import soundfile as sf

from test_dubbing_preparation import index_prepared, prepared
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline import step_tts
from dubforge.pipeline.step_tts import _cut, _resolve_ref, synth_line
from dubforge.backends import tts_indextts25
from dubforge.utils.fs import atomic_replace


def vocal_file(path, *, tail=-.25):
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.concatenate([np.full(32000 * 8, .25), np.full(32000 * 5, tail)])
    tmp = path.with_suffix('.wav.tmp')
    sf.write(tmp, samples, 32000, format='WAV')
    atomic_replace(tmp, path)


@pytest.fixture
def cuts(monkeypatch):
    calls = []
    run = step_tts.subprocess.run

    def track(args, **kwargs):
        calls.append(args)
        return run(args, **kwargs)

    monkeypatch.setattr(step_tts.subprocess, 'run', track)
    return calls


@pytest.mark.parametrize('tag', ['timbre', 'emo'])
@pytest.mark.parametrize('seconds', [.3, 1.84, 2.5])
def test_dynamic_uses_exact_sentence_even_when_short(tmp_path, tag, seconds):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    logs = []
    line = {'id': 9, 'start': 10, 'end': 10 + seconds}
    path = _resolve_ref(None, 'dynamic', '', line, source, tmp_path / 'refs', tag, logs.append)
    samples, rate = sf.read(path)
    assert len(samples) / rate == pytest.approx(seconds, abs=.01)
    assert samples.mean() == pytest.approx(-.25, abs=.001)
    assert path.parent == tmp_path / 'refs'
    assert path.with_suffix('.input').is_file()
    assert '本句 10.00' in logs[-1]
    assert ('不足 2 秒' in logs[-1]) == (seconds < 2)


def test_stable_uses_selected_original_sentence(tmp_path):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    path = _resolve_ref(None, 'stable', '', {'id': 1}, source, tmp_path / 'refs', 'timbre',
                        reference_line={'id': 7, 'start': 10, 'end': 12})
    samples, rate = sf.read(path)
    assert len(samples) / rate == 2
    assert samples.mean() == pytest.approx(-.25, abs=.001)


@pytest.mark.parametrize('tag', ['timbre', 'emo'])
@pytest.mark.parametrize('mode,seconds', [('preset', 13), ('stable', 8)])
def test_dynamic_limit_does_not_trim_custom_or_selected_reference(tmp_path, tag, mode, seconds):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    # 极短逐句上限不能截断自定义文件，也不能改变固定开头参考。
    path = _resolve_ref(None, mode, str(source), {'id': 9, 'start': 10, 'end': 11},
                        source, tmp_path / 'refs', tag, max_seconds=.5,
                        reference_line={'id': 1, 'start': 1, 'end': 9})
    samples, rate = sf.read(path)
    assert len(samples) / rate == seconds
    if mode == 'preset':
        assert path == source


def test_long_dynamic_reference_is_capped_for_low_vram_s2mel(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    calls = []
    ref = tmp_path / 'refs' / 'dynamic.wav'
    monkeypatch.setattr(step_tts, '_cut',
                        lambda src, dst, start, dur: calls.append((start, dur)) or ref)
    _resolve_ref(None, 'dynamic', '', {'id': 9, 'start': 10, 'end': 20},
                 source, tmp_path / 'refs', 'timbre')
    assert calls == [(10, step_tts.DYNAMIC_REFERENCE_MAX_SECONDS)]


def test_dynamic_reference_limit_can_be_overridden(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    calls = []
    ref = tmp_path / 'refs' / 'dynamic.wav'
    monkeypatch.setattr(step_tts, '_cut',
                        lambda src, dst, start, dur: calls.append((start, dur)) or ref)
    _resolve_ref(None, 'dynamic', '', {'id': 9, 'start': 10, 'end': 20},
                 source, tmp_path / 'refs', 'timbre', max_seconds=10)
    assert calls == [(10, 10.0)]


def test_dynamic_reference_limit_is_bounded_to_service_window(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    calls = []
    ref = tmp_path / 'refs' / 'dynamic.wav'
    monkeypatch.setattr(step_tts, '_cut',
                        lambda src, dst, start, dur: calls.append((start, dur)) or ref)
    _resolve_ref(None, 'dynamic', '', {'id': 9, 'start': 10, 'end': 40},
                 source, tmp_path / 'refs', 'timbre', max_seconds=30)
    assert calls == [(10, 15.0)]


@pytest.mark.parametrize('mode', ['dynamic', 'stable', 'preset'])
def test_same_references_reach_model_with_requested_emotion(index_prepared, cuts, mode):
    client, uid, received = index_prepared
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    source = root / 'audio/vocal.wav'
    vocal_file(source)
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': mode, 'tts.indextts25.emo_mode': mode,
        'tts.indextts25.timbre_audio': str(source), 'tts.indextts25.emo_audio': str(source),
        'tts.indextts25.emo_weight': .37,
        'tts.indextts25.duration_retry_factors': ''}})
    project = crud.get_project(uid)
    line = {**Dependencies(project).dubbing_lines()[0], 'start': 10, 'end': 10.5}
    synth_line(project, line, root)
    args = received[-1]
    assert args['timbre_ref'] == args['emo_ref']
    assert args['emo_mode'] == mode and args['emo_weight'] == .37
    assert len(cuts) == (0 if mode == 'preset' else 1)
    samples, rate = sf.read(args['timbre_ref'])
    assert len(samples) / rate == (.5 if mode == 'dynamic' else 2 if mode == 'stable' else 13)
    if mode == 'dynamic':
        assert samples.mean() == pytest.approx(-.25, abs=.001)


def test_stable_reference_is_shared_across_sentence_fingerprints(index_prepared, cuts):
    client, uid, received = index_prepared
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': 'stable', 'tts.indextts25.emo_mode': 'stable',
        'tts.indextts25.duration_retry_factors': ''}})
    project, root = crud.get_project(uid), crud.project_dir(uid)
    vocal_file(root / 'audio/vocal.wav')
    for line in Dependencies(project).dubbing_lines():
        synth_line(project, line, root)
    assert len(received) == 2 and len(cuts) == 1
    assert received[0]['timbre_ref'] == received[1]['timbre_ref']


def test_different_reference_ranges_remain_separate(tmp_path, cuts):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    line = {'id': 9, 'start': 10, 'end': 10.5}
    stable = _resolve_ref(None, 'stable', '', line, source, tmp_path / 'refs', 'timbre',
                          reference_line={'id': 1, 'start': 1, 'end': 3})
    dynamic = _resolve_ref(None, 'dynamic', '', line, source, tmp_path / 'refs', 'emo')
    assert stable != dynamic and len(cuts) == 2
    assert sf.read(stable)[0].mean() == pytest.approx(.25, abs=.001)
    assert sf.read(dynamic)[0].mean() == pytest.approx(-.25, abs=.001)


def test_changing_sentence_time_creates_a_new_reference(tmp_path, cuts):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    line = {'id': 9, 'start': 10, 'end': 10.5}
    before = _resolve_ref(None, 'dynamic', '', line, source, tmp_path / 'refs', 'timbre')
    after = _resolve_ref(None, 'dynamic', '', {**line, 'end': 11}, source, tmp_path / 'refs', 'timbre')
    assert before != after and len(cuts) == 2
    assert sf.info(before).duration == .5 and sf.info(after).duration == 1


def test_replacing_source_invalidates_reference_even_at_same_size(tmp_path, cuts):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    line = {'id': 9, 'start': 10, 'end': 10.5}
    before = _resolve_ref(None, 'dynamic', '', line, source, tmp_path / 'refs', 'timbre')
    version = source.stat()
    vocal_file(source, tail=.5)
    os.utime(source, ns=(version.st_atime_ns, version.st_mtime_ns + 1_000_000))
    assert source.stat().st_size == version.st_size
    after = _resolve_ref(None, 'dynamic', '', line, source, tmp_path / 'refs', 'timbre')
    assert before != after and len(cuts) == 2
    assert sf.read(after)[0].mean() == pytest.approx(.5, abs=.001)


def test_cut_rechecks_source_and_range_with_same_destination(tmp_path, cuts):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    destination = tmp_path / 'slice.wav'
    _cut(source, destination, 0, .5)
    _cut(source, destination, 0, .5)
    assert len(cuts) == 1
    _cut(source, destination, 10, .5)
    assert len(cuts) == 2
    assert sf.read(destination)[0].mean() == pytest.approx(-.25, abs=.001)
    vocal_file(source, tail=.5)
    _cut(source, destination, 10, .5)
    assert len(cuts) == 3
    assert sf.read(destination)[0].mean() == pytest.approx(.5, abs=.001)


def test_cut_retries_transient_ffmpeg_failure(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    destination = tmp_path / 'slice.wav'
    original = step_tts.subprocess.run
    attempts = 0

    def flaky(args, **kwargs):
        nonlocal attempts
        if str(args[-1]).endswith('.wav.tmp') and attempts == 0:
            attempts += 1
            raise subprocess.CalledProcessError(1, args, stderr=b'transient ffmpeg failure')
        return original(args, **kwargs)

    monkeypatch.setattr(step_tts.subprocess, 'run', flaky)
    _cut(source, destination, 10, .5)
    assert attempts == 1
    assert sf.info(destination).duration == .5
    assert not list(destination.parent.glob('.reference-*.wav.tmp'))


def test_dynamic_reference_falls_back_to_stable_after_slice_failure(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    original = step_tts._cut
    calls = []

    def fail_dynamic(src, dst, start, dur):
        calls.append((start, dur))
        if start == 10:
            raise tts_indextts25.ReferenceAudioError('动态区间没有可用帧')
        return original(src, dst, start, dur)

    monkeypatch.setattr(step_tts, '_cut', fail_dynamic)
    logs = []
    path = _resolve_ref(None, 'dynamic', '', {'id': 31, 'start': 10, 'end': 10.5},
                        source, tmp_path / 'refs', 'timbre', logs.append,
                        reference_line={'id': 1, 'start': 1, 'end': 3})
    assert sf.info(path).duration == 2
    assert calls == [(10, .5), (1.0, 2.0)]
    assert any('回退指定参考句' in item for item in logs)


def test_dynamic_reference_falls_back_to_preset_when_stable_slice_fails(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    preset = tmp_path / 'preset.wav'
    vocal_file(preset, tail=.1)

    def fail_all(src, dst, start, dur):
        raise tts_indextts25.ReferenceAudioError('切片失败')

    monkeypatch.setattr(step_tts, '_cut', fail_all)
    logs = []
    path = _resolve_ref(None, 'dynamic', str(preset), {'id': 31, 'start': 10, 'end': 10.5},
                        source, tmp_path / 'refs', 'emo', logs.append)
    assert path == preset
    assert any('回退备用音频' in item for item in logs)


def test_audio_without_input_marker_is_not_reused(tmp_path, cuts):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    destination = tmp_path / 'slice.wav'
    _cut(source, destination, 10, .5)
    destination.with_suffix('.input').unlink()
    _cut(source, destination, 10, .5)
    assert len(cuts) == 2


def test_invalid_audio_with_matching_input_marker_is_regenerated(tmp_path, cuts):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    destination = tmp_path / 'slice.wav'
    _cut(source, destination, 10, .5)
    sf.write(destination, [], 32000)
    destination.write_bytes(destination.read_bytes() + b'\0' * 2048)
    assert sf.info(destination).frames == 0
    _cut(source, destination, 10, .5)
    assert len(cuts) == 2
    assert sf.info(destination).duration == .5


@pytest.mark.parametrize('invalid', ['missing', 'unreadable'])
def test_invalid_source_has_reference_error(tmp_path, invalid):
    source = tmp_path / 'vocal.wav'
    if invalid == 'unreadable':
        source.write_bytes(b'not audio')
    with pytest.raises(tts_indextts25.ReferenceAudioError) as caught:
        _resolve_ref(None, 'stable', '', {'id': 1}, source, tmp_path / 'refs', 'timbre',
                     reference_line={'id': 1, 'start': 1, 'end': 3})
    assert caught.value.code == 'REF_AUDIO_INVALID'
    assert '原声' in caught.value.message
    assert not list((tmp_path / 'refs').glob('*.tmp'))


def test_parallel_same_reference_uses_independent_temporary_files(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    run = step_tts.subprocess.run
    barrier, temporary = threading.Barrier(2), []

    def concurrent_run(args, **kwargs):
        temporary.append(args[-1])
        barrier.wait(timeout=10)
        return run(args, **kwargs)

    monkeypatch.setattr(step_tts.subprocess, 'run', concurrent_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_resolve_ref, None, 'stable', '', {'id': number},
                   source, tmp_path / 'refs', 'timbre',
                   reference_line={'id': 1, 'start': 1, 'end': 3}) for number in (1, 2)]
        paths = [future.result(timeout=15) for future in futures]
    assert paths[0] == paths[1]
    assert len(set(temporary)) == 2
    assert sf.info(paths[0]).duration == 2
    assert paths[0].with_suffix('.input').is_file()
    assert not list((tmp_path / 'refs').glob('*.tmp'))


def test_selected_reference_caps_at_model_window_and_records_actual_source(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    calls, resolved = [], {}
    monkeypatch.setattr(step_tts, '_cut', lambda src, dst, start, dur: calls.append((start, dur)) or dst)
    _resolve_ref(None, 'stable', '', {'id': 9}, source, tmp_path / 'refs', 'timbre',
                 reference_line={'id': 2, 'start': 3, 'end': 25}, resolved=resolved)
    assert calls == [(3., 15.)]
    assert resolved['timbre']['line_id'] == 2
    assert resolved['timbre']['start'] == 3 and resolved['timbre']['duration'] == 15
    assert resolved['timbre']['source_version']


def test_stable_missing_reference_does_not_silently_use_opening_or_custom(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    monkeypatch.setattr(step_tts, '_cut', lambda *args: pytest.fail('不得偷偷切开头'))
    with pytest.raises(tts_indextts25.ReferenceAudioError, match='参考句'):
        _resolve_ref(None, 'stable', str(source), {'id': 9}, source, tmp_path / 'refs', 'timbre')


def test_separate_timbre_and_emotion_reference_sentences_reach_model(index_prepared):
    client, uid, received = index_prepared
    response = client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': 'stable', 'tts.indextts25.emo_mode': 'stable',
        'tts.indextts25.timbre_reference_line_id': 2, 'tts.indextts25.emo_reference_line_id': 1,
        'tts.indextts25.duration_retry_factors': ''}})
    assert response.status_code == 200, response.text
    project, root = crud.get_project(uid), crud.project_dir(uid)
    vocal_file(root / 'audio/vocal.wav')
    source_line = Dependencies(project).dubbing_lines()[0]
    # 正式字幕的手动/自动播放位置不能挪动被选作参考的原声句。
    line = {**source_line, 'start': 10, 'end': 11, 'voice_timing': [0, 2]}
    synth_line(project, line, root)
    refs = received[-1]
    assert refs['timbre_ref'] != refs['emo_ref']
    snapshot = step_tts.dubbing_edit.read_take(root, line, 1)['settings']['resolved_references']
    assert snapshot['timbre']['line_id'] == 2 and snapshot['timbre']['start'] == 3.
    assert snapshot['emo']['line_id'] == 1 and snapshot['emo']['start'] == 0.


def test_only_selected_source_sentence_changes_all_stable_consumers(index_prepared):
    client, uid, _ = index_prepared
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': 'stable', 'tts.indextts25.emo_mode': 'same',
        'tts.indextts25.timbre_reference_line_id': 1}})
    project = crud.get_project(uid)
    fixed = {'id': 8, 'text': '固定目标', 'start': 10, 'end': 11}
    before = step_tts.line_fingerprint(project, fixed)
    path = Dependencies(project).path('split', 'split.txt')
    original = path.read_text(encoding='utf-8')
    path.write_text(original.replace('3.000-5.000', '3.000-6.000'), encoding='utf-8')
    assert step_tts.line_fingerprint(project, fixed) == before
    path.write_text(original.replace('Hello', 'Recognized correction'), encoding='utf-8')
    assert step_tts.line_fingerprint(project, fixed) == before
    path.write_text(original.replace('0.000-2.000', '0.000-2.500'), encoding='utf-8')
    assert step_tts.line_fingerprint(project, fixed) != before


@pytest.mark.parametrize('mode', ['preset', 'dynamic'])
def test_unused_reference_selectors_preserve_audio_and_step_fingerprint(index_prepared, mode):
    from dubforge.runner import compute_fingerprint
    client, uid, _ = index_prepared
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': mode, 'tts.indextts25.emo_mode': 'same'}})
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    before = step_tts.line_fingerprint(project, line), compute_fingerprint(project, 'tts')
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_reference_line_id': 2, 'tts.indextts25.emo_reference_line_id': 2}})
    project = crud.get_project(uid)
    assert (step_tts.line_fingerprint(project, line), compute_fingerprint(project, 'tts')) == before


def test_historical_actual_reference_replays_old_range_without_new_selection(tmp_path):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    resolved = {}
    before = _resolve_ref(None, 'stable', '', {'id': 9}, source, tmp_path / 'refs', 'timbre',
                          reference_line={'id': 2, 'start': 10, 'end': 12}, resolved=resolved)
    after = _resolve_ref(None, 'stable', '', {'id': 9}, source, tmp_path / 'refs', 'timbre',
                         reference_line={'id': 1, 'start': 0, 'end': 2}, frozen_reference=resolved['timbre'])
    assert before == after
    version = source.stat()
    os.utime(source, ns=(version.st_atime_ns, version.st_mtime_ns + 1_000_000))
    with pytest.raises(tts_indextts25.ReferenceAudioError, match='已变化'):
        _resolve_ref(None, 'stable', '', {'id': 9}, source, tmp_path / 'refs', 'timbre',
                     frozen_reference=resolved['timbre'])


def test_legacy_dynamic_fallback_is_confined_to_historical_replay(tmp_path, monkeypatch):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    original = step_tts._cut
    def cut(src, dst, start, duration):
        if start == 10:
            raise tts_indextts25.ReferenceAudioError('历史动态区间失败')
        return original(src, dst, start, duration)
    monkeypatch.setattr(step_tts, '_cut', cut)
    resolved, logs = {}, []
    old = _resolve_ref(None, 'dynamic', '', {'id': 9, 'start': 10, 'end': 11}, source,
                       tmp_path / 'refs', 'timbre', logs.append,
                       reference_line={'id': 1, 'start': 1, 'end': 3},
                       resolved=resolved, legacy_dynamic=True)
    assert sf.info(old).duration == 8
    assert resolved['timbre']['mode'] == 'legacy_opening'
    assert any('历史开头 8 秒' in text for text in logs)
    new = _resolve_ref(None, 'dynamic', '', {'id': 9, 'start': 10, 'end': 11}, source,
                       tmp_path / 'refs', 'timbre',
                       reference_line={'id': 1, 'start': 1, 'end': 3})
    assert sf.info(new).duration == 2 and old != new


def test_actual_dynamic_fallback_binds_only_first_source_sentence(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': 'dynamic', 'tts.indextts25.emo_mode': 'same',
        'tts.indextts25.duration_retry_factors': ''}})
    project, root = crud.get_project(uid), crud.project_dir(uid)
    vocal_file(root / 'audio/vocal.wav')
    line = Dependencies(project).dubbing_lines()[1]
    original_cut = step_tts._cut
    def cut(src, dst, start, duration):
        if start == 3:
            raise tts_indextts25.ReferenceAudioError('本句区间失败')
        return original_cut(src, dst, start, duration)
    monkeypatch.setattr(step_tts, '_cut', cut)
    synth_line(project, line, root)
    assert step_tts.line_cache_valid(project, line, root)
    ref = step_tts.dubbing_edit.read_take(root, line, 1)['settings']['resolved_references']['timbre']
    assert ref['fallback'] and ref['line_id'] == 1
    path = Dependencies(project).path('split', 'split.txt')
    original = path.read_text(encoding='utf-8')
    path.write_text(original.replace('0.000-2.000', '0.000-2.500'), encoding='utf-8')
    assert not step_tts.line_cache_valid(project, line, root)


def test_missing_selected_source_sentence_blocks_synthesis_without_fallback(index_prepared):
    client, uid, received = index_prepared
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': 'stable', 'tts.indextts25.timbre_reference_line_id': 99}})
    project, root = crud.get_project(uid), crud.project_dir(uid)
    vocal_file(root / 'audio/vocal.wav')
    with pytest.raises(tts_indextts25.ReferenceAudioError, match='第 99 句已不存在'):
        synth_line(project, Dependencies(project).dubbing_lines()[0], root)
    assert not received


def test_new_reference_defaults_do_not_change_other_backend_fingerprint(make_project, monkeypatch):
    import copy
    from dubforge import config
    from dubforge.runner import compute_fingerprint
    uid = make_project(params={'step_params': {
        'tts.backend': 'gptsovits', 'tts.indextts25.timbre_mode': 'stable'}})
    project = crud.get_project(uid)
    before = compute_fingerprint(project, 'tts')
    older = copy.deepcopy(config.DEFAULTS['tts']['indextts25'])
    older.pop('timbre_reference_line_id')
    older.pop('emo_reference_line_id')
    monkeypatch.setitem(config.DEFAULTS['tts'], 'indextts25', older)
    assert compute_fingerprint(project, 'tts') == before
