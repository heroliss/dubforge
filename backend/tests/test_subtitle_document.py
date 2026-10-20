"""统一校对稿：编辑、导出、音频版本与成片时间轴的真实接口回归。"""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import synthesize, wait_status
from test_dubbing_preparation import prepared
from dubforge import config
from dubforge.domain import crud
from dubforge.pipeline import dubbing_session, line_settings
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint, runner
from dubforge.utils.fs import atomic_write_bytes


def state(client, uid):
    return client.get(f'/api/projects/{uid}/workbench?probe=false').json()


def edit(client, uid, values, line_id=1, **body):
    line = next(l for l in state(client, uid)['lines'] if l['id'] == line_id)
    return client.put(f'/api/projects/{uid}/workbench/lines/{line_id}/text', json={
        'revision': line['edit_revision'], 'values': values, **body})


def export(uid):
    runner.start_step(uid, 'subtitles')
    wait_status(uid, 'subtitles', {'done'})
    return crud.project_dir(uid) / 'srt'


def replace_source(uid, key, text):
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / 'artifacts' / key / f'{key}.txt'
    atomic_write_bytes(path, text.encode())
    crud.add_artifact(project.id, key, 'text', path.name, path, str(path.relative_to(root)))


def test_edit_export_and_reset_without_generating_audio(prepared):
    from app.main import app
    uid, project, calls = prepared
    root = crud.project_dir(uid)
    original = {key: (root / f'artifacts/{key}/{key}.txt').read_bytes() for key in ('split', 'translate')}
    before = compute_fingerprint(project, 'translate')
    with TestClient(app) as client:
        assert edit(client, uid, {'source_text': 'Hello again', 'text': '又见面了', 'start': .4, 'end': .9}).status_code == 200
        folder = export(uid)
        assert '00:00:00,400 --> 00:00:00,900' in (folder / 'source.srt').read_text(encoding='utf-8-sig')
        assert '又见面了\nHello again' in (folder / 'bilingual.srt').read_text(encoding='utf-8-sig')
        assert '又见面了' in (folder / 'trans.srt').read_text(encoding='utf-8-sig')
        current = state(client, uid)
        assert current['subtitles']['fresh'] and len(current['subtitles']['files']) == 3
        assert all(client.get(f['url']).status_code == 200 for f in current['subtitles']['files'])
        assert not calls and crud.get_step(project.id, 'tts').status == 'pending'
        assert edit(client, uid, {}, reset=True).status_code == 200
        assert not state(client, uid)['subtitles']['fresh']
        export(uid)
        assert '你好\nHello' in (folder / 'bilingual.srt').read_text(encoding='utf-8-sig')
    assert compute_fingerprint(crud.get_project(uid), 'translate') == before
    assert all((root / f'artifacts/{key}/{key}.txt').read_bytes() == raw for key, raw in original.items())


def test_source_only_edit_export_then_translation_arrives(prepared):
    from app.main import app
    from dubforge import configuration
    uid, project, calls = prepared
    (crud.project_dir(uid) / 'artifacts/translate/translate.txt').unlink()
    settings = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=settings['revision'], updates={'subtitles.mode': 'source'}))
    with TestClient(app) as client:
        rows = state(client, uid)['lines']
        assert len(rows) == 2 and not rows[0]['translation_available']
        assert edit(client, uid, {'source_text': 'Hello corrected', 'end': 2.5}).status_code == 200
        assert edit(client, uid, {'text': '不应写入'}).json()['code'] == 'DEPS_NOT_MET'
        folder = export(uid)
        assert 'Hello corrected' in (folder / 'source.srt').read_text(encoding='utf-8-sig')
        assert not (folder / 'trans.srt').exists() and not calls
        replace_source(uid, 'translate', '1. 新翻译\n2. 世界\n')
        row = state(client, uid)['lines'][0]
        assert row['source_text'] == 'Hello corrected' and row['text'] == '新翻译'
        assert row['end'] == 2.5 and not row['edit_stale']


def test_retime_preserves_audio_settings_and_confirmation_but_translation_expires_only_one(prepared):
    from app.main import app
    uid, _, calls = prepared
    base = f'/api/projects/{uid}/workbench'
    with TestClient(app) as client:
        for id in (1, 2):
            assert synthesize(client, f'{base}/lines/{id}/synthesize', json={}).status_code == 200
        assert client.post(base + '/confirm', json={'line_ids': [1, 2]}).status_code == 200
        project = crud.get_project(uid)
        line = Dependencies(project).dubbing_lines()[0]
        params = crud.project_params(project)
        params['tts_lines'] = {'1': {'identity': line_settings.identity(line), 'settings': {'tts.indextts25.duration_factor': .9}}}
        project.params = json.dumps(params); crud.save_project(project)
        assert edit(client, uid, {'start': .5, 'end': 2.5}).status_code == 200
        current = state(client, uid)
        assert all(l['wav']['exists'] and l['confirmed'] for l in current['lines'])
        assert current['lines'][0]['selection']['take'] == 1 and current['lines'][0]['customized']
        assert edit(client, uid, {'text': '你好呀'}).status_code == 200
        current = state(client, uid)
        assert not current['lines'][0]['wav']['exists'] and not current['lines'][0]['confirmed']
        assert current['lines'][0]['customized'] and current['lines'][0]['selection']['take'] == 1
        assert current['lines'][1]['wav']['exists'] and current['lines'][1]['confirmed']
        assert (crud.project_dir(uid) / 'artifacts/tts/1.wav').exists()
        assert synthesize(client, base + '/lines/1/synthesize', json={}).status_code == 200
        assert calls == ['你好', '世界', '你好呀']


