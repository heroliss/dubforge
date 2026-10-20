"""指定参考句的配置合同与轻量句源接口；不调用模型或读取全部配音版本。"""
import io
import wave

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import config
from dubforge.domain import crud
from dubforge.errors import ConfigError
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates
from dubforge.utils.fs import atomic_write_bytes
from test_dubbing_preparation import index_prepared, prepared

PREFIX = 'tts.indextts25.'
FIELDS = ('timbre_reference_line_id', 'emo_reference_line_id')


def source(uid, text):
    project, root = crud.get_project(uid), crud.project_dir(uid)
    path = root / 'artifacts/split/split.txt'
    atomic_write_bytes(path, text.encode())
    crud.add_artifact(project.id, 'split', 'text', 'split.txt', path, str(path.relative_to(root)))


def test_schema_keeps_legacy_stable_enum_and_defaults_to_first_sentence(isolated_env):
    fields = {field.path: field for field in STEP_PARAM_SCHEMA['tts']}
    for kind in ('timbre', 'emo'):
        key = PREFIX + kind + '_reference_line_id'
        assert config.get(key) == 1
        assert fields[key].type == 'int' and fields[key].min == 1
        assert fields[key].depends_on == (('tts.backend', 'indextts25'), (PREFIX + kind + '_mode', 'stable'))
        assert ('指定句原声', 'stable') in fields[PREFIX + kind + '_mode'].options
        assert not any('8 秒' in label for label, value in fields[PREFIX + kind + '_mode'].options)


def test_dynamic_reference_is_presented_as_current_sentence_without_changing_configuration(isolated_env):
    fields = {field.path: field for field in STEP_PARAM_SCHEMA['tts']}
    for kind in ('timbre', 'emo'):
        field = fields[PREFIX + kind + '_mode']
        assert ('本句原声', 'dynamic') in field.options
        assert '合成每句时使用该句自身的原声' in field.help
        assert '逐句原声' not in str(field.options)
    limit = fields[PREFIX + 'dynamic_reference_max_seconds']
    assert limit.label == '本句原声截取上限（秒）'
    assert (limit.min, limit.max, limit.step) == (0.5, 15, 0.5)
    assert config.get(PREFIX + 'dynamic_reference_max_seconds') == 5.0


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', [None, False, 0, -1, 1.5, '2'])
def test_sentence_reference_ids_require_positive_integers(field, value):
    with pytest.raises(ConfigError):
        validate_numeric_updates({PREFIX + field: value})


def test_sentence_reference_selection_is_independently_inherited_and_overridden(index_prepared):
    client, uid, _ = index_prepared
    project_url = f'/api/projects/{uid}/step_params'
    line_url = f'/api/projects/{uid}/workbench/lines/1/params'
    updates = {PREFIX + 'timbre_mode': 'stable', PREFIX + 'emo_mode': 'stable',
               PREFIX + FIELDS[0]: 2, PREFIX + FIELDS[1]: 1}
    assert client.put(project_url, json={'updates': updates}).status_code == 200
    row = client.get(line_url).json()
    assert row['defaults'][PREFIX + FIELDS[0]] == 2
    assert row['defaults'][PREFIX + FIELDS[1]] == 1
    field = next(item for item in row['steps']['tts']['fields'] if item['path'] == PREFIX + FIELDS[0])
    assert field['label'] == '音色参考句'
    assert client.put(line_url, json={'updates': {PREFIX + FIELDS[0]: 1}}).status_code == 200
    row = client.get(line_url).json()
    assert row['steps']['tts']['values'][PREFIX + FIELDS[0]] == 1
    assert row['sources'][PREFIX + FIELDS[0]] == {'kind': 'line'}
    assert client.get(project_url).json()['steps']['tts']['values'][PREFIX + FIELDS[0]] == 2
    assert client.put(line_url, json={'updates': {PREFIX + FIELDS[0]: 0}}).status_code == 400
    assert client.get(line_url).json()['steps']['tts']['values'][PREFIX + FIELDS[0]] == 1


