"""人工播放层的真实接口、音频保护、字幕与成片消费回归；全部使用隔离假配音。"""
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from conftest import synthesize
from test_configuration import save
from test_dubbing_preparation import index_prepared, prepared
from dubforge import chain_recovery
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions, dubbing_edit, dubbing_session
from dubforge.pipeline import line_settings, step_assemble, step_subtitles, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.recovery import RecoveryDependencies
from dubforge.runner import compute_fingerprint


def state(client, uid):
    response = client.get(f'/api/projects/{uid}/workbench?probe=false')
    assert response.status_code == 200, response.text
    return response.json()


def line_state(client, uid, line_id=1):
    return next(row for row in state(client, uid)['lines'] if row['id'] == line_id)


def adjust(client, uid, *, line_id=1, **body):
    body.setdefault('revision', line_state(client, uid, line_id)['timing']['timing_revision'])
    return client.put(f'/api/projects/{uid}/workbench/lines/{line_id}/timing', json=body)


def context(uid, version=None):
    deps = Dependencies(crud.get_project(uid))
    return SimpleNamespace(project=deps.project, pdir=deps.root, root=deps.root,
        cfg=deps.cfg, upstream=deps.upstream, version=version,
        check_cancel=lambda: None, log=lambda _: None, register_file=lambda *args: None)


@pytest.fixture
def voiced(index_prepared, monkeypatch):
    """真实生成入口与版本登记；模型只写固定非静音短音频，绝不访问服务。"""
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    calls = []

    def generate(url, text, output, language, **kwargs):
        calls.append((text, kwargs['duration_factor']))
        sf.write(output, np.full(25600, .15, np.float32), 32000,
                 subtype='PCM_16', format='WAV')
        return output

    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 12.)
    save('project:' + uid, {'assemble.loudnorm': False})
    for ident in (1, 2):
        base = f'/api/projects/{uid}/workbench/lines/{ident}'
        response = synthesize(client, base + '/synthesize', json={})
        assert response.status_code == 200, response.text
        assert client.post(base + '/confirm', json={'confirmed': True}).status_code == 200
    return client, uid, calls


def test_workbench_exposes_source_automatic_manual_and_effective_clock_without_writing(voiced):
    client, uid, _ = voiced
    original = crud.get_project(uid).params
    line = line_state(client, uid)
    timing = line['timing']
    assert timing['source_start'] == line['start'] == 0
    assert timing['source_end'] == line['end'] == 2
    assert timing['automatic_start'] == line['playback_start']
    assert timing['automatic_end'] == line['playback_end']
    assert timing['manual_start'] is None and timing['manual_end'] is None
    assert not timing['manual_active'] and not timing['manual_stale']
    assert timing['effective_mode'] == 'automatic'
    assert timing['timing_revision']
    assert crud.get_project(uid).params == original


def test_manual_window_preserves_selected_audio_confirmation_fine_tuning_and_model_calls(voiced):
    client, uid, calls = voiced
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {
        'tts.indextts25.temperature': .55}}).status_code == 200
    project, root = crud.get_project(uid), crud.project_dir(uid)
    before_params = crud.project_params(project)
    before = line_state(client, uid)
    audio = dubbing_edit.take_path(root, 1, 1)
    original = audio.read_bytes(), audio.with_suffix('.take.json').read_bytes()
    model_calls = list(calls)
    identity = line_settings.identity(Dependencies(project).dubbing_lines()[0])
    response = adjust(client, uid, start=.6, end=1.8)
    assert response.status_code == 200, response.text
    after = line_state(client, uid)
    assert after['start'] == 0 and after['end'] == 2
    assert after['playback_start'] == .6 and after['playback_end'] == 1.8
    assert after['timing']['manual_active'] and after['timing']['effective_mode'] == 'manual'
    assert after['timing']['automatic_start'] == before['timing']['automatic_start']
    assert after['selection'] == before['selection'] and after['confirmed']
    assert after['customized'] and after['wav']['exists']
    assert after['versions'] == before['versions']
    assert calls == model_calls
    assert (audio.read_bytes(), audio.with_suffix('.take.json').read_bytes()) == original
    project = crud.get_project(uid)
    assert line_settings.identity(Dependencies(project).dubbing_lines()[0]) == identity
    assert crud.project_params(project).get('tts_lines') == before_params.get('tts_lines')
    assert crud.project_params(project).get('dubbing_edit') == before_params.get('dubbing_edit')


def test_manual_start_without_end_uses_complete_selected_audio_and_is_effective_when_auto_disabled(voiced):
    client, uid, calls = voiced
    save('project:' + uid, {'tts.placement.enabled': False})
    response = adjust(client, uid, start=.5)
    assert response.status_code == 200, response.text
    line = line_state(client, uid)
    assert line['timing']['manual_end'] is None
    assert line['timing']['effective_mode'] == 'manual'
    assert line['playback_start'] == .5
    assert line['playback_end'] == pytest.approx(.5 + line['wav']['seconds'])
    assert not state(client, uid)['timing']['enabled']
    assert len(calls) == 2


