"""共同目标槽的配音生成通过正式任务入口验证，不调用真实配音服务。"""
from fastapi.testclient import TestClient
import pytest

from conftest import synthesize
from test_dubbing_preparation import prepared, index_prepared
from test_dubbing_tasks import finished
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, dubbing_task
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from dubforge.utils.fs import atomic_write_bytes


def snapshot_slot(root, line_id, take):
    """音频、输入指纹与生成快照必须一起保留，不能只验证文件存在。"""
    audio = dubbing_edit.take_path(root, line_id, take)
    return {path: path.read_bytes() for path in (
        audio, audio.with_suffix('.input'), audio.with_suffix('.take.json'))}


def assert_preserved(snapshot):
    assert all(path.read_bytes() == content for path, content in snapshot.items())


def test_shared_new_slot_preserves_previous_audio_and_confirmation(prepared):
    from app.main import app
    uid, _, calls = prepared
    base = f'/api/projects/{uid}/workbench'
    root = crud.project_dir(uid)
    with TestClient(app) as client:
        for line_id, take in ((1, 1), (2, 2)):
            assert synthesize(client, f'{base}/lines/{line_id}/synthesize',
                              json={'take': take}).status_code == 200
            assert client.post(f'{base}/lines/{line_id}/confirm', json={}).status_code == 200
        previous = {**snapshot_slot(root, 1, 1), **snapshot_slot(root, 2, 2)}
        # 全部重做明确包括选用原声的句子，但只在成功后改用新配音。
        assert client.put(f'{base}/lines/2/selection',
                          json={'source': 'original', 'take': 2}).status_code == 200
        result = synthesize(client, base + '/synthesize', json={
            'line_ids': [2, 1, 1], 'takes': {'1': 3, '2': 3}, 'force': True})
        assert result.status_code == 200
        assert [(row['id'], row['take']) for row in result.json()['lines']] == [(1, 3), (2, 3)]
        assert calls == ['你好', '世界', '你好', '世界']
        assert_preserved(previous)
        lines = client.get(base + '?probe=false').json()['lines']
        for line, previous_take in zip(lines, (1, 2)):
            assert line['selection']['source'] == 'dub' and line['selection']['take'] == 3
            assert [(version['id'], version['confirmed']) for version in line['versions']] == [
                (previous_take, True), (3, False)]
            assert client.put(f"{base}/lines/{line['id']}/selection",
                              json={'source': 'dub', 'take': previous_take}).status_code == 200
        # 切回旧槽仍保持满意；新生成的版本没有覆盖旧槽的评价。
        assert all(line['confirmed'] for line in client.get(base + '?probe=false').json()['lines'])


def test_explicit_slot_overwrites_that_slot_and_fills_missing_sentences(prepared):
    from app.main import app
    uid, _, calls = prepared
    base = f'/api/projects/{uid}/workbench'
    root = crud.project_dir(uid)
    with TestClient(app) as client:
        for line_id, take in ((1, 1), (1, 3), (2, 1)):
            assert synthesize(client, f'{base}/lines/{line_id}/synthesize',
                              json={'take': take}).status_code == 200
        assert client.post(base + '/confirm', json={'line_ids': [1, 2]}).status_code == 200
        unaffected = {**snapshot_slot(root, 1, 1), **snapshot_slot(root, 2, 1)}
        overwritten = dubbing_edit.take_path(root, 1, 3).read_bytes()
        result = synthesize(client, base + '/synthesize', json={
            'line_ids': [1, 2], 'takes': {'1': 3, '2': 3}, 'force': True})
        assert result.status_code == 200 and len(calls) == 5
        assert dubbing_edit.take_path(root, 1, 3).read_bytes() != overwritten
        assert_preserved(unaffected)
        lines = client.get(base + '?probe=false').json()['lines']
        assert [line['selection']['take'] for line in lines] == [3, 3]
        assert not any(line['confirmed'] for line in lines)
        assert [(version['id'], version['confirmed']) for version in lines[1]['versions']] == [
            (1, True), (3, False)]


@pytest.mark.parametrize('target', [1, 3])
@pytest.mark.parametrize('failure', ['invalid_audio', 'timeout'])
def test_failed_target_keeps_previous_slot_and_selected_voice(index_prepared, monkeypatch, target, failure):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    base = f'/api/projects/{uid}/workbench'
    for line_id in (1, 2):
        assert synthesize(client, f'{base}/lines/{line_id}/synthesize', json={'take': 1}).status_code == 200
    assert client.post(base + '/confirm', json={'line_ids': [1, 2]}).status_code == 200
    previous = snapshot_slot(root, 1, 1)
    if failure == 'timeout' or target != 1:
        previous.update(snapshot_slot(root, 2, 1))
    original = tts_indextts25.synth

    def fail_first(url, text, out, lang, **kwargs):
        if text == '你好':
            if failure == 'timeout':
                raise DubForgeError('隔离测试请求超时', code='TTS_REQUEST_TIMEOUT')
            atomic_write_bytes(out, b'invalid audio')
            return out
        return original(url, text, out, lang, **kwargs)

    monkeypatch.setattr(tts_indextts25, 'synth', fail_first)
    response = client.post(base + '/synthesize', json={
        'line_ids': [1, 2], 'takes': {'1': target, '2': target}, 'force': True})
    assert response.status_code == 200 and finished(project).status == 'failed'
    assert_preserved(previous)
    lines = client.get(base + '?probe=false').json()['lines']
    assert lines[0]['selection']['take'] == 1 and lines[0]['confirmed']
    assert lines[0]['wav']['exists']
    if target != 1:
        assert not dubbing_edit.take_path(root, 1, target).exists()
    if failure == 'timeout':
        assert lines[1]['selection']['take'] == 1 and lines[1]['confirmed']
    else:
        assert lines[1]['selection']['take'] == target and not lines[1]['confirmed']
    assert not list((root / 'artifacts/tts').glob('*.tmp'))
    client.close()


def test_new_slot_limit_rejects_before_a_task_is_created(prepared):
    uid, project, calls = prepared
    root = crud.project_dir(uid)
    atomic_write_bytes(dubbing_edit.take_path(root, 1, 10000), b'previous audio')
    with pytest.raises(DubForgeError) as error:
        runner.start_dubbing(uid, [1], takes={'1': None})
    assert error.value.code == 'DUB_TAKE_INVALID'
    assert crud.latest_job(project.id, 'tts') is None and not calls
    # 显式目标的上界和共同槽写入同一校验路径；无效编号不创建队列。
    with pytest.raises(DubForgeError) as error:
        dubbing_task.prepare(project, Dependencies(project), [1, 2], takes={'1': 10001, '2': 10001})
    assert error.value.code == 'DUB_TAKE_INVALID'


@pytest.mark.parametrize(('line_ids', 'expected_take'), [([1], 2), ([1, 2], 3)])
def test_new_slot_uses_only_selected_sentences_history(prepared, line_ids, expected_take):
    uid, project, calls = prepared
    root = crud.project_dir(uid)
    first = dubbing_edit.take_path(root, 1, 1)
    other = dubbing_edit.take_path(root, 2, 2)
    atomic_write_bytes(first, b'first-sentence-slot-1')
    atomic_write_bytes(other, b'other-sentence-slot-2')
    result = dubbing_task.prepare(project, Dependencies(project), line_ids,
                                 takes={str(ident): None for ident in line_ids})
    assert [(row['id'], row['take']) for row in result['lines']] == [
        (ident, expected_take) for ident in line_ids]
    assert first.read_bytes() == b'first-sentence-slot-1'
    assert other.read_bytes() == b'other-sentence-slot-2'
    assert not calls
