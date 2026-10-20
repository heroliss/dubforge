"""服务地址更新复用既有音频，旧缓存只在有完整输入证据时兼容。"""
import json

import pytest

from dubforge import config, configuration
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_tts import line_cache_valid, line_fingerprint


@pytest.fixture(params=['indextts25', 'gptsovits'])
def cached_voice(make_project, request):
    backend = request.param
    config.set_key('tts.backend', backend)
    project = crud.get_project(make_project())
    line = {'id': 1, 'start': 0, 'end': 2, 'text': '你好，世界。'}
    root = crud.project_dir(project.uid)
    path = dubbing_edit.take_path(root, 1)
    path.write_bytes(b'fixture voice')
    return backend, project, line, root, path


def save_cache(fixture, *, legacy=False, with_snapshot=True):
    backend, project, line, root, path = fixture
    fingerprint = line_fingerprint(project, line, _legacy_service=legacy)
    path.with_suffix('.input').write_text(fingerprint, encoding='utf-8')
    if with_snapshot:
        dubbing_edit.record_take(project, line, root, 1, fingerprint,
                                 Dependencies(project).cfg('tts.' + backend))
    return fingerprint


def test_new_cache_does_not_change_when_service_moves(cached_voice):
    backend, project, line, root, path = cached_voice
    before = save_cache(cached_voice)
    audio_before = path.read_bytes()
    config.set_key(f'tts.{backend}.url', 'http://127.0.0.1:19999/')
    assert Dependencies(project).cfg(f'tts.{backend}.url') == 'http://127.0.0.1:19999/'
    assert line_fingerprint(project, line) == before
    assert line_cache_valid(project, line, root)
    assert path.read_bytes() == audio_before


def test_old_service_hash_reuses_snapshot_after_service_moves(cached_voice):
    backend, project, line, root, path = cached_voice
    saved = save_cache(cached_voice, legacy=True)
    config.set_key(f'tts.{backend}.url', 'http://127.0.0.1:19999/')
    assert saved != line_fingerprint(project, line, _legacy_service=True)
    assert line_cache_valid(project, line, root)
    assert path.with_suffix('.input').read_text() == saved, '只读核对不得改写旧缓存'


def test_old_service_hash_without_history_is_not_guessed(cached_voice):
    backend, project, line, root, path = cached_voice
    save_cache(cached_voice, legacy=True, with_snapshot=False)
    assert line_cache_valid(project, line, root)
    config.set_key(f'tts.{backend}.url', 'http://127.0.0.1:19999/')
    assert not line_cache_valid(project, line, root)


def test_service_snapshot_does_not_hide_content_parameter_change(cached_voice):
    backend, project, line, root, path = cached_voice
    save_cache(cached_voice, legacy=True)
    config.set_key(f'tts.{backend}.url', 'http://127.0.0.1:19999/')
    assert not line_cache_valid(project, {**line, 'text': '另外一句。'}, root)
    key = f'tts.{backend}.' + ('temperature' if backend == 'indextts25' else 'character')
    current = configuration.resolve(project.uid)
    configuration.save(current['key'], {'revision': current['revision'],
        'updates': {key: .4 if backend == 'indextts25' else '新音色'}})
    assert not line_cache_valid(project, line, root)


def test_service_snapshot_must_match_audio_version_and_marker(cached_voice):
    backend, project, line, root, path = cached_voice
    save_cache(cached_voice, legacy=True)
    config.set_key(f'tts.{backend}.url', 'http://127.0.0.1:19999/')
    snapshot_path = path.with_suffix('.take.json')
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
    snapshot['fingerprint'] = 'unknown'
    snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
    assert not line_cache_valid(project, line, root)
    save_cache(cached_voice, legacy=True)
    path.write_bytes(b'changed fixture voice')
    assert not line_cache_valid(project, line, root)
