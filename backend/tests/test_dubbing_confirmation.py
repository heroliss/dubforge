"""确认信息在入队锁内核验，不再为提交单句重建完整工作台。"""
import json

import pytest

from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, dubbing_task, line_settings, pronunciation
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from dubforge.utils.fs import atomic_write_bytes
from test_dubbing_preparation import index_prepared, prepared
from test_dubbing_tasks import finished


def expected(state, ids=(1,)):
    return {'slots': state['synthesis_slots'],
            'lines': {str(line['id']): line['synthesis_revision']
                      for line in state['lines'] if line['id'] in ids}}


def test_confirmation_submits_without_rebuilding_full_workbench(index_prepared, monkeypatch):
    from app.api import workbench
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    base = f'/api/projects/{uid}/workbench'
    snapshot = client.get(base + '?probe=false').json()
    assert snapshot['synthesis_slots'] == []

    def full_page_forbidden(*args, **kwargs):
        raise AssertionError('单句提交不应再次构造整页配音、音频归一化或排布')

    monkeypatch.setattr(workbench, '_sample_lines', full_page_forbidden)
    from dubforge.pipeline import audio_post, voice_timing
    monkeypatch.setattr(audio_post, 'line_view', full_page_forbidden)
    monkeypatch.setattr(voice_timing, 'plan', full_page_forbidden)
    runner.acquire_slot('tts')
    try:
        response = client.post(base + '/synthesize', json={
            'line_ids': [1], 'takes': {'1': 1}, 'expected': expected(snapshot)})
        assert response.status_code == 200, response.text
        assert response.json()['lines'] == [{'id': 1, 'take': 1, 'status': 'queued'}]
        assert crud.get_step(project.id, 'tts').status == 'queued'
        runner.cancel_step(uid, 'tts', expected_job_id=response.json()['job_id'])
        assert finished(project).status == 'cancelled'
    finally:
        runner.release_slot('tts')


@pytest.mark.parametrize('change', ['text', 'time', 'selection', 'audio', 'confirmed',
                                    'line_params', 'project_params', 'other_slot'])
def test_stale_confirmation_cannot_start_or_overwrite_audio(index_prepared, change):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    deps = Dependencies(project)
    base = f'/api/projects/{uid}/workbench'
    snapshot = client.get(base + '?probe=false').json()
    line = deps.dubbing_lines(required=True)[0]
    original = deps.root / 'artifacts/tts/1.wav'
    if change in {'text', 'time'}:
        path = deps.path('translate' if change == 'text' else 'split',
                         'translate.txt' if change == 'text' else 'split.txt')
        value = path.read_text(encoding='utf-8').replace('你好', '您好') if change == 'text' else path.read_text(encoding='utf-8').replace('0.000-2.000', '0.100-2.000')
        atomic_write_bytes(path, value.encode())
    elif change == 'selection':
        dubbing_edit.save_selection(project, line, 'dub', None)
    elif change == 'audio':
        atomic_write_bytes(original, b'unchanged-until-user-reconfirms')
    elif change == 'confirmed':
        # 已有槽的满意记录也是用户确认覆盖时所见内容的一部分。
        atomic_write_bytes(original, b'audio')
        snapshot = client.get(base + '?probe=false').json()
        atomic_write_bytes(deps.root / 'artifacts/tts/workbench_confirm.json',
                           json.dumps({'1': {'fingerprint': 'new-receipt', 'version': 'new-version'}}).encode())
    elif change == 'line_params':
        line_settings.save(project, line, {pronunciation.PATH: '换成完全不同的朗读句子。'}, [], deps.cfg)
    elif change == 'project_params':
        response = client.put(f'/api/projects/{uid}/step_params', json={
            'updates': {'tts.indextts25.temperature': .456}, 'reset': []})
        assert response.status_code == 200
    elif change == 'other_slot':
        atomic_write_bytes(deps.root / 'artifacts/tts/2_v4.wav', b'other-sentence-version')
    before = original.read_bytes() if original.exists() else None
    response = client.post(base + '/synthesize', json={
        'line_ids': [1], 'takes': {'1': 1}, 'expected': expected(snapshot)})
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'TTS_CONFIRMATION_CHANGED'
    assert crud.latest_job(project.id, 'tts') is None
    assert (original.read_bytes() if original.exists() else None) == before


