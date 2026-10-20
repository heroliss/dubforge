"""统一时钟的消费者契约：有效音频、局部失败与双语字幕始终保持一致。"""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.errors import ConfigError
from dubforge.pipeline import dubbing_edit, voice_timing
from dubforge.pipeline.line_settings import identity


def cfg(values=None):
    return lambda key, default=None: (values or {}).get(key, default)


def cue(id=1, start=2.0, end=3.0, **values):
    return dict(id=id, start=start, end=end, text='测试译文', source_text='Source text',
                source='dub', take=1, **values)


@pytest.fixture()
def env(isolated_env):
    root = isolated_env / 'project'
    return SimpleNamespace(uid='test', source_kind='link', source_url=''), root


def wav(root, line, seconds=1):
    path = dubbing_edit.take_path(root, line['id'], line['take'])
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros(round(seconds * 8000)), 8000)
    return path


def allocation(rows, *, start_delta=-.5, ready=True, **kwargs):
    return [{**row, 'allocated_start': row['start'] + start_delta,
             'allocated_end': row['start'] + start_delta + (row['seconds'] or 1),
             'needs_scale': not ready, 'scale_factor': 1.0 if ready else .7,
             'window_ready': ready, 'window_id': 0,
             'window_start': 0, 'window_end': 10, 'window_lines': len(rows)} for row in rows]


def test_settings_defaults_and_explicit_values():
    assert voice_timing.settings(cfg()) == {
        'enabled': True, 'max_shift_seconds': 2.0,
        'window_lines': 9, 'inter_sentence_gap': .08}
    assert voice_timing.settings(cfg({'tts.placement.enabled': False,
                                     'tts.placement.max_shift_seconds': 0,
                                     'tts.placement.window_lines': 5,
                                     'tts.placement.inter_sentence_gap': .12})) == {
        'enabled': False, 'max_shift_seconds': 0.,
        'window_lines': 5, 'inter_sentence_gap': .12}


@pytest.mark.parametrize('key,value', [
    ('enabled', 'false'), ('max_shift_seconds', -1),
    ('max_shift_seconds', float('nan')), ('max_shift_seconds', True),
    ('window_lines', 0), ('window_lines', 2), ('window_lines', 16), ('window_lines', 3.5),
    ('inter_sentence_gap', -1), ('inter_sentence_gap', float('inf')),
])
def test_invalid_settings_have_domain_errors(key, value):
    with pytest.raises(ConfigError):
        voice_timing.settings(cfg({'tts.placement.' + key: value}))


def test_old_take_snapshot_cannot_retime_newly_edited_text(env):
    import json
    from dubforge.utils.fs import atomic_write_bytes
    project, root = env
    row = cue()
    path = wav(root, row, 1.5)
    atomic_write_bytes(path.with_suffix('.take.json'), json.dumps({
        'identity': identity({**row, 'text': '已经修改的旧译文'}),
        'version': dubbing_edit.file_version(path),
    }).encode())
    result = voice_timing.plan(project, root, [row], cfg(), video_duration=8)
    assert result['timing']['missing_ids'] == [1]
    assert result['timing']['diagnostics'][0]['kind'] == 'audio_unavailable'
    assert result['inputs'][0]['start'] == row['start']
    assert not result['timing']['lines'][0]['applied']


def test_reads_chosen_real_wav_and_preserves_source_identity(env, monkeypatch):
    project, root = env
    rows = [cue()]
    wav(root, rows[0], 1.5)
    original = deepcopy(rows)
    seen = {}

    def allocate(values, **kwargs):
        seen.update(kwargs)
        assert values[0]['seconds'] == pytest.approx(1.5)
        return allocation(values)

    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocate)
    result = voice_timing.plan(project, root, rows, cfg(), video_duration=10)
    assert rows == original
    adjusted = result['inputs'][0]
    assert (adjusted['start'], adjusted['end']) == (1.5, 3.0)
    assert adjusted['voice_timing'] == [2.0, 3.0]
    assert identity(adjusted) == identity(original[0])
    assert adjusted['text'] == original[0]['text']
    assert adjusted['source_text'] == original[0]['source_text']
    assert result['timing']['lines'][0]['version'] == dubbing_edit.file_version(
        dubbing_edit.take_path(root, 1, 1))
    assert result['timing']['shifted'] == 1
    assert seen == dict(max_shift_seconds=2., window_lines=9,
                        inter_sentence_gap=.08, video_duration=10)


