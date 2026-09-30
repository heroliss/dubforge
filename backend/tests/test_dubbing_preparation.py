"""试配和批量配音自动准备任务，共用参数与音频缓存，不调用外部服务。"""
import io
import wave

import pytest
from fastapi.testclient import TestClient

from conftest import synthesize, wait_status
from dubforge import config
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies, selected_steps
from dubforge.pipeline.step_tts import line_fingerprint
from dubforge.runner import compute_fingerprint, runner
from dubforge.utils.fs import atomic_write_bytes


@pytest.fixture
def prepared(make_project, monkeypatch):
    from dubforge.backends import tts_gptsovits
    uid = make_project(params={"step_params": {"tts.backend": "gptsovits"}})
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    for key, name, text in [
        ("split", "split.txt", "1. 0.000-2.000: Hello\n2. 3.000-5.000: World\n"),
        ("translate", "translate.txt", "1. 你好\n2. 世界\n"),
    ]:
        path = root / "artifacts" / key / name
        atomic_write_bytes(path, text.encode())
        crud.add_artifact(project.id, key, "text", name, path, str(path.relative_to(root)))
        step = crud.get_step(project.id, key)
        step.status = "done"
        crud.save_step(step)
    calls = []

    def synth(url, text, out, lang, **kwargs):
        calls.append(text)
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 32000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x00" * (32000 + len(calls)))
        atomic_write_bytes(out, audio.getvalue())
        return out

    monkeypatch.setattr(tts_gptsovits, "synth", synth)
    return uid, project, calls


def test_trial_and_batch_need_no_plan_step(prepared):
    from app.main import app
    uid, project, calls = prepared
    old_step = crud.get_step(project.id, "dubtask")
    old_step.status = "failed"
    crud.save_step(old_step)
    deps = Dependencies(project)
    assert deps.plan(["tts"], True)["steps"] == ["tts"]
    assert "dubtask" not in selected_steps(project)
    with TestClient(app) as client:
        base = f"/api/projects/{uid}/workbench"
        state = client.get(base + "?probe=false").json()
        assert state["counts"]["total"] == 2 and not state["unready_inputs"]
        assert "dubtask" not in [s["key"] for s in client.get(f"/api/projects/{uid}/steps").json()]
        assert client.post(base + "/lines/1/confirm", json={"confirmed": True}).status_code == 400
        assert synthesize(client, base + "/lines/1/synthesize", json={}).status_code == 200
        assert client.post(base + "/lines/1/confirm", json={"confirmed": True}).status_code == 200
        assert not client.get(base + "?probe=false").json()["completed"]
        runner.start_step(uid, "tts")
        wait_status(uid, "tts", {"done"})
        assert calls == ["你好", "世界"]  # 批量步骤复用已试听的第一句。
    assert not (crud.project_dir(uid) / "artifacts/dubtask/dubtask.json").exists()


def test_parameter_changes_preserve_recorded_versions_for_comparison(prepared):
    from app.main import app
    uid, project, _ = prepared
    line = Dependencies(project).dubbing_lines(required=True)[0]
    before = line_fingerprint(project, line)
    assert line_fingerprint(project, {**line, "gap": 999, "speed_flag": 2, "est_dur": 99}) == before
    batch_before = compute_fingerprint(project, "tts")
    with TestClient(app) as client:
        base = f"/api/projects/{uid}/workbench"
        synthesize(client, base + "/lines/1/synthesize", json={})
        client.post(base + "/lines/1/confirm", json={"confirmed": True})
        synthesize(client, base + "/lines/2/synthesize", json={})
        config.set_key("tts.tolerance", 1.2)
        assert compute_fingerprint(project, "tts") == batch_before
        assert client.get(base + "?probe=false").json()["completed"]
        assert Dependencies(crud.get_project(uid)).fresh("tts")
        assert crud.get_step(project.id, "tts").started_at is not None
        previous_default = config.get("tts.gptsovits.character")
        result = client.put(f"/api/projects/{uid}/step_params", json={
            "updates": {"tts.gptsovits.character": "另一个角色"}, "reset": []})
        assert result.status_code == 200
        assert config.get("tts.gptsovits.character") == previous_default
        state = client.get(base + "?probe=false").json()
        assert state["completed"] and state["counts"]["confirmed"] == 1
        assert state["lines"][0]["wav"]["settings_changed"]
        assert state["lines"][0]["wav"]["exists"]
        assert Dependencies(crud.get_project(uid)).fresh("tts")


