"""旧配音自动缩时计划保留声音快照、精调与人工位置，不暗换当前全局音色。"""
import json

import numpy as np
import pytest
import soundfile as sf

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, dubbing_policy, line_settings, timing_repair, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.fs import atomic_write_bytes
from test_configuration import save


@pytest.fixture
def voices(make_project, monkeypatch):
    uid = make_project()
    project, root = crud.get_project(uid), crud.project_dir(uid)
    reference = root / 'old-reference.wav'
    atomic_write_bytes(reference, b'old-reference')
    save('project:' + uid, {
        'tts.backend': 'indextts25', 'tts.indextts25.timbre_mode': 'preset',
        'tts.indextts25.timbre_audio': str(reference), 'tts.indextts25.emo_mode': 'same',
        'tts.indextts25.duration_factor': 1., 'tts.indextts25.duration_retry_factors': '0.8,0.6',
        'tts.placement.max_shift_seconds': 0.,
    })
    project = crud.get_project(uid)
    for key, name, text in [
        ('split', 'split.txt', '1. 0.000-1.000: Source one\n2. 1.000-2.000: Source two\n'),
        ('translate', 'translate.txt', '1. 第一行\n2. 第二行\n'),
    ]:
        path = root / 'artifacts' / key / name
        atomic_write_bytes(path, text.encode())
        crud.add_artifact(project.id, key, 'text', name, path, str(path.relative_to(root)))
        step = crud.get_step(project.id, key)
        step.status = 'done'
        crud.save_step(step)
    deps = Dependencies(project)
    lines = deps.dubbing_lines(required=True)
    for row in lines:
        path = dubbing_edit.take_path(root, row['id'], 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, np.zeros(2 * 8000), 8000)
        atomic_write_bytes(path.with_suffix('.input'), b'historical-fingerprint')
        dubbing_edit.record_take(project, row, root, 1, 'historical-fingerprint', {
            'timbre_mode': 'preset', 'timbre_audio': str(reference), 'emo_mode': 'same',
            'duration_factor': 1., 'lang': 'ZH', 'temperature': .4, 'top_p': .5,
            'emo_weight': 1., 'do_sample': True, 'top_k': 30, 'length_penalty': 0.,
            'num_beams': 3, 'repetition_penalty': 10., 'max_mel_tokens': 1500,
            'max_text_tokens_per_segment': 120, 'dynamic_reference_max_seconds': 5.,
        })
        crud.add_artifact(project.id, 'tts', 'audio', path.name, path, str(path.relative_to(root)))
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2.)
    return uid, root, lines


def rewrite_record(root, row, **values):
    record = dubbing_edit.read_take(root, row, 1)
    record.update(values)
    atomic_write_bytes(dubbing_edit.take_path(root, row['id'], 1).with_suffix('.take.json'),
                       json.dumps(record).encode())
    return record


def test_fill_policy_can_disable_timing_repairs_without_enabling_hidden_redo_fields():
    policy = dubbing_policy.normalize(dict(mode='fill', target='slot', slot=9,
        line_settings='project', include_manual_sources=True, repair_timing=False))
    assert policy == {**dubbing_policy.DEFAULT_POLICY, 'repair_timing': False}
    with pytest.raises(DubForgeError) as exc:
        dubbing_policy.normalize({'repair_timing': 1})
    assert exc.value.code == 'DUB_POLICY_INVALID'


def test_context_replays_snapshot_voice_and_preserves_current_runtime_protection(voices):
    uid, root, rows = voices
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params['tts_lines'] = {'1': {'identity': line_settings.identity(rows[0]), 'settings': {
        'tts.indextts25.temperature': 1.8, 'tts.indextts25.duration_factor': 1.8}}}
    project.params = json.dumps(params)
    crud.save_project(project)
    save('project:' + uid, {'tts.indextts25.url': 'http://127.0.0.1:9999',
        'tts.indextts25.request_timeout_seconds': 900., 'tts.indextts25.temperature': 1.6,
        'tts.indextts25.duration_retry_factors': '.7,.5'})
    project = crud.get_project(uid)
    record = dubbing_edit.read_take(root, rows[0], 1)
    rewrite_record(root, rows[0], duration_retry={'used_factor': .8, 'attempts': []})
    before = project.params
    synthetic, get = timing_repair.context(project, rows[0], root, 1, Dependencies(project).cfg)
    assert get('tts.backend') == 'indextts25'
    assert get('tts.indextts25.temperature') == .4
    assert get('tts.indextts25.top_p') == .5
    assert get('tts.indextts25.timbre_audio') == record['settings']['timbre_audio']
    assert get('tts.indextts25.duration_factor') == .8
    assert get('tts.indextts25.url') == 'http://127.0.0.1:9999'
    assert get('tts.indextts25.request_timeout_seconds') == 900.
    assert get('tts.indextts25.duration_retry_factors') == '.7,.5'
    assert 'tts_lines' not in crud.project_params(synthetic)
    assert project.params == before
    settings = get('tts.indextts25')
    settings['temperature'] = 999
    assert get('tts.indextts25.temperature') == .4