def test_reset_removes_only_manual_layer_and_restores_current_automatic_result(voiced):
    client, uid, calls = voiced
    before = line_state(client, uid)
    assert adjust(client, uid, start=.6, end=1.8).status_code == 200
    response = adjust(client, uid, reset=True)
    assert response.status_code == 200, response.text
    after = line_state(client, uid)
    assert not after['timing']['manual_active'] and after['timing']['manual_start'] is None
    assert after['playback_start'] == before['playback_start']
    assert after['playback_end'] == before['playback_end']
    assert after['selection'] == before['selection'] and after['confirmed']
    assert len(calls) == 2


def test_three_subtitle_tracks_and_actual_mix_share_manual_clock_while_source_version_keeps_source(voiced):
    client, uid, calls = voiced
    assert adjust(client, uid, start=.6, end=1.8).status_code == 200
    current = state(client, uid)
    expected = [(row['playback_start'], row['playback_end']) for row in current['lines']]
    tracks, source = step_subtitles.build_tracks(context(uid), 'all')
    for lines in tracks.values():
        assert [(line.start, line.end) for line in lines] == expected
    assert [(row['start'], row['end']) for row in source] == [(0, 2), (3, 5)]
    source_tracks, _ = step_subtitles.build_tracks(context(uid, {'dubbing': False}), 'all')
    for lines in source_tracks.values():
        assert [(line.start, line.end) for line in lines] == [(0, 2), (3, 5)]
    audio, sr = sf.read(step_assemble._mix_timeline(context(uid), crud.project_dir(uid), 12))
    assert len(audio) == sr * 12
    assert abs(audio[round(.3 * sr)]) < .001
    assert abs(audio[round(.9 * sr)]) > .01
    assert abs(audio[round(1.7 * sr)]) < .001  # 预留窗口不会拉长或循环声音。
    assert len(calls) == 2


def test_manual_timing_changes_only_downstream_fingerprints_and_preserves_audio_identity(voiced):
    client, uid, _ = voiced
    project = crud.get_project(uid)
    steps = ('translate', 'dubtask', 'tts', 'subtitles', 'assemble')
    before = {key: compute_fingerprint(project, key) for key in steps}
    assert adjust(client, uid, start=.6, end=1.8).status_code == 200
    project = crud.get_project(uid)
    after = {key: compute_fingerprint(project, key) for key in steps}
    assert all(after[key] == before[key] for key in ('translate', 'dubtask', 'tts'))
    assert all(after[key] != before[key] for key in ('subtitles', 'assemble'))
    assert Dependencies(project).fresh('tts')


def versions(uid):
    deps = RecoveryDependencies(crud.get_project(uid))
    specs = [dict(id='dub', name='配音版', dubbing=True, burn_subtitles=True,
                  include_comments=False, overrides={}),
             dict(id='source', name='原声版', dubbing=False, burn_subtitles=True,
                  include_comments=False, overrides={})]
    return deps, [assembly_versions.resolve(deps, spec) for spec in specs]


def test_manual_timing_refreshes_dubbed_version_fingerprint_without_invalidating_original_version(voiced):
    client, uid, _ = voiced
    deps, resolved = versions(uid)
    before = {version['id']: assembly_versions.fingerprint(deps, version) for version in resolved}
    assert adjust(client, uid, start=.6, end=1.8).status_code == 200
    deps, resolved = versions(uid)
    after = {version['id']: assembly_versions.fingerprint(deps, version) for version in resolved}
    assert after['dub'] != before['dub']
    assert after['source'] == before['source']


def test_manual_revision_rejects_old_page_and_preserves_saved_adjustment(voiced):
    client, uid, _ = voiced
    revision = line_state(client, uid)['timing']['timing_revision']
    assert adjust(client, uid, start=.6).status_code == 200
    before = crud.get_project(uid).params
    response = adjust(client, uid, revision=revision, start=.7)
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'PLAYBACK_TIMING_CONFLICT'
    assert crud.get_project(uid).params == before
    assert line_state(client, uid)['playback_start'] == .6


@pytest.mark.parametrize('body', [
    {'start': -1}, {'start': True}, {'start': '1'}, {'start': None},
    {'start': 1, 'end': 1}, {'start': 1, 'end': 1.2},
    {'start': 12}, {'start': 11.5}, {'start': 1, 'end': 13},
    {'start': 1, 'unknown': 1},
])
def test_invalid_manual_adjustment_has_no_side_effects(voiced, body):
    client, uid, calls = voiced
    original = crud.get_project(uid).params
    original_audio = dubbing_edit.take_path(crud.project_dir(uid), 1, 1).read_bytes()
    response = adjust(client, uid, **body)
    assert response.status_code == 400, response.text
    assert crud.get_project(uid).params == original
    assert dubbing_edit.take_path(crud.project_dir(uid), 1, 1).read_bytes() == original_audio
    assert len(calls) == 2