def test_forced_resynthesis_requires_listening_again(prepared):
    from app.main import app
    uid, _, _ = prepared
    with TestClient(app) as client:
        base = f"/api/projects/{uid}/workbench"
        synthesize(client, base + "/lines/1/synthesize", json={})
        client.post(base + "/lines/1/confirm", json={"confirmed": True})
        synthesize(client, base + "/lines/2/synthesize", json={})
        assert synthesize(client, base + "/lines/1/synthesize", json={"force": True}).status_code == 200
        state = client.get(base + "?probe=false").json()
        assert state["lines"][0]["wav"]["exists"]
        assert not state["lines"][0]["confirmed"] and state["completed"]


def test_trial_reports_real_missing_and_stale_inputs(prepared):
    from app.main import app
    uid, project, _ = prepared
    translate = crud.get_step(project.id, "translate")
    translate.status = "stale"
    crud.save_step(translate)
    with TestClient(app) as client:
        base = f"/api/projects/{uid}/workbench"
        state = client.get(base + "?probe=false").json()
        blocker, = state["unready_inputs"]
        assert blocker["name"] == "translate.txt" and blocker["state"] == "stale"
        assert synthesize(client, base + "/lines/1/synthesize", json={}).json()["code"] == "DEPS_NOT_MET"
        Dependencies(project).path("translate", "translate.txt").unlink()
        state = client.get(base + "?probe=false").json()
        assert state["unready_inputs"][0]["state"] == "deleted"
        assert len(state["lines"]) == 2
        assert all(not l['translation_available'] and not l['text'] for l in state['lines'])
        assert not state['edit_blockers']


def test_all_single_lines_complete_dubbing_without_confirmation(prepared):
    from app.main import app
    uid, project, calls = prepared
    with TestClient(app) as client:
        base = f"/api/projects/{uid}/workbench"
        synthesize(client, base + "/lines/1/synthesize", json={})
        assert crud.get_step(project.id, "tts").progress == 100  # 本次单句任务完成，不代表整份文稿已配齐。
        synthesize(client, base + "/lines/2/synthesize", json={})
        row = crud.get_step(project.id, "tts")
        assert row.status == "done" and row.progress == 100
        assert Dependencies(project).fresh("tts")
        state = client.get(base + "?probe=false").json()
        assert state["completed"] and state["counts"]["confirmed"] == 0
        assert runner.start_step(uid, "tts")["status"] == "skipped"
        assert calls == ["你好", "世界"]


def test_old_project_with_all_trial_audio_is_already_complete(prepared):
    from app.main import app
    uid, project, calls = prepared
    with TestClient(app) as client:
        for line_id in (1, 2):
            synthesize(client, f"/api/projects/{uid}/workbench/lines/{line_id}/synthesize", json={})
        row = crud.get_step(project.id, "tts")
        row.status = "pending"
        row.input_fingerprint = ""
        crud.save_step(row)
        view = next(s for s in client.get(f"/api/projects/{uid}/steps").json() if s["key"] == "tts")
        assert view["status"] == "done" and view["fresh"]
        assert view["progress"] == 100
        assert runner.start_step(uid, "tts")["status"] == "skipped"
        assert calls == ["你好", "世界"]

@pytest.fixture
def index_prepared(prepared, monkeypatch):
    from dubforge.backends import tts_gptsovits, tts_indextts25
    from app.main import app
    uid, project, calls = prepared
    ref = crud.project_dir(uid) / 'reference.wav'
    atomic_write_bytes(ref, b'reference')
    client = TestClient(app)
    config.set_key('tts.indextts25.duration_factor', 1.0)
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.backend': 'indextts25', 'tts.indextts25.timbre_mode': 'preset',
        'tts.indextts25.timbre_audio': str(ref), 'tts.indextts25.emo_mode': 'same',
        'tts.indextts25.duration_factor': 1.1}})
    received = []
    def synth(url, text, out, lang, **kwargs):
        received.append(kwargs)
        return tts_gptsovits.synth(url, text, out, lang)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    return client, uid, received