def test_unselected_sentence_parameters_do_not_invalidate_confirmation(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    deps = Dependencies(project)
    snapshot = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    line_settings.save(project, deps.dubbing_lines(required=True)[1],
                       {pronunciation.PATH: '只改第二句的朗读。'}, [], deps.cfg)
    fresh = crud.get_project(uid)
    assert dubbing_task.prepare(fresh, Dependencies(fresh), [1], {'1': 1}, expected(snapshot))['lines'] == [
        {'id': 1, 'take': 1, 'status': 'queued'}]


@pytest.mark.parametrize('bad', [{}, {'slots': [], 'lines': {}}, {'slots': [True], 'lines': {'1': 'a' * 64}},
                                  {'slots': [], 'lines': {'1': None}}, {'slots': [], 'lines': {'1': 'a' * 64, '2': 'a' * 64}}])
def test_malformed_confirmation_does_not_create_job(index_prepared, bad):
    client, uid, _ = index_prepared
    response = client.post(f'/api/projects/{uid}/workbench/synthesize', json={
        'line_ids': [1], 'expected': bad})
    assert response.status_code == 400
    assert response.json()['code'] == 'TTS_CONFIRMATION_INVALID'
    assert crud.latest_job(crud.get_project(uid).id, 'tts') is None


def test_take_catalog_matches_individual_slot_lookup(prepared):
    uid, project, _ = prepared
    root = crud.project_dir(uid)
    for name in ['1.wav', '1_v3.wav', '2_v2.wav', '2_v10001.wav', 'voice.wav', '2_vbad.wav']:
        atomic_write_bytes(root / 'artifacts/tts' / name, b'audio')
    (root / 'artifacts/tts/3.wav').mkdir()
    index = dubbing_edit.take_index(root)
    assert index == {1: [1, 3], 2: [2]}
    assert index[1] == dubbing_edit.take_ids(root, 1)
    assert index[2] == dubbing_edit.take_ids(root, 2)


def test_audio_snapshot_can_be_reused_only_for_its_file_version(prepared):
    uid, project, _ = prepared
    root = crud.project_dir(uid)
    line = Dependencies(project).dubbing_lines(required=True)[0]
    path = dubbing_edit.take_path(root, 1)
    atomic_write_bytes(path, b'audio')
    version = dubbing_edit.file_version(path)
    data = {'identity': line_settings.identity(line), 'version': version}
    assert dubbing_edit.snapshot_current(root, line, 1, snapshot=data, version=version)
    assert dubbing_edit.playable(project, line, root, 1, snapshot=data, version=version)
    atomic_write_bytes(path, b'replaced-audio')
    current = dubbing_edit.file_version(path)
    assert not dubbing_edit.snapshot_current(root, line, 1, snapshot=data, version=current)
    assert not dubbing_edit.playable(project, line, root, 1, snapshot=data, version=current)
    atomic_write_bytes(path, b'')
    assert not dubbing_edit.playable(project, line, root, 1)


def test_workbench_reuses_directory_index_and_take_snapshot(prepared, monkeypatch):
    from app.api import workbench
    from conftest import synthesize
    from fastapi.testclient import TestClient
    from app.main import app
    uid, project, _ = prepared
    with TestClient(app) as client:
        for ident in (1, 2):
            assert synthesize(client, f'/api/projects/{uid}/workbench/lines/{ident}/synthesize', json={}).status_code == 200
    project = crud.get_project(uid)
    original = dubbing_edit.read_take
    reads = []

    def tracked(root, line, take):
        reads.append((line['id'], take))
        return original(root, line, take)

    def individual_lookup_forbidden(*args):
        raise AssertionError('工作台不能逐句扫描整个配音目录')

    monkeypatch.setattr(dubbing_edit, 'read_take', tracked)
    monkeypatch.setattr(dubbing_edit, 'take_ids', individual_lookup_forbidden)
    rows = workbench._sample_lines(project, crud.project_dir(uid), Dependencies(project))
    assert len(rows) == 2 and all(row['wav']['exists'] for row in rows)
    assert reads == [(1, 1), (2, 1)]


def test_batch_confirmation_reads_receipts_and_catalog_once(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    project = crud.get_project(uid)
    calls = []
    catalog, receipts = dubbing_edit.take_index, dubbing_task._confirmation_records

    def tracked_catalog(root):
        calls.append('catalog')
        return catalog(root)

    def tracked_receipts(root):
        calls.append('receipts')
        return receipts(root)

    monkeypatch.setattr(dubbing_edit, 'take_index', tracked_catalog)
    monkeypatch.setattr(dubbing_task, '_confirmation_records', tracked_receipts)
    result = dubbing_task.prepare(project, Dependencies(project), [1, 2], {'1': 1, '2': 1}, expected(state, (1, 2)))
    assert len(result['lines']) == 2
    assert calls == ['catalog', 'receipts']
