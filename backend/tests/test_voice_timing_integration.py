"""真实任务、选槽、字幕及成片共同消费局部配音时钟；模型仅写隔离 WAV。"""
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from conftest import synthesize
from test_configuration import save
from test_dubbing_preparation import index_prepared, prepared
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, step_assemble, step_subtitles, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_tts import line_fingerprint
from dubforge.runner import compute_fingerprint, runner
from dubforge.utils.fs import atomic_write_bytes


def timeline(uid, intervals):
    project, root = crud.get_project(uid), crud.project_dir(uid)
    for key, text in (
        ('split', ''.join(f'{i + 1}. {start:.3f}-{end:.3f}: Source {i + 1}\n'
                          for i, (start, end) in enumerate(intervals))),
        ('translate', ''.join(f'{i + 1}. {word}\n' for i, word in
                              enumerate(('你好', '世界', '再见', '早上好', '谢谢')[:len(intervals)]))),
    ):
        path = root / 'artifacts' / key / f'{key}.txt'
        atomic_write_bytes(path, text.encode())
        crud.add_artifact(project.id, key, 'text', path.name, path, str(path.relative_to(root)))


def fake_index(monkeypatch, duration):
    from dubforge.backends import tts_indextts25
    calls = []

    def generate(url, text, output, language, **kwargs):
        factor = kwargs['duration_factor']
        calls.append((text, factor))
        seconds = duration(text, factor)
        # 保留不同值以区分最短版本；不会占用真实模型或 GPU。
        sf.write(output, np.full(round(seconds * 32000), factor / 10, np.float32),
                 32000, subtype='PCM_16', format='WAV')
        return output

    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    return calls


