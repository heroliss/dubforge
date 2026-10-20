from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import audio_processing as api
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_post


@pytest.fixture()
def audio_api(isolated_env, monkeypatch):
    project = crud.create_project('后期音量测试', 'https://example.invalid/audio')
    root = crud.project_dir_by_project(project)
    row = dict(id=1, text='测试语音', start=0., end=2., source='dub', take=1)
    from dubforge.pipeline.dubbing_edit import take_path
    audio = take_path(root, 1, 1)
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b'first-audio')

    class Dependencies:
        def __init__(self, current):
            self.root = root

        def production_lines(self):
            return [dict(row)]

        def path(self, step, name):
            return None

    monkeypatch.setattr(api, 'Dependencies', Dependencies)
    app = FastAPI()
    app.include_router(api.router)

    @app.exception_handler(DubForgeError)
    async def error(request, exc):
        return JSONResponse(exc.to_dict(), status_code=400)

    return project, row, audio, TestClient(app)


def test_line_gain_get_save_reset_and_no_audio_rewrite(audio_api):
    project, row, audio, client = audio_api
    url = f'/api/projects/{project.uid}/audio/lines/1'
    before = audio.read_bytes()
    response = client.get(url)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    state = response.json()
    result = client.put(url, json=dict(binding=state['binding'], revision=state['revision'], gain_db=-3))
    assert result.status_code == 200
    assert result.json()['effective_gain_db'] == -3
    assert audio.read_bytes() == before
    current = result.json()
    result = client.put(url, json=dict(binding=current['binding'], revision=current['revision'], reset=True))
    assert result.json()['effective_gain_db'] == 0
    assert not audio_post.records(crud.get_project(project.uid))
    assert audio.read_bytes() == before


def test_changed_audio_or_old_revision_cannot_silently_reapply_gain(audio_api):
    project, row, audio, client = audio_api
    url = f'/api/projects/{project.uid}/audio/lines/1'
    state = client.get(url).json()
    assert client.put(url, json=dict(binding=state['binding'], revision=state['revision'], gain_db=2)).status_code == 200
    assert client.put(url, json=dict(binding=state['binding'], revision=state['revision'], gain_db=3)).json()['code'] == 'AUDIO_GAIN_CONFLICT'
    audio.write_bytes(b'replacement-with-a-different-size')
    stale = client.get(url).json()
    assert stale['stale'] and stale['gain_db'] == 0 and stale['saved_gain_db'] == 2
    reapplied = client.put(url, json=dict(binding=stale['binding'], revision=stale['revision'], gain_db=2))
    assert reapplied.status_code == 200 and not reapplied.json()['stale']


def test_tts_does_not_lock_post_gain_but_running_assemble_does(audio_api):
    project, row, audio, client = audio_api
    url = f'/api/projects/{project.uid}/audio/lines/1'
    tts = crud.get_step(project.id, 'tts')
    tts.status = 'running'
    crud.save_step(tts)
    state = client.get(url).json()
    result = client.put(url, json=dict(binding=state['binding'], revision=state['revision'], gain_db=-2))
    assert result.status_code == 200
    assemble = crud.get_step(project.id, 'assemble')
    assemble.status = 'running'
    crud.save_step(assemble)
    state = result.json()
    result = client.put(url, json=dict(binding=state['binding'], revision=state['revision'], gain_db=-1))
    assert result.json()['code'] == 'CONFIGURATION_BUSY'
    assert client.get(url).json()['effective_gain_db'] == -2


def test_save_reloads_line_inside_edit_gate_and_unknown_targets_are_explicit(audio_api, monkeypatch):
    project, row, audio, client = audio_api
    url = f'/api/projects/{project.uid}/audio/lines/1'
    state = client.get(url).json()
    edit = api.dubbing_session.edit

    @contextmanager
    def changed(current, *, paths):
        assert paths == {'assemble.audio_line_gains'}
        with edit(current, paths=paths):
            row['take'] = 2
            yield

    monkeypatch.setattr(api.dubbing_session, 'edit', changed)
    result = client.put(url, json=dict(binding=state['binding'], revision=state['revision'], gain_db=2))
    assert result.json()['code'] == 'AUDIO_GAIN_CONFLICT'
    assert client.get(f'/api/projects/{project.uid}/audio/lines/99').json()['code'] == 'LINE_NOT_FOUND'
    assert client.get('/api/projects/missing/audio/lines/1').json()['code'] == 'PROJECT_NOT_FOUND'