def test_known_seconds_reused_without_probing_audio(env, monkeypatch):
    project, root = env
    monkeypatch.setattr(voice_timing, 'audio_seconds', lambda *_: pytest.fail('重复探测音频'))
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, [cue(seconds=1.2)], cfg())
    assert result['inputs'][0]['seconds'] == 1.2
    assert result['placement']['lines'][0]['audio_end'] == pytest.approx(2.7)


def test_audio_duration_alias_reused(env, monkeypatch):
    project, root = env
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, [cue(audio_duration=1.25)], cfg())
    assert result['inputs'][0]['seconds'] == 1.25


@pytest.mark.parametrize('flags', [{'exists': False}, {'wav': {'exists': False}}])
def test_unavailable_history_never_read_even_if_file_and_seconds_exist(env, monkeypatch, flags):
    project, root = env
    row = cue(seconds=3, **flags)
    wav(root, row, 3)
    monkeypatch.setattr(voice_timing, 'audio_seconds', lambda *_: pytest.fail('读取了过期配音'))
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, [row], cfg())
    assert result['inputs'][0]['seconds'] is None
    assert result['inputs'][0]['start'] == row['start']
    assert result['timing']['missing_ids'] == [1]
    assert result['timing']['ready'] is False
    assert result['placement']['lines'][0]['audio_end'] is None


def test_missing_and_invalid_audio_reported_without_fake_duration(env, monkeypatch):
    project, root = env
    invalid = cue(2, 5, 6)
    path = dubbing_edit.take_path(root, 2, 1)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'not audio')
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, [cue(), invalid], cfg())
    assert result['timing']['missing_ids'] == [1, 2]
    assert [d['kind'] for d in result['timing']['diagnostics']] == ['audio_missing', 'audio_invalid']
    assert all(row['seconds'] is None for row in result['inputs'])


def test_failed_component_does_not_disable_another_component_or_clip_audio(env, monkeypatch):
    project, root = env
    rows = [cue(1, 2., 3., seconds=1.), cue(2, 8., 9., seconds=4.)]

    def mixed(values, **kwargs):
        return [allocation(values[:1])[0],
                {**values[1], 'allocated_start': 8., 'allocated_end': 9.,
                 'needs_scale': True, 'scale_factor': .25, 'window_ready': False,
                 'window_id': 1}]

    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', mixed)
    result = voice_timing.plan(project, root, rows, cfg(), 15)
    assert result['inputs'][0]['start'] == 1.5
    assert result['inputs'][1]['start'] == 8.
    assert result['inputs'][1]['seconds'] == 4.
    assert result['placement']['lines'][1]['audio_end'] == 12.
    assert result['timing']['unresolved_ids'] == [2]
    assert result['timing']['ready'] is False
    assert result['timing']['lines'][1]['allocated_end'] == 9.
    assert result['timing']['lines'][1]['audio_end'] == 12.


def test_whole_failed_window_keeps_its_source_clock(env, monkeypatch):
    project, root = env
    rows = [cue(1, 2., 3., seconds=1.), cue(2, 3., 4., seconds=3.)]

    def failed(values, **kwargs):
        result = allocation(values)
        for row in result:
            row['window_ready'] = False
        result[1]['needs_scale'] = True
        return result

    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', failed)
    result = voice_timing.plan(project, root, rows, cfg())
    assert [r['start'] for r in result['inputs']] == [2., 3.]
    assert result['timing']['shifted'] == 0