def test_reference_lines_are_available_before_translation_and_skip_voice_scan(make_project, monkeypatch):
    from app.api import workbench
    uid = make_project(params={'subtitle_edits': {'1': {'base': {'source_text': 'First source',
        'text': '', 'start': 2.0, 'end': 4.0}, 'values': {'start': 2.5, 'source_text': 'Edited caption'}}}})
    source(uid, '1. 2.000-4.000: First source\n2. 8.000-10.000: Second source\n')

    def forbid_scan(*args, **kwargs):
        raise AssertionError('参考句选择不得扫描配音版本或生成任务')

    monkeypatch.setattr(workbench, '_sample_lines', forbid_scan)
    response = TestClient(app).get(f'/api/projects/{uid}/workbench/reference-lines')
    assert response.status_code == 200
    assert response.json() == {'lines': [
        {'id': 1, 'start': 2.0, 'end': 4.0, 'text': 'First source'},
        {'id': 2, 'start': 8.0, 'end': 10.0, 'text': 'Second source'},
    ]}


def test_reference_lines_are_empty_before_source_is_ready(make_project):
    client = TestClient(app)
    uid = make_project()
    assert client.get(f'/api/projects/{uid}/workbench/reference-lines').json() == {'lines': []}
    assert client.get('/api/projects/nonexistent/workbench/reference-lines').json()['code'] == 'PROJECT_NOT_FOUND'


def test_reference_lines_show_invalid_source_instead_of_silently_empty(make_project):
    uid = make_project()
    source(uid, '2. 2.000-4.000: Wrong number\n')
    response = TestClient(app).get(f'/api/projects/{uid}/workbench/reference-lines')
    assert response.status_code == 400
    assert response.json()['code'] == 'TEXT_ALIGNMENT'


def reference_vocal(uid):
    """只播种分离人声，不要求翻译或任何配音版本。"""
    project, root = crud.get_project(uid), crud.project_dir(uid)
    data = io.BytesIO()
    with wave.open(data, 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00' * (8000 * 28))
    path = root / 'audio/vocal.wav'
    atomic_write_bytes(path, data.getvalue())
    crud.add_artifact(project.id, 'preprocess', 'audio', 'vocal.wav', path, str(path.relative_to(root)))
    step = crud.get_step(project.id, 'preprocess')
    step.status = 'done'
    crud.save_step(step)
    return path


def test_reference_audio_uses_current_separated_voice_and_source_time_without_production(make_project, monkeypatch):
    from app.api import workbench
    from dubforge.pipeline import step_tts
    uid = make_project(params={'subtitle_edits': {'1': {'base': {'source_text': 'Source',
        'text': '', 'start': 2.0, 'end': 23.0}, 'values': {'start': 8.0, 'end': 12.0}}}})
    source(uid, '1. 2.000-23.000: Source\n2. 24.000-26.000: Second source\n')
    vocal = reference_vocal(uid)

    def forbid_production(*args, **kwargs):
        raise AssertionError('参考试听不得生成切片、启动制作或扫描配音版本')

    monkeypatch.setattr(workbench, '_sample_lines', forbid_production)
    monkeypatch.setattr(workbench.runner, 'start_step', forbid_production)
    monkeypatch.setattr(step_tts, '_cut', forbid_production)
    before = sorted(str(path.relative_to(vocal.parent.parent)) for path in vocal.parent.parent.rglob('*'))
    client = TestClient(app)
    response = client.get(f'/api/projects/{uid}/workbench/reference-audio', params={'line_id': 1})
    assert response.status_code == 200
    state = response.json()
    assert state == {'url': workbench._versioned_url(f'/api/projects/{uid}/workbench/audio/vocal', vocal),
                     'start': 1.85, 'end': 17.25, 'reference_start': 2., 'reference_end': 17., 'line_id': 1}
    audio = client.get(state['url'], headers={'Range': 'bytes=0-9'})
    assert audio.status_code == 206 and audio.content == vocal.read_bytes()[:10]
    assert audio.headers['content-range'].startswith('bytes 0-9/')
    assert client.head(state['url']).status_code == 200
    limited = client.get(f'/api/projects/{uid}/workbench/reference-audio', params={
        'line_id': 1, 'max_seconds': 5}).json()
    assert limited['start'] == 1.85 and limited['end'] == 7.25
    assert limited['reference_start'] == 2. and limited['reference_end'] == 7.
    short = client.get(f'/api/projects/{uid}/workbench/reference-audio', params={
        'line_id': 2, 'max_seconds': 15}).json()
    assert short['start'] == 23.85 and short['end'] == 26.25
    assert short['reference_start'] == 24. and short['reference_end'] == 26.
    assert sorted(str(path.relative_to(vocal.parent.parent)) for path in vocal.parent.parent.rglob('*')) == before


@pytest.mark.parametrize('text,line_id,expected', [
    ('1. 0.000-0.440: China!\n2. 0.900-2.400: History\n', 1, (0., .69, 0., .44)),
    ('1. 2.000-4.000: First\n2. 4.000-6.000: Second\n', 2, (3.85, 6.25, 4., 6.)),
    ('1. 26.000-28.000: Last\n', 1, (25.85, 28., 26., 28.)),
    ('1. 27.000-30.000: Longer than track\n', 1, (26.85, 28., 27., 28.)),
])
def test_reference_audio_context_preserves_word_tails_even_between_touching_sentences(make_project, text, line_id, expected):
    uid = make_project()
    source(uid, text)
    reference_vocal(uid)
    response = TestClient(app).get(f'/api/projects/{uid}/workbench/reference-audio', params={'line_id': line_id})
    assert response.status_code == 200
    state = response.json()
    assert tuple(state[key] for key in ('start', 'end', 'reference_start', 'reference_end')) == expected


def test_reference_audio_rejects_sentence_beyond_track_instead_of_empty_preview(make_project):
    uid = make_project()
    source(uid, '1. 29.000-30.000: Outside\n')
    reference_vocal(uid)
    response = TestClient(app).get(f'/api/projects/{uid}/workbench/reference-audio', params={'line_id': 1})
    assert response.status_code == 400
    assert response.json()['code'] == 'REFERENCE_AUDIO_RANGE'


def test_reference_audio_checks_done_voice_fingerprint_and_refreshes_media_version(make_project):
    uid = make_project()
    source(uid, '1. 2.000-4.000: Source\n')
    vocal = reference_vocal(uid)
    client = TestClient(app)
    endpoint = f'/api/projects/{uid}/workbench/reference-audio'
    first = client.get(endpoint, params={'line_id': 1}).json()
    atomic_write_bytes(vocal, vocal.read_bytes() + b'\x00\x00')
    second = client.get(endpoint, params={'line_id': 1}).json()
    assert second['url'] != first['url']
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'preprocess')
    step.input_fingerprint = 'outdated-input-fingerprint'
    crud.save_step(step)
    response = client.get(endpoint, params={'line_id': 1})
    assert response.status_code == 400 and response.json()['code'] == 'REFERENCE_AUDIO_STALE'