def test_line_settings_inheritance_cache_and_actual_synthesis(index_prepared):
    client, uid, received = index_prepared
    base = f'/api/projects/{uid}/workbench'
    for id in (1, 2):
        assert synthesize(client, f'{base}/lines/{id}/synthesize', json={}).status_code == 200
        assert client.post(f'{base}/lines/{id}/confirm', json={}).status_code == 200
    schema = client.get(f'{base}/lines/1/params').json()
    key = 'tts.indextts25.duration_factor'
    assert schema['defaults'][key] == 1.1
    assert schema['sources'][key] == {'kind': 'project'}
    assert not any(f['path'].endswith('.url') for f in schema['steps']['tts']['fields'])
    assert client.put(f'{base}/lines/1/params', json={'updates': {key: .8}}).status_code == 200
    sources = client.get(f'{base}/lines/1/params').json()
    assert sources['sources'][key] == {'kind': 'line'}
    assert sources['parent_sources'][key] == {'kind': 'project'}
    state = client.get(base + '?probe=false').json()
    assert state['lines'][0]['customized'] and state['lines'][0]['wav']['settings_changed']
    assert state['lines'][0]['confirmed']
    assert state['lines'][1]['confirmed'] and state['lines'][1]['wav']['exists']
    assert Dependencies(crud.get_project(uid)).fresh('tts')
    assert synthesize(client, f'{base}/lines/1/synthesize', json={'force': True}).status_code == 200
    assert [r['duration_factor'] for r in received] == [1.1, 1.1, .8]
    assert Dependencies(crud.get_project(uid)).fresh('tts')
    assert client.put(f'{base}/lines/1/params', json={'reset': [key]}).status_code == 200
    assert client.get(f'{base}/lines/1/params').json()['steps']['tts']['values'][key] == 1.1
    assert client.get(f'{base}/lines/1/params').json()['sources'][key] == {'kind': 'project'}
    assert config.get(key) == 1.0


def test_line_overrides_do_not_move_to_changed_text_and_validate_inputs(index_prepared):
    from dubforge.pipeline import line_settings
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench/lines/1/params'
    for updates in ({'tts.backend': 'gptsovits'}, {'tts.indextts25.duration_factor': None},
                    {'tts.indextts25.emo_vector': [1]}, {'tts.indextts25.emo_mode': 'bad'}):
        assert client.put(base, json={'updates': updates}).status_code == 400
    assert client.put(base, json={'updates': {'tts.indextts25.emo_text': '有点兴奋'}}).status_code == 200
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    assert line_settings.overrides(project, line)
    assert not line_settings.overrides(project, {**line, 'text': '这是全新的句子'})
    assert not line_settings.overrides(project, {**line, 'start': 1.0})


def test_per_line_reference_requires_vocal_and_custom_files_affect_fingerprint(index_prepared):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {'tts.indextts25.timbre_mode': 'dynamic'}}).status_code == 200
    assert any(x['name'] == 'vocal.wav' for x in client.get(base + '?probe=false').json()['unready_inputs'])
    ref = crud.project_dir(uid) / 'other.wav'
    atomic_write_bytes(ref, b'first')
    client.put(base + '/lines/1/params', json={'updates': {
        'tts.indextts25.timbre_mode': 'preset', 'tts.indextts25.timbre_audio': str(ref)}})
    project = crud.get_project(uid)
    before = compute_fingerprint(project, 'tts')
    atomic_write_bytes(ref, b'changed audio')
    assert compute_fingerprint(project, 'tts') != before


def test_manual_synthesis_prevents_concurrent_rerun_or_parameter_edit(index_prepared):
    from dubforge.pipeline import dubbing_session
    from dubforge.errors import DubForgeError
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    with dubbing_session.use(project, 1):
        base = f'/api/projects/{uid}/workbench'
        assert client.get(base + '?probe=false').json()['active_line'] == 1
        assert synthesize(client, base + '/lines/2/synthesize', json={}).status_code == 400
        assert client.put(base + '/lines/1/params', json={'updates': {}}).status_code == 400
        with pytest.raises(DubForgeError, match='正在合成配音'):
            runner.start_step(uid, 'tts', force=True)
    assert dubbing_session.active_line(uid) is None


def test_one_missing_reference_does_not_block_other_sentences(index_prepared):
    client, uid, received = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert synthesize(client, base + '/lines/2/synthesize', json={}).status_code == 200
    client.put(base + '/lines/1/params', json={'updates': {'tts.indextts25.timbre_mode': 'dynamic'}})
    state = client.get(base + '?probe=false').json()
    assert state['lines'][0]['unready_inputs']
    assert not state['lines'][1]['unready_inputs']
    assert state['lines'][1]['wav']['exists']
    assert synthesize(client, base + '/lines/2/synthesize', json={}).status_code == 200
    assert client.post(base + '/lines/2/confirm', json={}).status_code == 200
    assert len(received) == 1