def test_context_requires_current_index_backend_and_provable_snapshot(voices):
    uid, root, rows = voices
    project = crud.get_project(uid)
    deps = Dependencies(project)
    with pytest.raises(timing_repair.TimingRepairError, match='引擎'):
        timing_repair.context(project, rows[0], root, 1,
                              lambda key, default=None: 'gptsovits' if key == 'tts.backend' else deps.cfg(key, default))
    dubbing_edit.take_path(root, 1, 1).with_suffix('.take.json').unlink()
    with pytest.raises(timing_repair.TimingRepairError, match='快照'):
        timing_repair.context(project, rows[0], root, 1, deps.cfg)


@pytest.mark.parametrize('settings', [{'character': '旧GPT音色'}, {'timbre_mode': 'preset', 'duration_factor': True},
    {'timbre_mode': 'wrong', 'duration_factor': 1.}])
def test_unsupported_or_invalid_voice_snapshots_are_not_guessed(voices, settings):
    uid, root, rows = voices
    project = crud.get_project(uid)
    rewrite_record(root, rows[0], settings=settings)
    with pytest.raises(timing_repair.TimingRepairError) as exc:
        timing_repair.context(project, rows[0], root, 1, Dependencies(project).cfg)
    assert exc.value.code == 'TTS_TIMING_REPAIR_UNAVAILABLE'


@pytest.mark.parametrize('missing', ['do_sample', 'top_k', 'temperature', 'num_beams',
                                    'max_mel_tokens', 'max_text_tokens_per_segment'])
def test_partial_sampling_snapshot_is_blocked_without_filling_current_values(voices, missing):
    uid, root, rows = voices
    project = crud.get_project(uid)
    record = dubbing_edit.read_take(root, rows[0], 1)
    record['settings'].pop(missing)
    rewrite_record(root, rows[0], settings=record['settings'])
    deps = Dependencies(project)
    assert deps.cfg(f'tts.indextts25.{missing}') is not None
    with pytest.raises(timing_repair.TimingRepairError, match=missing) as exc:
        timing_repair.context(project, rows[0], root, 1, deps.cfg)
    assert exc.value.code == 'TTS_TIMING_REPAIR_UNAVAILABLE'
    inspected = timing_repair.inspect(deps)
    assert not any(row['id'] == 1 for row in inspected['repair_rows'])
    assert any(row['id'] == 1 and missing in row['reason'] for row in inspected['blocked'])


def test_remaining_factors_consider_actual_snapshot_used_factor_and_successful_attempts(voices):
    uid, root, rows = voices
    project = crud.get_project(uid)
    record = dubbing_edit.read_take(root, rows[0], 1)
    record['settings']['duration_factor'] = .7
    record['duration_retry'] = {'used_factor': .8, 'attempts': [{'factor': .6, 'seconds': 2.2}]}
    get = lambda key, default=None: '.8,.7,.6,.5' if key.endswith('duration_retry_factors') else .8
    assert timing_repair.remaining_factors(record, get) == [.5]
    record['duration_retry']['attempts'].append({'factor': .5, 'seconds': 3.})
    assert timing_repair.remaining_factors(record, get) == []


def test_inspect_proposes_only_needed_valid_dub_and_respects_excluded_generation_ids(voices):
    uid, root, _ = voices
    deps = Dependencies(crud.get_project(uid))
    result = timing_repair.inspect(deps)
    assert result['unresolved_ids'] == [1, 2]
    assert [(row['id'], row['take'], row['kind'], row['force']) for row in result['repair_rows']] == [
        (1, 1, 'repair', False), (2, 1, 'repair', False)]
    assert not result['blocked'] and not result['requirements']
    assert result['repair_rows'][0]['repair_factors'] == [.8, .6]
    assert [row['id'] for row in timing_repair.inspect(deps, exclude_ids=[1])['repair_rows']] == [2]
    assert all(dubbing_edit.take_path(root, id, 1).is_file() for id in (1, 2))


def test_inspect_reports_unprovable_or_exhausted_voices_without_touching_them(voices):
    uid, root, rows = voices
    old = dubbing_edit.take_path(root, 1, 1).read_bytes()
    dubbing_edit.take_path(root, 1, 1).with_suffix('.take.json').unlink()
    rewrite_record(root, rows[1], duration_retry={'used_factor': .6, 'attempts': [{'factor': .6, 'seconds': 2.}]})
    result = timing_repair.inspect(Dependencies(crud.get_project(uid)))
    assert not result['repair_rows']
    assert {item['id'] for item in result['blocked']} == {1, 2}
    assert dubbing_edit.take_path(root, 1, 1).read_bytes() == old