def test_original_and_silent_are_fixed_without_reading_any_audio(env, monkeypatch):
    project, root = env
    rows = [{**cue(1, 2., 3.), 'source': 'original'},
            {**cue(2, 4., 5.), 'source': 'silent', 'take': None}]
    monkeypatch.setattr(voice_timing, 'audio_seconds', lambda *_: pytest.fail('读取了非配音声源'))
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, rows, cfg())
    assert [(r['start'], r['end']) for r in result['inputs']] == [(2., 3.), (4., 5.)]
    assert [r['seconds'] for r in result['inputs']] == [1., 0.]
    assert result['timing']['missing'] == 0


def test_disabled_preserves_source_clock_and_reports_existing_overlap(env, monkeypatch):
    project, root = env
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', lambda *_a, **_k: pytest.fail('关闭时仍排布'))
    result = voice_timing.plan(project, root, [cue(1, 2., 3., seconds=3.),
                                             cue(2, 4., 5., seconds=1.)],
                               cfg({'tts.placement.enabled': False}), 10)
    assert [r['start'] for r in result['inputs']] == [2., 4.]
    assert result['timing']['enabled'] is False
    assert any(i['kind'] == 'overlap' for i in result['placement']['issues'])


def test_source_translation_and_bilingual_apply_same_mapping(env, monkeypatch):
    project, root = env
    rows = [cue(seconds=1.2)]
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, rows, cfg())
    translated = voice_timing.apply(rows, result)
    source = voice_timing.apply([{**rows[0], 'text': 'Different original text'}], result)
    bilingual = voice_timing.apply([{**rows[0], 'text': 'Original\n译文'}], result)
    for track in (translated, source, bilingual):
        assert (track[0]['start'], track[0]['end']) == (1.5, 2.7)
        assert track[0]['voice_timing'] == [2., 3.]
    assert source[0]['text'] == 'Different original text'
    assert rows[0]['start'] == 2.


def test_apply_never_uses_stale_source_clock(env, monkeypatch):
    project, root = env
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, [cue(seconds=1.)], cfg())
    edited = [cue(start=2.1, end=3.1)]
    assert voice_timing.apply(edited, result) == edited


def test_existing_manual_voice_identity_anchor_survives_relocation(env, monkeypatch):
    project, root = env
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    row = cue(start=2.2, end=3.2, seconds=1., voice_timing=[2., 3.])
    result = voice_timing.plan(project, root, [row], cfg())
    assert result['inputs'][0]['voice_timing'] == [2., 3.]
    assert identity(result['inputs'][0]) == identity(row)
    assert voice_timing.apply([row], result)[0]['voice_timing'] == [2., 3.]


def test_empty_plan_is_ready_and_no_input_mutation(env, monkeypatch):
    project, root = env
    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocation)
    result = voice_timing.plan(project, root, [], cfg())
    assert result['inputs'] == []
    assert result['timing']['ready'] is True
    assert result['timing']['lines'] == []


def test_video_boundary_probe_cached_by_actual_file_version(env, monkeypatch):
    project, root = env
    video = root / 'media' / 'video.mp4'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'first version')
    from dubforge.pipeline import step_assemble
    calls = []
    monkeypatch.setattr(step_assemble, '_ffprobe_duration', lambda path: calls.append(path) or 12.5)
    assert voice_timing.video_seconds(project, root) == 12.5
    assert voice_timing.video_seconds(project, root) == 12.5
    assert len(calls) == 1
    video.write_bytes(b'changed and longer second version')
    assert voice_timing.video_seconds(project, root) == 12.5
    assert len(calls) == 2


def test_bad_video_probe_failure_is_cached(env, monkeypatch):
    project, root = env
    video = root / 'media' / 'video.mp4'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'bad video')
    from dubforge.pipeline import step_assemble
    calls = []
    monkeypatch.setattr(step_assemble, '_ffprobe_duration', lambda path: calls.append(path) or 0)
    assert voice_timing.video_seconds(project, root) is None
    assert voice_timing.video_seconds(project, root) is None
    assert len(calls) == 1


