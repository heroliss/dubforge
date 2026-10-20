"""人工播放层与源稿、自动层和完整声音之间的独立契约。"""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, playback_timing, voice_timing
from dubforge.pipeline.line_settings import identity


def cue(id=1, start=2., end=3., seconds=1., **values):
    return dict(id=id, start=start, end=end, seconds=seconds, text=f'译文{id}',
                source_text=f'Source {id}', source='dub', take=1, **values)


def cfg(values=None):
    return lambda key, default=None: (values or {}).get(key, default)


def edit_for(line, start, end=None):
    return {'identity': identity(line), 'start': start, **({'end': end} if end is not None else {})}


def project_with(rows, edits):
    return SimpleNamespace(uid='test', source_kind='link', source_url='',
                           params=json.dumps({'playback_timing_edits': {
                               str(id): edit_for(next(row for row in rows if row['id'] == id), *bounds)
                               for id, bounds in edits.items()}}))


def test_manual_layer_moves_audio_and_all_text_without_changing_identity(isolated_env):
    rows = [cue()]
    project = project_with(rows, {1: (4.,)})
    before = deepcopy(rows)
    result = voice_timing.plan(project, isolated_env, rows, cfg(), 10)
    row = result['timing']['lines'][0]
    assert row['source_start'] == 2.
    assert (row['automatic_start'], row['automatic_end']) == (2., 3.)
    assert row['manual_start'] == 4. and row['manual_end'] is None
    assert row['effective_mode'] == 'manual' and row['manual_active']
    assert (row['applied_start'], row['applied_end'], row['audio_end']) == (4., 5., 5.)
    assert identity(result['inputs'][0]) == identity(rows[0])
    for text in ('原文', '译文', '译文\nOriginal'):
        track = voice_timing.apply([{**rows[0], 'text': text}], result)
        assert (track[0]['start'], track[0]['end'], track[0]['text']) == (4., 5., text)
    assert rows == before


def test_manual_anchor_is_fixed_and_neighbor_automatically_moves_away(isolated_env):
    rows = [cue(1, 2, 3, 1.5), cue(2, 3, 4, 1.5), cue(3, 4, 5, 1.5)]
    project = project_with(rows, {2: (3.,)})
    result = voice_timing.plan(project, isolated_env, rows, cfg(), 10)
    final = result['timing']['lines']
    assert result['timing']['ready']
    assert final[1]['applied_start'] == 3.
    assert final[0]['applied_end'] + .08 <= 3. + 1e-7
    assert final[2]['applied_start'] >= 4.5 + .08 - 1e-7
    assert not [issue for issue in result['placement']['issues'] if issue['kind'] == 'overlap']


def test_manual_end_reserves_subtitle_window_but_never_stretches_audio(isolated_env):
    rows = [cue(), cue(2, 5, 6)]
    project = project_with(rows, {1: (2., 4.5)})
    result = voice_timing.plan(project, isolated_env, rows, cfg(), 10)
    item = result['timing']['lines'][0]
    assert item['applied_end'] == 4.5 and item['audio_end'] == 3.
    assert result['inputs'][0]['seconds'] == 1.
    assert result['placement']['lines'][0]['audio_end'] == 3.
    assert result['inputs'][1]['start'] >= 4.58


def test_chosen_longer_voice_keeps_manual_start_and_reports_short_window(isolated_env):
    original = cue()
    project = project_with([original], {1: (2., 3.)})
    result = voice_timing.plan(project, isolated_env, [{**original, 'seconds': 2.}], cfg(), 10)
    item = result['timing']['lines'][0]
    assert item['manual_active'] and not item['manual_stale']
    assert item['needs_scale'] and not result['timing']['ready']
    assert item['applied_start'] == 2.
    assert item['applied_end'] == 4. and item['audio_end'] == 4.
    assert item['allocated_end'] == 3.
    assert result['inputs'][0]['seconds'] == 2.


def test_text_change_disables_old_manual_record_instead_of_moving_new_sentence(isolated_env):
    rows = [cue()]
    project = project_with(rows, {1: (4.,)})
    result = voice_timing.plan(project, isolated_env, [{**rows[0], 'text': '新的译文'}], cfg(), 10)
    item = result['timing']['lines'][0]
    assert item['manual_stale'] and not item['manual_active']
    assert item['manual_start'] is None
    assert item['applied_start'] == 2.
    assert result['timing']['manual_stale'] == 1


def test_disabled_auto_keeps_explicit_manual_layer_and_shared_subtitles(isolated_env):
    rows = [cue()]
    project = project_with(rows, {1: (4.,)})
    result = voice_timing.plan(project, isolated_env, rows, cfg({'tts.placement.enabled': False}), 10)
    assert not result['timing']['enabled']
    assert result['inputs'][0]['start'] == 4.
    assert voice_timing.apply(rows, result)[0]['start'] == 4.
    assert result['timing']['lines'][0]['effective_mode'] == 'manual'


