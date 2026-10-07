"""真实 FFmpeg 验证字幕样式预览、时钟、双语及成片的共用渲染。"""
import subprocess

import numpy as np
import pytest

from dubforge.errors import ConfigError
from dubforge.pipeline.subtitle_style import STYLE_DEFAULTS, preview_frame, subtitle_filter
from dubforge.utils.srt import SubLine


def cfg(values):
    return lambda key, default=None: values.get(key, default)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / '有 空格.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'color=c=black:s=640x360:r=10:d=3', '-c:v', 'libx264', str(path)], check=True)
    return path


def pixels(data):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-f', 'rawvideo',
                          '-pix_fmt', 'rgb24', 'pipe:1'], input=data, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(360, 640, 3)


def test_preview_time_color_position_bilingual_and_scale(video):
    lines = [SubLine(1, 1, 2, '第一行\nSecond line')]
    settings = {**STYLE_DEFAULTS, 'assemble.subtitle_font': 'Arial',
                'assemble.subtitle_color': '#FF0000', 'assemble.subtitle_size': 72}
    blank = pixels(preview_frame(video, lines, cfg(settings), .5))
    red = pixels(preview_frame(video, lines, cfg(settings), 1.4))
    moved = pixels(preview_frame(video, lines, cfg({**settings, 'assemble.subtitle_margin': 180}), 1.4))
    assert blank.max() == 0
    mask = (red[:, :, 0] > 100) & (red[:, :, 1] < 40)
    assert mask.sum() > 100
    rows = np.flatnonzero(mask.any(axis=1))
    assert rows[-1] > 320
    # 两行分别渲染，且字号按 1080 高画布缩放；不会撑满画面。
    assert 25 < rows[-1] - rows[0] < 60
    moved_rows = np.flatnonzero((moved[:, :, 0] > 100).any(axis=1))
    assert 40 < rows[-1] - moved_rows[-1] < 46


@pytest.mark.parametrize('updates', [
    {'assemble.subtitle_font': "Arial',Outline=9"},
    {'assemble.subtitle_color': 'red'},
    {'assemble.subtitle_size': None},
    {'assemble.subtitle_size': -1},
    {'assemble.subtitle_outline': float('inf')},
    {'assemble.subtitle_track': '../../bad'},
    {'assemble.subtitle_track': []},
    {'subtitles.source_first': 'false'},
    {'subtitles.min_duration': -1},
    {'subtitles.min_duration': 4},
    {'subtitles.min_duration': None},
])
def test_style_validation(updates):
    from dubforge.pipeline.step_params import validate_numeric_updates
    with pytest.raises(ConfigError):
        validate_numeric_updates(updates)