def test_plan_uses_same_real_video_boundary_as_other_consumers(env, monkeypatch):
    project, root = env
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 8.2)
    boundaries = []

    def allocate(rows, **kwargs):
        boundaries.append(kwargs['video_duration'])
        return allocation(rows)

    monkeypatch.setattr(voice_timing, 'allocate_voice_windows', allocate)
    result = voice_timing.plan(project, root, [cue(seconds=1.)], cfg())
    assert boundaries == [8.2]
    assert result['placement']['video_duration'] == 8.2


def test_fingerprint_has_selected_valid_versions_but_no_media_probe(env, monkeypatch):
    project, root = env
    row = cue()
    path = wav(root, row, 1.)
    dubbing_edit.record_take(project, row, root, 1, 'hash', {})
    deps = SimpleNamespace(project=project, root=root, cfg=cfg(), production_lines=lambda: [row])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: pytest.fail('指纹启动了媒体探测'))
    first = voice_timing.fingerprint(deps)
    assert first['strategy'] == voice_timing.STRATEGY_VERSION
    assert first['voices'][0]['version'] == dubbing_edit.file_version(path)
    path.write_bytes(b'changed audio')
    second = voice_timing.fingerprint(deps)
    assert second['voices'][0]['version'] is None
    assert first != second


def test_fingerprint_disabled_does_not_read_selected_voices(env):
    project, root = env
    deps = SimpleNamespace(project=project, root=root,
                           cfg=cfg({'tts.placement.enabled': False}),
                           production_lines=lambda: pytest.fail('关闭排布时读取了音频'))
    assert set(voice_timing.fingerprint(deps)) == {'strategy', 'settings'}


def test_real_allocator_moves_three_sentence_group_and_all_subtitle_tracks(env):
    project, root = env
    rows = [cue(1, 2, 3, seconds=1.8), cue(2, 3, 4, seconds=1.8),
            cue(3, 4, 5, seconds=1.8)]
    result = voice_timing.plan(project, root, rows, cfg(), 9)
    assert result['timing']['ready'] is True
    assert result['timing']['shifted'] >= 2
    assert not [x for x in result['placement']['issues'] if x['kind'] == 'overlap']
    for raw, adjusted in zip(rows, result['inputs'], strict=True):
        assert identity(raw) == identity(adjusted)
        assert adjusted['end'] - adjusted['start'] == pytest.approx(1.8)
    source_track = voice_timing.apply([{**row, 'text': row['source_text']} for row in rows], result)
    target_track = voice_timing.apply(rows, result)
    assert [(r['start'], r['end']) for r in source_track] == [
        (r['start'], r['end']) for r in target_track]


def test_real_allocator_failed_window_preserves_full_audio_and_other_window(env):
    project, root = env
    rows = [cue(1, 0, 1, seconds=5.),
            {**cue(2, 4, 5), 'source': 'original', 'take': None},
            cue(3, 5, 6, seconds=1.5), cue(4, 6, 7, seconds=1.5)]
    result = voice_timing.plan(project, root, rows, cfg(), 10)
    assert result['timing']['unresolved_ids'] == [1]
    assert result['inputs'][0]['seconds'] == 5.
    assert result['placement']['lines'][0]['audio_end'] == 5.
    assert result['inputs'][1]['start'] == 4
    assert result['inputs'][3]['start'] > 6
    assert result['timing']['lines'][3]['applied']
    assert identity(rows[0]) == identity(result['inputs'][0])


def test_disabled_plan_preserves_integer_source_identity(env):
    project, root = env
    row = cue(1, 2, 3, seconds=2.)
    result = voice_timing.plan(project, root, [row],
                               cfg({'tts.placement.enabled': False}), 10)
    assert identity(result['inputs'][0]) == identity(row)
