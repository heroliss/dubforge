"""自动缩短历史可在原槽恢复；不调用真实模型或覆盖其他配音槽。"""
import json

import pytest

from test_dubbing_preparation import index_prepared, prepared
from test_voice_timing_integration import timeline, fake_index, start_selected
from dubforge.domain import crud
from dubforge.pipeline import audio_adjustments, dubbing_edit, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.tts_duration import audio_seconds
from conftest import synthesize


def generated(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    calls = fake_index(monkeypatch, lambda text, factor: .5 if text == '世界' else
                       2 if factor == 1.1 else 1.3)
    assert start_selected(client, uid).status == 'done'
    base = f'/api/projects/{uid}/workbench'
    state = client.get(base + '?probe=false').json()
    return client, uid, base, state, calls


def test_shortening_history_restores_same_slot_both_ways_without_tts(index_prepared, monkeypatch):
    client, uid, base, state, calls = generated(index_prepared, monkeypatch)
    row = state['lines'][0]
    history = row['wav']['adjustment_history']
    assert [entry['factor'] for entry in history['entries']] == [1.1, .7]
    assert history['current_id'] == history['entries'][1]['id']
    root = crud.project_dir(uid)
    project = crud.get_project(uid)
    params = crud.project_params(project)
    assert client.post(base + '/lines/1/confirm', json={'confirmed': True}).status_code == 200
    before = client.get(base + '?probe=false').json()['lines'][0]
    assert before['confirmed']
    restore = lambda h, entry: client.put(base + '/lines/1/audio-adjustment',
        json={'revision': h['revision'], 'entry_id': entry})
    assert restore(history, history['entries'][0]['id']).status_code == 200
    assert audio_seconds(dubbing_edit.take_path(root, 1, 1)) == 2
    restored = client.get(base + '?probe=false').json()
    assert restored['lines'][0]['selection']['take'] == 1
    assert not restored['lines'][0]['confirmed']
    assert restored['timing']['unresolved_ids'] == [1]
    assert crud.project_params(crud.get_project(uid)) == params
    new_history = restored['lines'][0]['wav']['adjustment_history']
    assert restore(new_history, history['entries'][1]['id']).status_code == 200
    assert audio_seconds(dubbing_edit.take_path(root, 1, 1)) == pytest.approx(1.3)
    assert client.get(base + '?probe=false').json()['timing']['ready']
    assert len(calls) == 3
    assert Dependencies(crud.get_project(uid)).cfg('tts.indextts25.duration_factor') == 1.1


def test_restore_rejects_stale_revision_or_unknown_record_without_changing_audio(index_prepared, monkeypatch):
    client, uid, base, state, calls = generated(index_prepared, monkeypatch)
    history = state['lines'][0]['wav']['adjustment_history']
    output = dubbing_edit.take_path(crud.project_dir(uid), 1, 1)
    before = output.read_bytes()
    for body, code in [({'revision': 'stale', 'entry_id': history['entries'][0]['id']},
                         'AUDIO_ADJUSTMENT_CONFLICT'),
                        ({'revision': history['revision'], 'entry_id': '../invalid'},
                         'AUDIO_ADJUSTMENT_INVALID')]:
        response = client.put(base + '/lines/1/audio-adjustment', json=body)
        assert response.status_code == 400 and response.json()['code'] == code
        assert output.read_bytes() == before
    assert len(calls) == 3


def test_corrupt_history_never_replaces_current_audio(index_prepared, monkeypatch):
    client, uid, base, state, _ = generated(index_prepared, monkeypatch)
    history = state['lines'][0]['wav']['adjustment_history']
    root = crud.project_dir(uid)
    row = Dependencies(crud.get_project(uid)).dubbing_lines()[0]
    data = audio_adjustments._read(root, row, 1)
    entry = data['entries'][0]
    path = audio_adjustments._index_path(root, row, 1).parent / entry['file']
    path.write_bytes(b'corrupted history')
    output = dubbing_edit.take_path(root, 1, 1)
    before = output.read_bytes()
    response = client.put(base + '/lines/1/audio-adjustment', json={
        'revision': history['revision'], 'entry_id': entry['id']})
    assert response.status_code == 400
    assert response.json()['code'] == 'AUDIO_ADJUSTMENT_INVALID'
    assert output.read_bytes() == before


def test_new_generation_replaces_visible_adjustment_history_not_old_slot(index_prepared, monkeypatch):
    client, uid, base, state, _ = generated(index_prepared, monkeypatch)
    history = state['lines'][0]['wav']['adjustment_history']
    assert start_selected(client, uid).status == 'done'
    current = client.get(base + '?probe=false').json()['lines'][0]['wav']['adjustment_history']
    assert {e['id'] for e in history['entries']}.isdisjoint(e['id'] for e in current['entries'])
    assert client.put(base + '/lines/1/audio-adjustment', json={
        'revision': history['revision'], 'entry_id': history['entries'][0]['id']}).status_code == 400


def test_legacy_snapshot_restores_known_actual_factor_without_inventing_request_history(index_prepared, monkeypatch):
    client, uid, base, state, _ = generated(index_prepared, monkeypatch)
    root = crud.project_dir(uid)
    row = Dependencies(crud.get_project(uid)).dubbing_lines()[0]
    data = audio_adjustments._read(root, row, 1)
    data['entries'][0]['record'].pop('duration_retry')
    audio_adjustments._write(root, row, 1, data)
    history = state['lines'][0]['wav']['adjustment_history']
    assert client.put(base + '/lines/1/audio-adjustment', json={
        'revision': history['revision'], 'entry_id': history['entries'][0]['id']}).status_code == 200
    result = dubbing_edit.read_take(root, row, 1)['duration_retry']
    assert result['used_factor'] == 1.1 and result['attempts'] == []


def test_workbench_skips_history_reader_for_slots_without_index(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    for line_id in (1, 2):
        response = synthesize(client, base + f'/lines/{line_id}/synthesize', json={})
        assert response.status_code == 200, response.text

    def unexpected_read(*args):
        pytest.fail('没有历史索引的有效配音槽不应逐槽试读缺失文件')

    monkeypatch.setattr(audio_adjustments, 'view', unexpected_read)
    response = client.get(base + '?probe=false')
    assert response.status_code == 200, response.text
    assert all(row['wav']['exists'] and row['wav']['adjustment_history'] is None
               for row in response.json()['lines'])


def test_workbench_inventory_reads_existing_history_and_detects_new_index_next_request(index_prepared, monkeypatch):
    import numpy as np
    import soundfile as sf
    client, uid, base, _, calls = generated(index_prepared, monkeypatch)
    reads = []
    original_view = audio_adjustments.view

    def observe(root, line, take):
        reads.append((line['id'], take))
        return original_view(root, line, take)

    monkeypatch.setattr(audio_adjustments, 'view', observe)
    before = client.get(base + '?probe=false').json()
    assert reads == [(1, 1)]
    assert before['lines'][0]['wav']['adjustment_history']
    assert before['lines'][1]['wav']['adjustment_history'] is None

    # 在两次请求之间通过正式领域入口新增第二句历史；不能跨请求缓存目录库存。
    root = crud.project_dir(uid)
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[1]
    generation = audio_adjustments.begin(root, line, 1)
    assert generation
    output = dubbing_edit.take_path(root, 2, 1)
    record = dubbing_edit.read_take(root, line, 1)
    sf.write(output, np.full(12800, .1, np.float32), 32000, format='WAV', subtype='PCM_16')
    dubbing_edit.record_take(project, line, root, 1, record['fingerprint'],
                             {'duration_factor': .7})
    crud.add_artifact(project.id, 'tts', 'audio', output.name, output, str(output.relative_to(root)))
    audio_adjustments.finish(root, line, 1, generation)

    reads.clear()
    response = client.get(base + '?probe=false')
    assert response.status_code == 200, response.text
    assert reads == [(1, 1), (2, 1)]
    assert len(response.json()['lines'][1]['wav']['adjustment_history']['entries']) == 2
    assert len(calls) == 3