def test_snapshot_reference_requirements_do_not_use_current_new_timbre(voices):
    uid, root, rows = voices
    (root / 'old-reference.wav').unlink()
    new = root / 'new-reference.wav'
    atomic_write_bytes(new, b'new-reference')
    save('project:' + uid, {'tts.indextts25.timbre_audio': str(new)})
    result = timing_repair.inspect(Dependencies(crud.get_project(uid)))
    assert result['repair_rows']
    assert any(item.get('file') == str(root / 'old-reference.wav') for item in result['requirements'])
    assert not any(item.get('file') == str(new) for item in result['requirements'])


def test_original_silent_and_manual_order_conflicts_are_not_auto_revoiced(voices):
    uid, root, rows = voices
    project = crud.get_project(uid)
    dubbing_edit.save_selection(project, rows[0], 'original', None)
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params['playback_timing_edits'] = {'2': {'identity': line_settings.identity(rows[1]), 'start': 0.}}
    project.params = json.dumps(params)
    crud.save_project(project)
    result = timing_repair.inspect(Dependencies(project))
    assert not result['repair_rows']
    assert result['blocked'] == [{'id': 2, 'reason': '人工播放起点导致顺序或间隔冲突，请移动起点或恢复自动排布。'}]


def test_policy_preview_and_prepare_add_repair_without_regenerating_all_or_creating_slot(voices):
    uid, _, _ = voices
    project = crud.get_project(uid)
    summary = dubbing_policy.preview(project)
    assert (summary['generate'], summary['repair'], summary['reuse']) == (0, 2, 0)
    assert summary['repair_ids'] == [1, 2]
    assert summary['new_slot'] is None and not summary['overwrite']
    prepared = dubbing_policy.prepare(project, Dependencies(project), operation_id='repair-round')
    assert [(row['id'], row['kind'], row['take']) for row in prepared['lines']] == [(1, 'repair', 1), (2, 'repair', 1)]
    off = dubbing_policy.preview(project, {'repair_timing': False})
    assert (off['generate'], off['repair'], off['reuse']) == (0, 0, 2)


def test_recovery_arranges_tts_when_only_reused_voice_timing_needs_repair(voices):
    uid, _, _ = voices
    summary = dubbing_policy.preview(crud.get_project(uid))
    recovery = dict(rows=[{'key': 'assemble'}], execute_steps=['assemble'], reusable_steps=['tts'])
    result = dubbing_policy.apply_recovery(summary, recovery)
    assert result['execute_steps'] == ['tts', 'assemble']
    assert result['start_step'] == 'tts'
    assert '自动缩时修复 2 句' in result['reason']


def test_old_stable_snapshot_requires_explicit_redo_instead_of_switching_speaker(voices):
    uid, root, rows = voices
    record = dubbing_edit.read_take(root, rows[0], 1)
    record['settings']['timbre_mode'] = 'stable'
    rewrite_record(root, rows[0], settings=record['settings'])
    project = crud.get_project(uid)
    with pytest.raises(timing_repair.TimingRepairError, match='旧版开头原声'):
        timing_repair.context(project, rows[0], root, 1, Dependencies(project).cfg)
    assert dubbing_edit.snapshot_current(root, rows[0], 1)


def test_old_dynamic_snapshot_keeps_legacy_reference_contract_for_repair(voices):
    from dubforge.pipeline.step_tts import LEGACY_REFERENCE_KEY
    uid, root, rows = voices
    record = dubbing_edit.read_take(root, rows[0], 1)
    record['settings']['timbre_mode'] = 'dynamic'
    rewrite_record(root, rows[0], settings=record['settings'])
    project = crud.get_project(uid)
    _, get = timing_repair.context(project, rows[0], root, 1, Dependencies(project).cfg)
    assert get(f'tts.indextts25.{LEGACY_REFERENCE_KEY}') is True


def test_new_reference_snapshot_replays_actual_range_and_rejects_changed_source(voices):
    import os
    from dubforge.pipeline.step_tts import _reference_fingerprint
    uid, root, rows = voices
    vocal = root / 'audio/vocal.wav'
    vocal.parent.mkdir(parents=True, exist_ok=True)
    sf.write(vocal, np.zeros(4 * 8000), 8000)
    record = dubbing_edit.read_take(root, rows[0], 1)
    saved_ref = dict(mode='sentence', line_id=2, start=1., duration=1.,
                     source_version=_reference_fingerprint(vocal, 1., 1.))
    record['settings'].update(timbre_mode='stable', resolved_references={'timbre': saved_ref})
    rewrite_record(root, rows[0], settings=record['settings'])
    save('project:' + uid, {'tts.indextts25.timbre_reference_line_id': 1})
    project = crud.get_project(uid)
    _, get = timing_repair.context(project, rows[0], root, 1, Dependencies(project).cfg)
    assert get('tts.indextts25.resolved_references')['timbre'] == saved_ref
    version = vocal.stat()
    os.utime(vocal, ns=(version.st_atime_ns, version.st_mtime_ns + 1_000_000))
    with pytest.raises(timing_repair.TimingRepairError, match='原声参考文件已变化'):
        timing_repair.context(project, rows[0], root, 1, Dependencies(project).cfg)