@pytest.mark.parametrize('fails', [False, True])
def test_single_synthesis_progress_is_visible_and_cleared(index_prepared, monkeypatch, fails):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from dubforge.backends import tts_indextts25
    from dubforge.errors import DubForgeError
    from dubforge.pipeline import dubbing_session

    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    reported, release = Event(), Event()
    original = tts_indextts25.synth
    callbacks = []

    def synth(*args, **kwargs):
        callbacks.append(kwargs['progress'])
        kwargs['progress'](.2)
        reported.set()
        assert release.wait(10)
        if fails:
            raise DubForgeError('测试合成失败', code='TTS_FAILED')
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, base + '/lines/1/synthesize', json={})
        try:
            assert reported.wait(10)
            state = client.get(base + '?probe=false').json()
            assert state['active_line'] == 1 and state['active_progress'] == .2
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 200
        wait_status(uid, 'tts', {'failed' if fails else 'done'})

    state = client.get(base + '?probe=false').json()
    assert state['active_line'] is None and state['active_progress'] is None
    # 新句未上报时保持未知；上一次合成的迟到回调不能污染本句。
    with dubbing_session.use(crud.get_project(uid), 2):
        callbacks[0](.9)
        state = client.get(base + '?probe=false').json()
        assert state['active_line'] == 2 and state['active_progress'] is None


@pytest.mark.parametrize('failure', [None, 'domain', 'unexpected'])
def test_pipeline_reports_current_line_and_progress_skipping_cached_lines(index_prepared, monkeypatch, failure):
    from threading import Event
    from dubforge.backends import tts_indextts25
    from dubforge.errors import DubForgeError
    from dubforge.pipeline import dubbing_session

    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert synthesize(client, base + '/lines/1/synthesize', json={}).status_code == 200
    reported, release = Event(), Event()
    original = tts_indextts25.synth
    callbacks = []

    def synth(*args, **kwargs):
        callbacks.append(kwargs['progress'])
        assert dubbing_session.active_line(uid) == 2
        kwargs['progress'](.9)
        reported.set()
        assert release.wait(10)
        if failure == 'domain':
            raise DubForgeError('测试合成失败', code='TTS_FAILED')
        if failure == 'unexpected':
            raise RuntimeError('测试意外失败')
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    runner.start_step(uid, 'tts')
    try:
        assert reported.wait(10)
        state = client.get(base + '?probe=false').json()
        assert state['active_line'] == 2 and state['active_progress'] == .9
        assert synthesize(client, base + '/lines/1/synthesize', json={'force': True}).json()['code'] == 'PIPELINE_BUSY'
    finally:
        release.set()
    wait_status(uid, 'tts', {'failed' if failure else 'done'})
    state = client.get(base + '?probe=false').json()
    assert state['active_line'] is None and state['active_progress'] is None
    with dubbing_session.use(crud.get_project(uid), 1):
        callbacks[0](1.0)
        assert dubbing_session.status(uid) == {'active_line': 1, 'active_progress': None}


def test_volume_save_and_voice_selection_do_not_overwrite_each_other(index_prepared, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event
    from dubforge.pipeline import dubbing_session

    client, uid, _ = index_prepared
    volume_writing, selection_waiting, release = Event(), Event(), Event()
    from dubforge import configuration
    check_idle, edit = configuration._check_idle, dubbing_session.edit

    def slow_check(session, key):
        if not volume_writing.is_set():
            volume_writing.set()
            assert release.wait(10)
        return check_idle(session, key)

    @contextmanager
    def tracked_edit(project):
        selection_waiting.set()
        with edit(project):
            yield

    monkeypatch.setattr(configuration, '_check_idle', slow_check)
    monkeypatch.setattr(dubbing_session, 'edit', tracked_edit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        volume = pool.submit(client.put, f'/api/projects/{uid}/step_params',
                             json={'updates': {'assemble.voice_volume': .45}})
        try:
            assert volume_writing.wait(10)
            selection = pool.submit(client.put, f'/api/projects/{uid}/workbench/lines/1/selection',
                                    json={'source': 'dub', 'take': None})
            assert selection_waiting.wait(10)
            assert not selection.done(), '参数读改写尚未完成时，选择保存必须等待共同的锁'
        finally:
            release.set()
        assert volume.result(timeout=10).status_code == 200
        assert selection.result(timeout=10).status_code == 200
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    assert state['mix']['voice'] == .45
    assert state['lines'][0]['selection']['source'] == 'dub'
    assert state['lines'][0]['selection']['take'] is None