def test_manual_conflict_after_voice_growth_is_reported_without_clipping(isolated_env):
    rows = [cue(1, 1, 2), cue(2, 4, 5)]
    project = project_with(rows, {1: (1.,), 2: (4.,)})
    result = voice_timing.plan(project, isolated_env, [{**rows[0], 'seconds': 4.}, rows[1]], cfg(), 10)
    assert [r['start'] for r in result['inputs']] == [1., 4.]
    assert result['inputs'][0]['seconds'] == 4.
    assert not result['timing']['ready']
    assert result['timing']['unresolved_ids'] == [1]
    assert result['timing']['lines'][0]['allocated_end'] == pytest.approx(3.92)
    assert any(issue['kind'] == 'overlap' for issue in result['placement']['issues'])
    assert any(item['kind'] == 'manual_timing_conflict' for item in result['timing']['diagnostics'])
    assert result['timing']['missing'] == 0


def test_inactive_original_choice_keeps_record_for_switching_back(isolated_env):
    row = cue()
    project = project_with([row], {1: (4.,)})
    result = voice_timing.plan(project, isolated_env, [{**row, 'source': 'original'}], cfg(), 10)
    item = result['timing']['lines'][0]
    assert not item['manual_active'] and not item['manual_stale']
    assert item['manual_start'] == 4. and item['applied_start'] == 2.
    assert playback_timing.records(project)


def test_manual_before_earlier_source_is_not_silently_accepted(isolated_env):
    rows = [{**cue(1, 2, 3), 'source': 'original'}, cue(2, 4, 5)]
    project = project_with(rows, {2: (0.,)})
    result = voice_timing.plan(project, isolated_env, rows, cfg(), 10)
    assert result['inputs'][1]['start'] == 0.
    assert not result['timing']['ready']
    assert result['timing']['unresolved_ids'] == [2]
    assert result['timing']['lines'][1]['manual_conflict']
    assert not result['timing']['lines'][1]['needs_scale']


def test_manual_overlap_with_original_needs_movement_instead_of_speeding_later_line(isolated_env):
    rows = [{**cue(1, 1, 3, 2.), 'source': 'original'}, cue(2, 4, 5)]
    project = project_with(rows, {2: (2.,)})
    result = voice_timing.plan(project, isolated_env, rows, cfg(), 10)
    assert result['timing']['unresolved_ids'] == [2]
    assert not result['timing']['lines'][1]['needs_scale']
    assert result['timing']['lines'][1]['manual_conflict']


def test_manual_anchors_keep_required_gap_after_audio_growth(isolated_env):
    rows = [cue(1, 1, 2, 2.97), cue(2, 4, 5)]
    project = project_with(rows, {1: (1.,), 2: (4.,)})
    result = voice_timing.plan(project, isolated_env, rows, cfg(), 10)
    assert not [item for item in result['placement']['issues'] if item['kind'] == 'overlap']
    assert result['timing']['unresolved_ids'] == [1]
    assert result['timing']['lines'][0]['allocated_end'] == pytest.approx(3.92)


@pytest.fixture
def saved_project(make_project):
    return crud.get_project(make_project(params={'another': {'preserve': True}}))


def body(project, line, **values):
    return {'revision': playback_timing.revision(project, line), **values}


def test_save_and_reset_preserve_source_settings_and_sound_records(saved_project):
    line = cue()
    record = playback_timing.save(saved_project, line, body(saved_project, line, start=4.), seconds=1., video_duration=10)
    assert record == edit_for(line, 4.)
    params = crud.project_params(crud.get_project(saved_project.uid))
    assert params['another'] == {'preserve': True}
    assert params['playback_timing_edits']['1'] == record
    assert 'subtitle_edits' not in params and 'tts_lines' not in params and 'dubbing_edit' not in params
    playback_timing.save(saved_project, line, body(saved_project, line, reset=True))
    assert 'playback_timing_edits' not in crud.project_params(crud.get_project(saved_project.uid))


@pytest.mark.parametrize('values', [
    {'start': -1}, {'start': True}, {'start': float('inf')}, {'start': 2, 'end': 2.5},
    {'start': 2, 'end': '3'}, {'start': 2, 'extra': 1}, {},
])
def test_invalid_manual_ranges_do_not_write_anything(saved_project, values):
    line = cue()
    before = saved_project.params
    with pytest.raises(DubForgeError) as exc:
        playback_timing.save(saved_project, line, body(saved_project, line, **values), seconds=1., video_duration=10)
    assert exc.value.code == 'PLAYBACK_TIMING_INVALID'
    assert saved_project.params == before