def test_preview_api_without_tts_or_export(make_project, video, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import workbench
    from conftest import mark_upstream_done
    from dubforge.domain import crud
    uid = make_project()
    p = crud.get_project(uid)
    path = crud.project_dir(uid) / 'artifacts/split/split.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('1. 1.00-2.00: 原文字幕\n', encoding='utf-8')
    crud.add_artifact(p.id, 'split', 'txt', 'split.txt', path, 'artifacts/split/split.txt')
    mark_upstream_done(uid, 'translate')
    monkeypatch.setattr(workbench, 'resolve_video', lambda *_: video)
    with TestClient(app) as client:
        response = client.post(f'/api/projects/{uid}/workbench/subtitle-preview', json={
            'seconds': 1.4, 'settings': {'assemble.subtitle_track': 'source'}})
        assert response.status_code == 200, response.text[:100] if response.status_code != 200 else ''
        assert response.headers['content-type'] == 'image/png'
        assert response.headers['x-subtitle-active'] == '1'
        assert response.content.startswith(b'\x89PNG')
        from dubforge.configuration import resolve
        assert not resolve(uid)['overrides']
        assert not (crud.project_dir(uid) / 'srt/source.srt').exists()
        missing = client.post(f'/api/projects/{uid}/workbench/subtitle-preview', json={
            'seconds': 1.4, 'settings': {'assemble.subtitle_track': 'trans'}})
        assert missing.status_code == 400


def test_burn_uses_same_filter(make_project, video, monkeypatch, tmp_path):
    from dubforge.pipeline import step_assemble
    from dubforge.pipeline.subtitle_style import video_canvas
    from dubforge.utils.srt import write_srt
    from types import SimpleNamespace
    srt = tmp_path / 'srt/trans.srt'
    write_srt([SubLine(1, 1, 2, '画面字幕')], srt)
    settings = {**STYLE_DEFAULTS, 'assemble.burn_subtitles': True,
                'assemble.bgm_volume': 0, 'assemble.subtitle_color': '#12AB34',
                'content_review.enabled': False}  # 此用例单独验证未启用内容处理时的原有样式滤镜。
    commands = []
    def run(cmd, cwd=None, **_):
        commands.append(cmd)
        (cwd / cmd[-1]).write_bytes(b'mock mp4')
        return 0, '', ''
    ctx = SimpleNamespace(cfg=cfg(settings), upstream=lambda *_: srt, run_subprocess=run, task_data={})
    out = tmp_path / 'final.mp4'
    step_assemble._mux(ctx, tmp_path, video, tmp_path / 'voice.wav', out, burn=True)
    expected = subtitle_filter(srt.name, ctx.cfg, video_canvas(video))
    assert f'[0:v]{expected}[v]' in commands[0][commands[0].index('-filter_complex') + 1]
    assert out.read_bytes() == b'mock mp4'


def test_workspace_preview_uses_unsaved_order_and_duration(make_project, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import workbench
    from conftest import mark_upstream_done
    from dubforge.domain import crud
    from dubforge.pipeline import subtitle_style
    from dubforge.utils.fs import atomic_write_bytes

    uid = make_project()
    project, root = crud.get_project(uid), crud.project_dir(uid)
    for key, text in {
        'split': '1. 1.00-1.20: Hello\n2. 3.00-3.50: World\n',
        'translate': '1. 你好\n2. 世界\n',
    }.items():
        path = root / f'artifacts/{key}/{key}.txt'
        atomic_write_bytes(path, text.encode('utf-8'))
        crud.add_artifact(project.id, key, 'text', path.name, path, str(path.relative_to(root)))
    mark_upstream_done(uid, 'subtitles')
    captured = []

    def render(_video, lines, cfg, _seconds):
        captured.append((lines, cfg('subtitles.source_first'), cfg('subtitles.min_duration')))
        return b'\x89PNG'

    monkeypatch.setattr(workbench, 'resolve_video', lambda *_: tmp_path / 'fixture.mp4')
    monkeypatch.setattr(subtitle_style, 'preview_frame', render)
    before = (root / 'manifest.json').read_bytes()
    with TestClient(app) as client:
        response = client.post(f'/api/projects/{uid}/workbench/subtitle-preview', json={
            'seconds': 1.8, 'settings': {'assemble.subtitle_track': 'bilingual',
                'subtitles.source_first': True, 'subtitles.min_duration': 2}})
        assert response.status_code == 200
        assert response.headers['x-subtitle-active'] == '1'
        lines, source_first, minimum = captured[0]
        assert source_first is True and minimum == 2
        assert lines[0].text == 'Hello\n你好' and lines[0].end == 3
        invalid = client.post(f'/api/projects/{uid}/workbench/subtitle-preview', json={
            'seconds': 1.8, 'settings': {'subtitles.min_duration': 10}})
        assert invalid.status_code == 400 and len(captured) == 1
    from dubforge.configuration import resolve
    assert not resolve(uid)['overrides']
    assert (root / 'manifest.json').read_bytes() == before
    assert not (root / 'srt/bilingual.srt').exists()


def test_workspace_settings_save_as_one_transaction(make_project):
    from fastapi.testclient import TestClient
    from app.main import app
    from dubforge.domain import crud

    uid = make_project()
    with TestClient(app) as client:
        endpoint = f'/api/projects/{uid}/step_params'
        values = {'assemble.subtitle_size': 72, 'subtitles.source_first': True,
                  'subtitles.min_duration': 2, 'subtitles.mode': 'bilingual'}
        assert client.put(endpoint, json={'updates': values}).status_code == 200
        assert all(crud.project_step_params(crud.get_project(uid))[k] == v for k, v in values.items())
        # 一个字段无效时，另一个步骤的合法字段也不能被部分保存。
        response = client.put(endpoint, json={'updates': {
            'assemble.subtitle_size': 90, 'subtitles.min_duration': -1}})
        assert response.status_code == 400
        assert all(crud.project_step_params(crud.get_project(uid))[k] == v for k, v in values.items())