def test_legacy_audio_survives_manual_retime(prepared):
    from app.main import app
    uid, _, _ = prepared
    with TestClient(app) as client:
        assert synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={}).status_code == 200
        (crud.project_dir(uid) / 'artifacts/tts/1.take.json').unlink()
        assert edit(client, uid, {'start': .5, 'end': 2.5}).status_code == 200
        assert state(client, uid)['lines'][0]['wav']['exists']


def test_actual_mix_uses_edited_start_without_truncating_clip(prepared, monkeypatch):
    from app.main import app
    from dubforge import configuration
    from dubforge.pipeline.step_assemble import _mix_timeline
    from dubforge.utils import audio as au
    import soundfile as sf
    uid, _, _ = prepared
    settings = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=settings['revision'], updates={'assemble.loudnorm': False}))
    with TestClient(app) as client:
        base = f'/api/projects/{uid}/workbench'
        assert synthesize(client, base + '/lines/1/synthesize', json={}).status_code == 200
        assert client.put(base + '/lines/2/selection', json={'source': 'dub', 'take': None}).status_code == 200
        assert edit(client, uid, {'start': .5, 'end': .9}).status_code == 200
        deps = Dependencies(crud.get_project(uid))
        monkeypatch.setattr(au, 'load_mono_32k', lambda path: np.ones(au.SR, dtype=np.float32))
        class Context:
            project = deps.project
            upstream = deps.upstream
            cfg = deps.cfg
            def check_cancel(self): pass
            def log(self, message): pass
            def register_file(self, *args): pass
        audio, sr = sf.read(_mix_timeline(Context(), crud.project_dir(uid), 6))
        assert audio[int(.3 * sr)] == 0 and audio[int(.7 * sr)] > 0
        assert audio[int(1.3 * sr)] > 0 and audio[int(1.7 * sr)] == 0


@pytest.mark.parametrize('values', [
    {'source_text': ''}, {'text': '  '}, {'start': -1}, {'end': 0},
    {'start': True}, {'end': '3'}, {'start': None}, {'end': 3.1}, {'unknown': 1},
])
def test_invalid_edit_does_not_write(prepared, values):
    from app.main import app
    uid, project, _ = prepared
    before = project.params
    with TestClient(app) as client:
        assert edit(client, uid, values).json()['code'] == 'SUBTITLE_EDIT_INVALID'
    assert crud.get_project(uid).params == before


def test_conflict_and_active_synthesis_block_edit(prepared):
    from app.main import app
    uid, project, _ = prepared
    with TestClient(app) as client:
        revision = state(client, uid)['lines'][0]['edit_revision']
        assert edit(client, uid, {'text': '改过了'}).status_code == 200
        assert edit(client, uid, {'text': '旧页面'}, revision=revision).json()['code'] == 'SUBTITLE_EDIT_CONFLICT'
        with dubbing_session.use(project, 2):
            assert edit(client, uid, {'text': '不能覆盖'}).json()['code'] == 'CONFIGURATION_BUSY'
        assert state(client, uid)['lines'][0]['text'] == '改过了'


def test_upstream_conflict_requires_review_and_preserves_manual_text(prepared):
    from app.main import app
    from dubforge.errors import DubForgeError
    from dubforge.pipeline.subtitle_document import read
    uid, _, _ = prepared
    with TestClient(app) as client:
        assert edit(client, uid, {'text': '我的译文', 'end': 2.5}).status_code == 200
        replace_source(uid, 'split', '1. 0.000-2.000: Hello changed\n2. 3.000-5.000: World\n')
        row = state(client, uid)['lines'][0]
        assert row['edit_stale'] and row['saved_edit']['text'] == '我的译文'
        with pytest.raises(DubForgeError, match='核对'):
            read(Dependencies(crud.get_project(uid)))
        assert edit(client, uid, row['saved_edit']).status_code == 200
        row = state(client, uid)['lines'][0]
        assert not row['edit_stale'] and row['text'] == '我的译文' and row['end'] == 2.5
        replace_source(uid, 'translate', '1. 模型重新翻译\n2. 世界\n')
        assert state(client, uid)['lines'][0]['edit_stale']
        assert edit(client, uid, {}, reset=True).status_code == 200
        assert state(client, uid)['lines'][0]['text'] == '模型重新翻译'


def test_orphaned_edits_are_visible_and_can_be_removed(prepared):
    from app.main import app
    uid, _, _ = prepared
    with TestClient(app) as client:
        assert edit(client, uid, {'text': '第二句修改'}, line_id=2).status_code == 200
        replace_source(uid, 'split', '1. 0.000-2.000: Hello\n')
        replace_source(uid, 'translate', '1. 你好\n')
        assert state(client, uid)['orphaned_edits'] == ['2']
        assert client.post(f'/api/projects/{uid}/workbench/discard_orphaned_edits').status_code == 200
        assert not state(client, uid)['orphaned_edits']
        export(uid)


def test_source_edit_does_not_invalidate_voice_or_translation_and_expires_legacy_export(prepared):
    from app.main import app
    uid, project, _ = prepared
    before = {k: compute_fingerprint(project, k) for k in ('tts', 'translate', 'assemble')}
    with TestClient(app) as client:
        export(uid)
        step = crud.get_step(project.id, 'subtitles')
        step.input_fingerprint = ''; crud.save_step(step)
        assert edit(client, uid, {'source_text': 'Corrected source'}).status_code == 200
        assert not state(client, uid)['subtitles']['fresh']
        after = crud.get_project(uid)
        assert all(compute_fingerprint(after, k) == before[k] for k in ('tts', 'translate'))
        # 原文编辑改变发布审查输入，因此成片必须重新核对；不重新生成声音和翻译。
        assert compute_fingerprint(after, 'assemble') != before['assemble']