def start_selected(client, uid, **body):
    response = client.post(f'/api/projects/{uid}/workbench/synthesize',
                           json={'line_ids': [1, 2], 'force': True, **body})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 15
    project = crud.get_project(uid)
    while runner.current_job_id(project.id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    return crud.latest_job(project.id, 'tts')


def report(uid):
    return json.loads((crud.project_dir(uid) / 'artifacts/tts/voice_timing.json').read_text(encoding='utf-8'))


def context(uid, version=None):
    deps = Dependencies(crud.get_project(uid))
    return SimpleNamespace(project=deps.project, pdir=deps.root, root=deps.root,
                           upstream=deps.upstream, cfg=deps.cfg, version=version,
                           check_cancel=lambda: None, log=lambda _: None,
                           register_file=lambda *args: None)


def test_selected_all_generates_natural_speed_before_group_sliding_without_early_retry(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(1, 2), (2, 3)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    calls = fake_index(monkeypatch, lambda _text, _factor: 1.5)
    job = start_selected(client, uid)
    assert job.status == 'done', job.error_detail
    assert calls == [('你好', 1.1), ('世界', 1.1)]
    timing = report(uid)
    assert timing['schema'] == 2 and timing['ready'] and timing['shifted'] == 2
    rows = timing['lines']
    assert rows[0]['applied_start'] < 1 < rows[0]['applied_end']
    assert rows[0]['applied_end'] + .08 <= rows[1]['applied_start'] + 1e-7
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    assert [row['playback_start'] for row in state['lines']] == [row['applied_start'] for row in rows]
    # 原始校对起止没有被派生播放时钟覆盖。
    assert [row['start'] for row in state['lines']] == [1, 2]


def test_group_layout_retries_only_actual_new_long_sentence_after_all_natural_attempts(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    calls = fake_index(monkeypatch, lambda text, factor: .5 if text == '世界' else
                       2 if factor == 1.1 else 1.3)
    job = start_selected(client, uid)
    assert job.status == 'done', job.error_detail
    assert calls == [('你好', 1.1), ('世界', 1.1), ('你好', .7)]
    assert report(uid)['ready']
    line = Dependencies(crud.get_project(uid)).dubbing_lines()[0]
    saved = dubbing_edit.read_take(crud.project_dir(uid), line, 1)
    assert saved['duration_retry']['used_factor'] == .7
    assert [a['factor'] for a in saved['duration_retry']['attempts']] == [1.1, .7]
    assert Dependencies(crud.get_project(uid)).cfg('tts.indextts25.duration_factor') == 1.1


def test_full_flow_precheck_discloses_existing_conflicts_without_overwriting_or_starting_tts(index_prepared, monkeypatch):
    from dubforge.workflow_plan import production_checks
    client, uid, _ = index_prepared
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    calls = fake_index(monkeypatch, lambda text, _factor: 2 if text == '你好' else .5)
    save('project:' + uid, {'tts.placement.enabled': False,
                           'tts.indextts25.duration_retry_factors': ''})
    assert start_selected(client, uid).status == 'done'
    save('project:' + uid, {'tts.placement.enabled': True})
    deps = Dependencies(crud.get_project(uid))
    issues = production_checks(deps, ['assemble'], dubbing_summary={'generate': 0})
    assert any('配音排布' in issue and '自动修复' in issue for issue in issues)
    assert not production_checks(deps, ['assemble'], dubbing_summary={'generate': 2})
    assert len(calls) == 2


def test_non_monotonic_shortening_keeps_shortest_audio_and_never_repeats_factor(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    calls = fake_index(monkeypatch, lambda text, factor: .5 if text == '世界' else
                       {1.1: 2, .7: 1.8, .5: 1.9}[factor])
    job = start_selected(client, uid)
    assert job.status == 'failed'
    assert crud.get_step(crud.get_project(uid).id, 'tts').error_code == 'DUB_TIMING_UNRESOLVED'
    assert calls == [('你好', 1.1), ('世界', 1.1), ('你好', .7), ('你好', .5)]
    root = crud.project_dir(uid)
    assert sf.info(dubbing_edit.take_path(root, 1, 1)).duration == pytest.approx(1.8)
    saved = dubbing_edit.read_take(root, Dependencies(crud.get_project(uid)).dubbing_lines()[0], 1)
    assert saved['settings']['duration_factor'] == .7
    assert saved['duration_retry']['used_factor'] == .7
    assert [a['factor'] for a in saved['duration_retry']['attempts']] == [1.1, .7, .5]
    assert report(uid)['unresolved_ids'] == [1]


def test_saved_fine_tuned_audio_is_not_regenerated_by_fill_policy(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    calls = fake_index(monkeypatch, lambda _text, _factor: .8)
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    assert start_selected(client, uid).status == 'done'
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {
        'tts.indextts25.temperature': .55}}).status_code == 200
    assert client.post(base + '/lines/1/confirm', json={'confirmed': True}).status_code == 200
    root = crud.project_dir(uid)
    before = dubbing_edit.file_version(dubbing_edit.take_path(root, 1, 1))
    runner.start_step(uid, 'tts', dubbing={'mode': 'fill'})
    assert calls == [('你好', 1.1), ('世界', 1.1)]
    assert dubbing_edit.file_version(dubbing_edit.take_path(root, 1, 1)) == before
    state = client.get(base + '?probe=false').json()
    assert state['lines'][0]['customized'] and state['lines'][0]['confirmed']


def test_partial_generation_does_not_repair_old_fine_tuned_conflicting_take(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    calls = fake_index(monkeypatch, lambda text, _factor: 2 if text == '你好' else .5)
    save('project:' + uid, {'tts.placement.enabled': False,
                           'tts.indextts25.duration_retry_factors': ''})
    assert start_selected(client, uid).status == 'done'
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {
        'tts.indextts25.temperature': .55}}).status_code == 200
    assert client.post(base + '/lines/1/confirm', json={'confirmed': True}).status_code == 200
    root = crud.project_dir(uid)
    old = dubbing_edit.take_path(root, 1, 1).read_bytes()
    save('project:' + uid, {'tts.placement.enabled': True,
                           'tts.indextts25.duration_retry_factors': '.7, .5'})
    assert synthesize(client, base + '/lines/2/synthesize', json={'force': True}).status_code == 200
    assert calls == [('你好', 1.1), ('世界', 1.1), ('世界', 1.1)]
    assert dubbing_edit.take_path(root, 1, 1).read_bytes() == old
    assert report(uid)['unresolved_ids'] == [1]
    state = client.get(base + '?probe=false').json()
    assert state['lines'][0]['customized'] and state['lines'][0]['confirmed']


def test_new_slot_whole_flow_uses_layout_for_new_selected_takes_without_touching_old_audio(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(1, 2), (2, 3)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    calls = fake_index(monkeypatch, lambda _text, _factor: 1.5)
    assert start_selected(client, uid).status == 'done'
    root = crud.project_dir(uid)
    old = {i: dubbing_edit.take_path(root, i, 1).read_bytes() for i in (1, 2)}
    base = f'/api/projects/{uid}/workbench'
    assert client.post(base + '/lines/1/confirm', json={'confirmed': True}).status_code == 200
    response = runner.start_step(uid, 'tts', dubbing={'mode': 'all', 'target': 'new'})
    assert response['status'] == 'started'
    deadline = time.monotonic() + 15
    while runner.current_job_id(crud.get_project(uid).id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert crud.latest_job(crud.get_project(uid).id, 'tts').status == 'done'
    state = client.get(base + '?probe=false').json()
    assert [row['selection']['take'] for row in state['lines']] == [2, 2]
    assert report(uid)['ready'] and {row['take'] for row in report(uid)['lines']} == {2}
    assert len(calls) == 4
    assert all(dubbing_edit.take_path(root, i, 1).read_bytes() == old[i] for i in old)
    assert state['lines'][0]['versions'][0]['confirmed']


def test_both_languages_and_bilingual_follow_workbench_clock_and_original_version_keeps_source(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(1, 2), (2, 3)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    fake_index(monkeypatch, lambda _text, _factor: 1.5)
    assert start_selected(client, uid).status == 'done'
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    tracks, source = step_subtitles.build_tracks(context(uid), 'all')
    expected = [(row['playback_start'], row['playback_end']) for row in state['lines']]
    for name in ('source.srt', 'trans.srt', 'bilingual.srt'):
        assert [(line.start, line.end) for line in tracks[name]] == expected
    assert [(row['start'], row['end']) for row in source] == [(1, 2), (2, 3)]
    original_tracks, _ = step_subtitles.build_tracks(context(uid, {'dubbing': False}), 'all')
    for lines in original_tracks.values():
        assert [(line.start, line.end) for line in lines] == [(1, 2), (2, 3)]


def test_formal_unresolved_blocks_before_replacing_old_audio_but_preview_retains_complete_test_region(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    fake_index(monkeypatch, lambda text, factor: .5 if text == '世界' else 2)
    assert start_selected(client, uid).status == 'failed'
    save('project:' + uid, {'assemble.loudnorm': False})
    root = crud.project_dir(uid)
    old = root / 'audio/dub_full.wav'
    atomic_write_bytes(old, b'old complete audio')
    monkeypatch.setattr(step_assemble, '_require_dubbing', lambda *_: None)
    with pytest.raises(DubForgeError) as caught:
        step_assemble._mix_timeline(context(uid), root, 2)
    assert caught.value.code == 'DUB_TIMING_UNRESOLVED'
    assert old.read_bytes() == b'old complete audio'
    sample = step_assemble._mix_timeline(context(uid), root, 2, start=0, end=1)
    assert sf.info(sample).duration == 1
    assert old.read_bytes() == b'old complete audio'


def test_actual_mixed_track_and_browser_share_shifted_audio_boundaries(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(1, 2), (2, 3)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    fake_index(monkeypatch, lambda _text, _factor: 1.5)
    assert start_selected(client, uid).status == 'done'
    save('project:' + uid, {'assemble.loudnorm': False})
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    mixed = step_assemble._mix_timeline(context(uid), crud.project_dir(uid), 6)
    audio, sr = sf.read(mixed)
    assert len(audio) == 6 * sr
    for line in state['lines']:
        midpoint = (line['playback_start'] + line['playback_end']) / 2
        assert abs(audio[round(midpoint * sr)]) > .01
    gap_midpoint = (state['lines'][0]['playback_end'] + state['lines'][1]['playback_start']) / 2
    assert abs(audio[round(gap_midpoint * sr)]) < .001
    assert not state['placement']['issues']


def test_placement_changes_and_selected_slot_change_refresh_outputs_not_line_voice_identity(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 6)
    fake_index(monkeypatch, lambda _text, _factor: .8)
    assert start_selected(client, uid).status == 'done'
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    audio_identity = line_fingerprint(project, line)
    before = {key: compute_fingerprint(project, key) for key in ('subtitles', 'assemble')}
    save('project:' + uid, {'tts.placement.max_shift_seconds': 1})
    project = crud.get_project(uid)
    assert line_fingerprint(project, line) == audio_identity
    after = {key: compute_fingerprint(project, key) for key in before}
    assert all(after[key] != before[key] for key in before)
    response = synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize',
                          json={'take': 2, 'force': True})
    assert response.status_code == 200
    project = crud.get_project(uid)
    assert line_fingerprint(project, line) == audio_identity
    assert all(compute_fingerprint(project, key) != after[key] for key in after)