def test_original_and_silent_sources_do_not_accept_new_playback_adjustments_but_can_reset(voiced):
    client, uid, _ = voiced
    base = f'/api/projects/{uid}/workbench'
    assert adjust(client, uid, start=.6).status_code == 200
    for source, take in (('original', 1), ('dub', None)):
        assert client.put(base + '/lines/1/selection', json={'source': source, 'take': take}).status_code == 200
        response = adjust(client, uid, start=.7)
        assert response.status_code == 400, response.text
    assert adjust(client, uid, reset=True).status_code == 200
    assert not line_state(client, uid)['timing']['manual_active']


def test_running_synthesis_protects_manual_timing_write(voiced):
    client, uid, _ = voiced
    project = crud.get_project(uid)
    original = project.params
    with dubbing_session.use(project, 2):
        response = adjust(client, uid, start=.6)
        assert response.status_code == 400, response.text
        assert response.json()['code'] == 'CONFIGURATION_BUSY'
    assert crud.get_project(uid).params == original


def test_source_identity_change_marks_saved_manual_layer_stale_and_allows_explicit_reset(voiced):
    client, uid, _ = voiced
    assert adjust(client, uid, start=.6, end=1.8).status_code == 200
    line = line_state(client, uid)
    response = client.put(f'/api/projects/{uid}/workbench/lines/1/text', json={
        'revision': line['edit_revision'], 'values': {'text': '经过人工修订的新译文'}})
    assert response.status_code == 200, response.text
    line = line_state(client, uid)
    assert line['timing']['manual_stale'] and not line['timing']['manual_active']
    assert line['timing']['effective_mode'] != 'manual'
    assert adjust(client, uid, reset=True).status_code == 200
    assert not line_state(client, uid)['timing']['manual_stale']


def test_selecting_longer_audio_preserves_manual_start_but_reports_insufficient_window(voiced):
    client, uid, _ = voiced
    base, root = f'/api/projects/{uid}/workbench', crud.project_dir(uid)
    assert adjust(client, uid, start=.6, end=1.8).status_code == 200
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    output = dubbing_edit.take_path(root, 1, 2)
    sf.write(output, np.full(64000, .15, np.float32), 32000,
             subtype='PCM_16', format='WAV')
    crud.add_artifact(project.id, 'tts', 'audio', output.name, output, str(output.relative_to(root)))
    dubbing_edit.record_take(project, line, root, 2, 'isolated-timing-integration', {})
    response = client.put(base + '/lines/1/selection', json={'source': 'dub', 'take': 2})
    assert response.status_code == 200, response.text
    current = state(client, uid)
    line = current['lines'][0]
    assert line['timing']['manual_active'] and line['timing']['manual_end'] == 1.8
    assert line['playback_start'] == .6 and line['playback_end'] >= 2.6
    assert 1 in current['timing']['unresolved_ids'] and not current['timing']['ready']
    assert line['timing']['needs_scale']
    assert line['versions'][0]['confirmed']
    assert output.exists() and dubbing_edit.take_path(root, 1, 1).exists()


def test_hand_adjusted_sentence_is_a_fixed_obstacle_for_automatic_neighbor_layout(voiced):
    client, uid, _ = voiced
    response = adjust(client, uid, start=2.4, end=3.6)
    assert response.status_code == 200, response.text
    current = state(client, uid)
    first, second = current['lines']
    assert first['playback_start'] == 2.4
    assert second['playback_start'] >= first['playback_end'] + .08 - 1e-6
    assert current['timing']['ready']


def test_overlapping_manual_windows_reject_second_edit_without_corrupting_first(voiced):
    client, uid, _ = voiced
    assert adjust(client, uid, start=2, end=3).status_code == 200
    original = crud.get_project(uid).params
    response = adjust(client, uid, line_id=2, start=2.5, end=3.8)
    assert response.status_code == 400, response.text
    assert crud.get_project(uid).params == original
    assert line_state(client, uid)['timing']['manual_start'] == 2


@pytest.mark.parametrize('dubbing,step,should_block', [
    (True, 'assemble', True), (False, 'assemble', False),
    (True, 'subtitles', True), (True, 'tts', False),
])
def test_chain_manual_input_protection_matches_actual_timing_consumers(voiced, dubbing, step, should_block):
    client, uid, _ = voiced
    spec = dict(id='only', name='隔离链版本', dubbing=dubbing,
                burn_subtitles=True, include_comments=False, overrides={})
    save('project:' + uid, {'assembly.versions': [spec], 'content_review.enabled': False})
    deps = RecoveryDependencies(crud.get_project(uid))
    definitions = assembly_versions.catalog(uid)['versions'] if step == 'assemble' else []
    resolved = [assembly_versions.resolve(deps, row) for row in definitions]
    if resolved:
        deps = deps.with_versions(resolved)
    snapshot = chain_recovery._execution_snapshot(deps, [step], definitions, resolved)
    assert adjust(client, uid, start=.6).status_code == 200
    if should_block:
        with pytest.raises(DubForgeError) as caught:
            chain_recovery.check_execution(uid, snapshot, [step])
        assert caught.value.code == 'CHAIN_PLAN_CHANGED'
    else:
        chain_recovery.check_execution(uid, snapshot, [step])
