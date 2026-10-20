"""文字/参数待应用统一展示，朗读与字幕各自失效，不自动覆盖声音。"""
import json

import pytest

from conftest import synthesize
from test_dubbing_preparation import index_prepared, prepared
from test_subtitle_document import edit
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, pronunciation
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_tts import line_fingerprint, line_cache_valid
from dubforge.runner import compute_fingerprint
from dubforge.utils.fs import atomic_write_bytes


def state(client, uid):
    return client.get(f'/api/projects/{uid}/workbench?probe=false').json()


def generate(client, uid):
    assert synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={}).status_code == 200


def test_changed_caption_marks_existing_stale_voice_pending_without_reenabling_it(index_prepared):
    client, uid, _ = index_prepared
    generate(client, uid)
    path = dubbing_edit.take_path(crud.project_dir(uid), 1)
    old = path.read_bytes()
    assert edit(client, uid, {'text': '您好，新的朗读内容'}).status_code == 200
    row = state(client, uid)['lines'][0]
    assert row['wav']['file_exists'] and not row['wav']['exists'] and row['wav']['stale']
    assert row['wav']['changes_pending'] and '朗读文字已修改' in row['wav']['change_reasons']
    assert not row['wav']['settings_changed'] and not row['confirmed']
    assert path.read_bytes() == old


@pytest.mark.parametrize('reading', ['你好', '你，好', '这是固定的另一句朗读。'])
@pytest.mark.parametrize('legacy', [True, False])
def test_fixed_reading_caption_edit_reuses_actual_voice_and_stales_assembly(index_prepared, reading, legacy):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {pronunciation.PATH: reading}}).status_code == 200
    generate(client, uid)
    project = crud.get_project(uid)
    deps = Dependencies(project)
    line = deps.dubbing_lines()[0]
    root = deps.root
    if legacy:
        # 模拟旧算法与未记录原稿的历史声音，必须严格回算其旧输入后才复用。
        fingerprint = line_fingerprint(project, line, dependencies=deps, _legacy_reading=True)
        record = dubbing_edit.read_take(root, line, 1)
        record['fingerprint'] = fingerprint
        record.pop('line_snapshot', None)
        record.pop('spoken_text', None)
        atomic_write_bytes(dubbing_edit.take_path(root, 1).with_suffix('.input'), fingerprint.encode())
        atomic_write_bytes(dubbing_edit.take_path(root, 1).with_suffix('.take.json'), json.dumps(record, ensure_ascii=False).encode())
    before = compute_fingerprint(project, 'assemble')
    assert edit(client, uid, {'text': '仅改变字幕显示内容'}).status_code == 200
    project = crud.get_project(uid)
    deps = Dependencies(project)
    line = deps.dubbing_lines()[0]
    assert dubbing_edit.playable(project, line, root, 1, dependencies=deps)
    assert line_cache_valid(project, line, root, dependencies=deps)
    assert 1 not in deps.dubbing_summary()['pending_ids']
    row = state(client, uid)['lines'][0]
    assert row['wav']['exists'] and not row['wav']['stale']
    assert not row['wav']['changes_pending'] and not row['wav']['settings_changed']
    assert row['wav']['change_reasons'] == [] and row['wav']['reading_caption_independent']
    assert compute_fingerprint(project, 'assemble') != before
    # 明确恢复跟随字幕才改变实际模型输入；不能继续用旧朗读冒充新字幕声音。
    assert client.put(base + '/lines/1/params', json={'reset': [pronunciation.PATH]}).status_code == 200
    row = state(client, uid)['lines'][0]
    assert row['wav']['changes_pending'] and not row['wav']['exists']
    assert not row['wav']['reading_caption_independent']


def test_reading_and_voice_parameter_changes_share_pending_state_and_keep_old_audio(index_prepared):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    generate(client, uid)
    path = dubbing_edit.take_path(crud.project_dir(uid), 1)
    old = path.read_bytes()
    assert client.put(base + '/lines/1/params', json={'updates': {'tts.indextts25.temperature': .4}}).status_code == 200
    wav = state(client, uid)['lines'][0]['wav']
    assert wav['exists'] and wav['changes_pending'] and wav['settings_changed']
    assert '配音设置已修改' in wav['change_reasons']
    assert client.put(base + '/lines/1/params', json={'updates': {pronunciation.PATH: '全新的实际朗读。'}}).status_code == 200
    wav = state(client, uid)['lines'][0]['wav']
    assert wav['exists'] and wav['changes_pending'] and '朗读文字已修改' in wav['change_reasons']
    assert path.read_bytes() == old


def test_reference_file_change_has_its_own_pending_reason(index_prepared):
    client, uid, _ = index_prepared
    generate(client, uid)
    ref = crud.project_dir(uid) / 'reference.wav'
    atomic_write_bytes(ref, ref.read_bytes() + b'new-reference-version')
    wav = state(client, uid)['lines'][0]['wav']
    assert wav['exists'] and wav['changes_pending']
    assert '参考音频设置或文件已变化' in wav['change_reasons']


def test_source_caption_only_changes_rendering_not_spoken_voice(index_prepared):
    client, uid, _ = index_prepared
    generate(client, uid)
    assert edit(client, uid, {'source_text': 'A corrected English subtitle'}).status_code == 200
    wav = state(client, uid)['lines'][0]['wav']
    assert wav['exists'] and not wav['changes_pending'] and not wav['reading_caption_independent']


def test_manual_display_retime_preserves_tts_clock_and_does_not_mark_pending(index_prepared):
    client, uid, _ = index_prepared
    generate(client, uid)
    assert edit(client, uid, {'start': .4, 'end': 2.5}).status_code == 200
    project = crud.get_project(uid)
    deps = Dependencies(project)
    line = deps.dubbing_lines()[0]
    assert 'voice_timing' in line and line_cache_valid(project, line, deps.root, dependencies=deps)
    wav = state(client, uid)['lines'][0]['wav']
    assert wav['exists'] and not wav['changes_pending'] and not wav['settings_changed']


def test_dynamic_reference_reuses_original_clock_after_display_retime(tmp_path, monkeypatch):
    from dubforge.pipeline import step_tts
    cuts, resolved = [], {}
    monkeypatch.setattr(step_tts, '_reference_fingerprint', lambda *args: 'reference-version')
    def cut(source, target, start, duration):
        cuts.append((start, duration))
        return target
    monkeypatch.setattr(step_tts, '_cut', cut)
    row = dict(id=1, text='仅调整显示位置', start=.4, end=2.5, voice_timing=[0., 2.])
    step_tts._resolve_ref(None, 'dynamic', '', row, tmp_path / 'vocal.wav', tmp_path,
                          'timbre', resolved=resolved)
    assert cuts == [(0., 2.)]
    assert resolved['timbre']['start'] == 0 and resolved['timbre']['duration'] == 2
