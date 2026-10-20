"""配音版本、试听选择与真实成片铺轨一致；不调用外部服务。"""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from test_dubbing_preparation import prepared
from dubforge import config
from dubforge.domain import crud
from test_configuration import save
from dubforge.pipeline import dubbing_edit
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint, runner
from conftest import synthesize, wait_status


def test_versions_overwrite_confirmation_and_selected_dependencies(prepared):
    from app.main import app
    uid, _, calls = prepared
    base = f'/api/projects/{uid}/workbench'
    root = crud.project_dir(uid)
    with TestClient(app) as client:
        assert synthesize(client, base + '/lines/1/synthesize', json={}).json()['take'] == 1
        first = (root / 'artifacts/tts/1.wav').read_bytes()
        assert client.post(base + '/confirm', json={'line_ids': [1, 2]}).json()['skipped'] == 1
        assert client.put(base + '/lines/1/selection', json={'source': 'dub', 'take': None}).status_code == 200
        assert synthesize(client, base + '/lines/1/synthesize', json={'take': None}).json()['take'] == 2
        assert (root / 'artifacts/tts/1.wav').read_bytes() == first
        state = client.get(base + '?probe=false').json()
        assert [v['confirmed'] for v in state['lines'][0]['versions']] == [True, False]
        deps = Dependencies(crud.get_project(uid))
        assert any(n == '1_v2.wav' for _, n, _ in deps.inputs('assemble'))
        assert not any(n == '1.wav' for _, n, _ in deps.inputs('assemble'))
        before = compute_fingerprint(crud.get_project(uid), 'assemble')
        assert client.put(base + '/lines/1/selection', json={'source': 'dub', 'take': 1}).status_code == 200
        assert compute_fingerprint(crud.get_project(uid), 'assemble') != before
        other = (root / 'artifacts/tts/1_v2.wav').read_bytes()
        assert synthesize(client, base + '/lines/1/synthesize', json={'force': True}).json()['take'] == 1
        assert (root / 'artifacts/tts/1_v2.wav').read_bytes() == other
        assert not client.get(base + '?probe=false').json()['lines'][0]['confirmed']
        # 正式强制重跑也只覆盖选用版本，不清除备选版本。
        runner.start_step(uid, 'tts', force=True)
        wait_status(uid, 'tts', {'done'})
        assert (root / 'artifacts/tts/1_v2.wav').read_bytes() == other
        assert len(calls) == 5


@pytest.mark.parametrize('take', [0, -1, 1.5, True, '2', 10001])
def test_invalid_variant_rejected(prepared, take):
    from app.main import app
    uid, _, _ = prepared
    with TestClient(app) as client:
        r = synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={'take': take})
        assert r.status_code == 400


def test_empty_original_and_versions_are_used_in_actual_mix(prepared, monkeypatch):
    from app.main import app
    from dubforge.pipeline.step_assemble import _mix_timeline
    from dubforge.utils import audio as au
    import soundfile as sf
    uid, _, _ = prepared
    root = crud.project_dir(uid)
    save('project:' + uid, {'assemble.loudnorm': False, 'assemble.bgm_volume': 0})
    with TestClient(app) as client:
        base = f'/api/projects/{uid}/workbench'
        for id in (1, 2):
            assert client.put(f'{base}/lines/{id}/selection', json={'source': 'dub', 'take': None}).status_code == 200
        deps = Dependencies(crud.get_project(uid))
        assert not any(k in {'tts', 'preprocess'} for k, _, _ in deps.inputs('assemble'))
        # 使用真实铺轨代码，只替换媒体解码，以波形区分原声和不同配音。
        used = []
        def load(path):
            used.append(path.name)
            return np.full(6 * au.SR if path.name == 'vocal.wav' else au.SR,
                           .5 if path.name == 'vocal.wav' else -.25, dtype=np.float32)
        monkeypatch.setattr(au, 'load_mono_32k', load)
        class Context:
            project = deps.project
            upstream = deps.upstream
            cfg = deps.cfg
            def check_cancel(self): pass
            def log(self, message): pass
            def register_file(self, *args): pass
        audio, sr = sf.read(_mix_timeline(Context(), root, 6))
        assert sr == au.SR and len(audio) == 6 * sr and not audio.any() and not used
        assert synthesize(client, base + '/lines/1/synthesize', json={'take': None}).status_code == 200
        assert synthesize(client, base + '/lines/1/synthesize', json={'take': None}).status_code == 200
        assert client.put(base + '/lines/2/selection', json={'source': 'original', 'take': None}).status_code == 200
        vocal = root / 'audio/vocal.wav'
        vocal.write_bytes(b'test')
        p = crud.get_project(uid)
        crud.add_artifact(p.id, 'preprocess', 'audio', 'vocal.wav', vocal, str(vocal.relative_to(root)))
        step = crud.get_step(p.id, 'preprocess'); step.status = 'done'; crud.save_step(step)
        deps = Dependencies(p)
        Context.project, Context.upstream, Context.cfg = p, deps.upstream, deps.cfg
        audio, sr = sf.read(_mix_timeline(Context(), root, 6))
        assert used == ['1_v2.wav', 'vocal.wav']
        assert audio[int(.5 * sr)] < 0 and audio[int(3.5 * sr)] > 0
        assert audio[int(2.5 * sr)] == 0 and audio[int(5.5 * sr)] == 0
        # 文本/时间变化不能继承旧版本、旧选择；参数变化则已由其他测试覆盖。
        line = deps.dubbing_lines()[0]
        assert not dubbing_edit.playable(p, {**line, 'text': '新的译文'}, root, 2)
        assert not dubbing_edit.selection(p, {**line, 'start': .1}, root)['explicit']


def test_fill_remaining_does_not_overwrite_comparison_version(prepared):
    from app.main import app
    uid, _, calls = prepared
    with TestClient(app) as client:
        assert synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={}).status_code == 200
        path = crud.project_dir(uid) / 'artifacts/tts/1.wav'
        before = path.read_bytes()
        save('project:' + uid, {'tts.gptsovits.character': '新角色'})
        runner.start_step(uid, 'tts')
        wait_status(uid, 'tts', {'done'})
        assert path.read_bytes() == before and calls == ['你好', '世界']
        # 步骤级生成的第二句没有显式选择记录，删文件仍应报告缺失配音。
        path.with_name('2.wav').unlink()
        deps = Dependencies(crud.get_project(uid))
        assert any(i['name'] == '2.wav' and i['state'] == 'deleted' for i in deps.unready('assemble'))
        assert runner.start_step(uid, 'tts')['status'] == 'started'
        wait_status(uid, 'tts', {'done'})
        assert calls == ['你好', '世界', '世界']
        second = path.with_name('2.wav')
        second.write_bytes(second.read_bytes() + b'changed')
        assert runner.start_step(uid, 'tts')['status'] == 'started'
        wait_status(uid, 'tts', {'done'})
        assert calls == ['你好', '世界', '世界', '世界']