@pytest.mark.parametrize('status', ['stale', 'running', 'queued', 'failed'])
def test_reference_audio_rejects_outdated_or_updating_voice(make_project, status):
    uid = make_project()
    source(uid, '1. 2.000-4.000: Source\n')
    reference_vocal(uid)
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'preprocess')
    step.status = status
    crud.save_step(step)
    response = TestClient(app).get(f'/api/projects/{uid}/workbench/reference-audio', params={'line_id': 1})
    assert response.status_code == 400
    assert response.json()['code'] == 'REFERENCE_AUDIO_STALE'
    assert '音轨分离' in response.json()['message']


def test_reference_audio_rejects_missing_project_source_sentence_and_voice(make_project):
    client = TestClient(app)
    assert client.get('/api/projects/nonexistent/workbench/reference-audio', params={
        'line_id': 1}).json()['code'] == 'PROJECT_NOT_FOUND'
    uid = make_project()
    assert client.get(f'/api/projects/{uid}/workbench/reference-audio', params={
        'line_id': 1}).json()['code'] == 'DEPS_NOT_MET'
    source(uid, '1. 2.000-4.000: Source\n')
    assert client.get(f'/api/projects/{uid}/workbench/reference-audio', params={
        'line_id': 2}).json()['code'] == 'LINE_NOT_FOUND'
    response = client.get(f'/api/projects/{uid}/workbench/reference-audio', params={'line_id': 1})
    assert response.status_code == 400 and response.json()['code'] == 'REF_MISSING'


@pytest.mark.parametrize('query', [
    {}, {'line_id': 0}, {'line_id': -1}, {'line_id': 1.5}, {'line_id': 'bad'},
    {'line_id': 1, 'max_seconds': 0.49}, {'line_id': 1, 'max_seconds': 15.01},
    {'line_id': 1, 'max_seconds': 'nan'}, {'line_id': 1, 'max_seconds': 'inf'},
])
def test_reference_audio_validates_sentence_and_preview_limit(make_project, query):
    uid = make_project()
    response = TestClient(app).get(f'/api/projects/{uid}/workbench/reference-audio', params=query)
    assert response.status_code == 422