@pytest.mark.parametrize('source,seconds', [('original', 1.), ('silent', 0.), ('dub', None), ('dub', 0.)])
def test_only_playable_dub_allows_manual_save(saved_project, source, seconds):
    line = {**cue(), 'source': source}
    with pytest.raises(DubForgeError) as exc:
        playback_timing.save(saved_project, line, body(saved_project, line, start=4.), seconds=seconds)
    assert exc.value.code == 'PLAYBACK_TIMING_UNAVAILABLE'


def test_manual_range_must_not_leave_video(saved_project):
    line = cue()
    with pytest.raises(DubForgeError) as exc:
        playback_timing.save(saved_project, line, body(saved_project, line, start=9.5), seconds=1., video_duration=10)
    assert exc.value.code == 'PLAYBACK_TIMING_OUTSIDE_VIDEO'


def test_millisecond_end_rounding_preserves_complete_audio_without_unnecessary_retry(saved_project, isolated_env):
    line = cue(seconds=1.00003125)
    playback_timing.save(saved_project, line, body(saved_project, line, start=2., end=3.), seconds=line['seconds'])
    result = voice_timing.plan(saved_project, isolated_env, [line], cfg(), 10)
    item = result['timing']['lines'][0]
    assert item['applied_end'] == pytest.approx(3.00003125)
    assert not item['needs_scale']


def test_save_can_leave_auto_neighbor_conflict_for_visible_review(saved_project):
    rows = [cue(1, 1, 2), cue(2, 2, 3)]
    assert playback_timing.save(saved_project, rows[0], body(saved_project, rows[0], start=2.),
                                seconds=1., lines=rows) == edit_for(rows[0], 2.)


@pytest.mark.parametrize('start', [0., 2., 3.5])
def test_save_rejects_overlap_or_reversal_with_another_manual_sentence(saved_project, start):
    rows = [cue(1, 1, 2), cue(2, 4, 5)]
    playback_timing.save(saved_project, rows[0], body(saved_project, rows[0], start=3.), seconds=1.)
    with pytest.raises(DubForgeError) as exc:
        playback_timing.save(saved_project, rows[1], body(saved_project, rows[1], start=start),
                             seconds=1., lines=rows)
    assert exc.value.code == 'PLAYBACK_TIMING_OVERLAP'


def test_revision_blocks_lost_updates_and_changes_to_source_timing_or_selected_voice(saved_project):
    line = cue()
    old = body(saved_project, line, start=4.)
    playback_timing.save(saved_project, line, old, seconds=1.)
    with pytest.raises(DubForgeError) as exc:
        playback_timing.save(saved_project, line, old, seconds=1.)
    assert exc.value.code == 'PLAYBACK_TIMING_CONFLICT'
    first = playback_timing.revision(saved_project, line, audio_version='first')
    assert playback_timing.revision(saved_project, line, audio_version='second') != first
    assert playback_timing.revision(saved_project, {**line, 'take': 2}, audio_version='first') != first
    assert playback_timing.revision(saved_project, {**line, 'start': 2.2, 'end': 3.2,
                                                   'voice_timing': [2., 3.]}, audio_version='first') != first


def test_reset_stale_record_does_not_require_valid_sound(saved_project):
    line = cue()
    playback_timing.save(saved_project, line, body(saved_project, line, start=4.), seconds=1.)
    changed = {**line, 'text': '变更后还没有音频'}
    assert playback_timing.resolve(saved_project, changed)['stale']
    playback_timing.save(saved_project, changed, body(saved_project, changed, reset=True))
    assert playback_timing.records(saved_project) == {}


def test_playback_fingerprint_includes_manual_even_with_auto_disabled(isolated_env, monkeypatch):
    row = cue()
    project = project_with([row], {1: (4.,)})
    deps = SimpleNamespace(project=project, root=isolated_env,
                           cfg=cfg({'tts.placement.enabled': False}), production_lines=lambda: [row])
    monkeypatch.setattr(dubbing_edit, 'playable', lambda *_a, **_k: True)
    first = voice_timing.fingerprint(deps)
    assert first['manual'] == playback_timing.records(project)
    assert first['voices'][0]['id'] == 1
    project.params = json.dumps({'playback_timing_edits': {'1': edit_for(row, 5.)}})
    assert voice_timing.fingerprint(deps) != first


def test_bad_manual_record_is_visible_stale_and_never_applied(isolated_env):
    row = cue()
    project = SimpleNamespace(uid='test', source_kind='link', source_url='', params=json.dumps({
        'playback_timing_edits': {'1': {'identity': identity(row), 'start': '坏值'}}}))
    result = voice_timing.plan(project, isolated_env, [row], cfg(), 10)
    item = result['timing']['lines'][0]
    assert item['manual_stale'] and not item['manual_active']
    assert item['applied_start'] == 2.
