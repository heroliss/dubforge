"""工作台计划与正式/试片铺轨使用同一份实际时长，失败保留旧音轨。"""
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from conftest import synthesize
from test_dubbing_preparation import prepared
from dubforge import config
from dubforge.domain import crud
from test_configuration import save
from dubforge.errors import DubForgeError
from dubforge.pipeline import step_assemble
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils import audio as au


def context(deps, logs):
    class Context:
        project, upstream, cfg = deps.project, deps.upstream, deps.cfg
        def check_cancel(self): pass
        def log(self, message): logs.append(message)
        def register_file(self, *args): pass
    return Context()


def test_workbench_and_actual_mix_share_selected_audio_plan(prepared, monkeypatch):
    from app.main import app
    from app.api import workbench
    uid, _, _ = prepared
    root = crud.project_dir(uid)
    save('project:' + uid, {'assemble.loudnorm': False})
    video = root / 'media/video.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b'test video')
    monkeypatch.setattr(workbench, '_video_duration', lambda path: 6 if path == video else None)
    captured = []
    real_plan = step_assemble.build_plan
    def capture(*args):
        plan = real_plan(*args)
        captured.append(plan)
        return plan
    monkeypatch.setattr(step_assemble, 'build_plan', capture)
    with TestClient(app) as client:
        base = f'/api/projects/{uid}/workbench'
        for line_id in (1, 2):
            assert synthesize(client, f'{base}/lines/{line_id}/synthesize', json={}).status_code == 200
        state = client.get(base + '?probe=false').json()
        assert state['placement']['video_duration'] == 6
        # 音频头中的实际时长保留精度，不能先按两位小数取整再用于冲突判断。
        assert state['lines'][0]['wav']['seconds'] == pytest.approx(1 + 1 / au.SR)
        deps = Dependencies(crud.get_project(uid))
        step_assemble._mix_timeline(context(deps, []), root, 6)
        keys = ('id', 'source', 'take', 'start', 'source_end', 'seconds', 'audio_end')
        assert [[row[k] for k in keys] for row in state['placement']['lines']] == [
            [row[k] for k in keys] for row in captured[0]['lines']]
        assert state['placement']['issues'] == captured[0]['issues']


def test_formal_overrun_preserves_old_track_but_explicit_preview_can_crop(prepared, monkeypatch):
    from app.main import app
    uid, _, _ = prepared
    root = crud.project_dir(uid)
    save('project:' + uid, {'assemble.loudnorm': False})
    with TestClient(app) as client:
        base = f'/api/projects/{uid}/workbench'
        assert synthesize(client, base + '/lines/1/synthesize', json={}).status_code == 200
        assert client.put(base + '/lines/2/selection', json={'source': 'dub', 'take': None}).status_code == 200
    # 本测试只验证真实音频边界；产物有效性已经由配音完整性用例覆盖。
    monkeypatch.setattr(step_assemble, '_require_dubbing', lambda ctx: None)
    wav = root / 'artifacts/tts/1.wav'
    sf.write(wav, np.full(7 * au.SR, .3, dtype=np.float32), au.SR, subtype='PCM_16')
    old_track = root / 'audio/dub_full.wav'
    old_track.parent.mkdir(exist_ok=True)
    old_track.write_bytes(b'previous complete audio')
    deps = Dependencies(crud.get_project(uid))
    with pytest.raises(DubForgeError) as exc:
        step_assemble._mix_timeline(context(deps, []), root, 6)
    assert exc.value.code == 'DUB_AUDIO_OUTSIDE_VIDEO'
    assert old_track.read_bytes() == b'previous complete audio'
    preview = step_assemble._mix_timeline(context(deps, []), root, 6, start=1, end=2)
    data, sr = sf.read(preview)
    assert len(data) == sr and data[int(.5 * sr)] > 0
    assert old_track.read_bytes() == b'previous complete audio'


def test_video_duration_probe_is_cached_by_actual_file_version(tmp_path, monkeypatch, isolated_env):
    from app.api import workbench
    file = tmp_path / 'video.mp4'
    file.write_bytes(b'first')
    calls = []
    def probe(path):
        calls.append(path)
        return 6 if path.stat().st_size == 5 else 8
    monkeypatch.setattr(step_assemble, '_ffprobe_duration', probe)
    workbench._cached_video_duration.cache_clear()
    assert workbench._video_duration(file) == 6
    assert workbench._video_duration(file) == 6
    file.write_bytes(b'replacement')
    assert workbench._video_duration(file) == 8
    assert len(calls) == 2


@pytest.mark.parametrize('unknown', [False, True])
def test_unreadable_selected_audio_never_silently_becomes_empty(prepared, monkeypatch, unknown):
    from app.main import app
    from dubforge.pipeline import tts_duration
    uid, _, _ = prepared
    root = crud.project_dir(uid)
    save('project:' + uid, {'assemble.loudnorm': False})
    with TestClient(app) as client:
        base = f'/api/projects/{uid}/workbench'
        assert synthesize(client, base + '/lines/1/synthesize', json={}).status_code == 200
        assert client.put(base + '/lines/2/selection', json={'source': 'dub', 'take': None}).status_code == 200
    monkeypatch.setattr(step_assemble, '_require_dubbing', lambda ctx: None)
    if unknown:
        monkeypatch.setattr(tts_duration, 'audio_seconds', lambda path: None)
    else:
        (root / 'artifacts/tts/1.wav').write_bytes(b'broken wav')
    old_track = root / 'audio/dub_full.wav'
    old_track.parent.mkdir(exist_ok=True)
    old_track.write_bytes(b'previous complete audio')
    deps = Dependencies(crud.get_project(uid))
    with pytest.raises(DubForgeError) as exc:
        step_assemble._mix_timeline(context(deps, []), root, 6)
    assert exc.value.code == 'TTS_AUDIO_INVALID'
    assert old_track.read_bytes() == b'previous complete audio'
    with pytest.raises(DubForgeError):
        step_assemble._mix_timeline(context(deps, []), root, 6, start=1, end=2)
