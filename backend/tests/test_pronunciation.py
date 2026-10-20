"""独立朗读只改变模型输入；隔离项目与模型替身覆盖缓存、版本和运行队列。"""
import json
from threading import Event

import pytest

from conftest import synthesize
from test_dubbing_preparation import index_prepared, prepared
from test_dubbing_tasks import finished
from test_timing_repair import voices, rewrite_record
from test_subtitle_document import edit
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, dubbing_policy, line_settings, pronunciation, timing_repair
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA
from dubforge.pipeline.step_tts import line_fingerprint, line_cache_valid
from dubforge.runner import runner

PATH = pronunciation.PATH


@pytest.mark.parametrize(('text', 'value', 'expected'), [
    ('19条：公元前2070年之前', '19条，公元前2070年之前', '19条，公元前2070年之前'),
    ('接下来的13年', '接下来的十三年', '接下来的十三年'),
    ('你好', '这是一句完全不同的朗读内容。', '这是一句完全不同的朗读内容。'),
    ('没有朝', '没有<朝|chao2>', '没有<朝|chao2>'),
    ('银行', '<银行|YIN2 HANG2>', '<银行|YIN2 HANG2>'),
    ('女声', '<女|nü3>声', '<女|nü3>声'),
    ('你好', '', ''), ('你好', ' \n\t ', ''), ('你好', ' 你好 \n', '你好'),
    ('你好', '  第一行\n第二行  \t内容  ', '第一行\n第二行  \t内容'),
    ('没有朝', '没有＜朝|CHAO2＞', '没有＜朝|CHAO2＞'),
    ('你好', '<break time="300ms"/>', '<break time="300ms"/>'),
    ('你好', '[pause] Hello <world|W ER L D>。', '[pause] Hello <world|W ER L D>。'),
    ('朝代 a < b，a|b 与 <div>', '<朝|CHAO2>代 a < b，a|b 与 <div>',
     '<朝|CHAO2>代 a < b，a|b 与 <div>'),
])
def test_arbitrary_reading_is_preserved_without_template_or_subtitle_checks(text, value, expected):
    assert pronunciation.validate(value, text) == expected


@pytest.mark.parametrize('value', [None, 1, [], {}, True])
def test_only_non_text_values_are_rejected(value):
    with pytest.raises(pronunciation.PronunciationError) as exc:
        pronunciation.validate(value, '没有朝')
    assert exc.value.code == 'TTS_PRONUNCIATION_INVALID'


def test_schema_is_only_per_sentence_and_empty_keeps_historical_hash(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    before = line_fingerprint(project, line)
    assert not any(f.path == PATH for fields in STEP_PARAM_SCHEMA.values() for f in fields)
    state = client.get(f'/api/projects/{uid}/workbench/lines/1/params').json()
    field = next(f for f in state['steps']['tts']['fields'] if f['path'] == PATH)
    assert field['label'] == '朗读文本' and field['type'] == 'text' and field['group'] == '朗读内容'
    assert state['line_text'] == '你好' and state['defaults'][PATH] == ''
    assert state['steps']['tts']['values'][PATH] == '' and not state['overridden'][PATH]
    for value in ('', '你好'):
        assert client.put(f'/api/projects/{uid}/workbench/lines/1/params',
            json={'updates': {PATH: value}}).status_code == 200
        assert line_fingerprint(crud.get_project(uid), line) == before
    # 主动载入与译文相同的文本仍是独立设置，而不是被悄悄恢复为跟随。
    state = client.get(f'/api/projects/{uid}/workbench/lines/1/params').json()
    assert state['steps']['tts']['values'][PATH] == '你好' and state['overridden'][PATH]
    assert client.put(f'/api/projects/{uid}/step_params', json={'updates': {PATH: '<你|NI3>好'}}).status_code == 400


def test_legacy_canonical_reading_keeps_existing_cache(index_prepared):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {PATH: '<你|NI3>好'}}).status_code == 200
    assert synthesize(client, base + '/synthesize', json={'line_ids': [1]}).status_code == 200
    root = crud.project_dir(uid)
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    before = (root / 'artifacts/tts/1.input').read_text()
    assert client.put(base + '/lines/1/params', json={'updates': {PATH: '<你|NI3>好'}}).status_code == 200
    project = crud.get_project(uid)
    assert line_fingerprint(project, line) == before
    assert line_cache_valid(project, line, root)
    assert line_settings.overrides(project, line)[PATH] == '<你|NI3>好'


def test_invalid_transport_type_does_not_replace_saved_reading(index_prepared):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench/lines/1/params'
    assert client.put(base, json={'updates': {PATH: '你，好'}}).status_code == 200
    result = client.put(base, json={'updates': {PATH: ['不是文本']}})
    assert result.status_code == 400 and result.json()['code'] == 'TTS_PRONUNCIATION_INVALID'
    assert '<朝|' not in result.json()['message']
    assert client.get(base).json()['steps']['tts']['values'][PATH] == '你，好'


@pytest.mark.parametrize('reading', ['你，好', '接下来的十三年。', '<你|NI3>好'])
def test_reading_changes_only_one_model_input_and_preserves_old_slot_and_subtitles(index_prepared, monkeypatch, reading):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    root = crud.project_dir(uid)
    assert synthesize(client, base + '/synthesize', json={'line_ids': [1, 2]}).status_code == 200
    assert client.post(base + '/lines/1/confirm', json={'confirmed': True}).status_code == 200
    old_wav = (root / 'artifacts/tts/1.wav').read_bytes()
    translate = (root / 'artifacts/translate/translate.txt').read_bytes()
    project = crud.get_project(uid)
    lines = Dependencies(project).dubbing_lines()
    hashes = [line_fingerprint(project, row) for row in lines]
    result = client.put(base + '/lines/1/params', json={'updates': {PATH: reading}})
    assert result.status_code == 200
    project = crud.get_project(uid)
    assert line_fingerprint(project, lines[0]) != hashes[0]
    assert line_fingerprint(project, lines[1]) == hashes[1]
    assert not line_cache_valid(project, lines[0], root)
    state = client.get(base + '?probe=false').json()
    assert state['lines'][0]['confirmed'] and state['lines'][0]['wav']['exists']
    calls, original = [], tts_indextts25.synth
    def synth(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    assert synthesize(client, base + '/lines/1/synthesize', json={'take': None}).status_code == 200
    assert calls == [reading]
    assert (root / 'artifacts/tts/1.wav').read_bytes() == old_wav
    assert (root / 'artifacts/translate/translate.txt').read_bytes() == translate
    take = dubbing_edit.read_take(root, lines[0], 2)
    assert take['settings']['pronunciation_text'] == reading
    assert dubbing_edit.playable(crud.get_project(uid), lines[0], root, 1)
    assert line_cache_valid(crud.get_project(uid), lines[0], root, take=2)


def test_project_voice_policy_preserves_pronunciation_but_not_voice_overrides(index_prepared):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert client.put(base + '/lines/1/params', json={'updates': {
        PATH: '<你|NI3>好', 'tts.indextts25.duration_factor': .7}}).status_code == 200
    project = crud.get_project(uid)
    policy = dubbing_policy.normalize({'mode': 'all', 'line_settings': 'project'})
    synthetic = dubbing_policy.synthesis_project(project, policy)
    line = Dependencies(project).dubbing_lines()[0]
    assert line_settings.overrides(synthetic, line) == {PATH: '<你|NI3>好'}
    assert line_settings.overrides(project, line)['tts.indextts25.duration_factor'] == .7


def test_timing_repair_replays_actual_take_reading_not_current_reading(voices):
    uid, root, lines = voices
    project = crud.get_project(uid)
    row = lines[0]
    record = dubbing_edit.read_take(root, row, 1)
    record['settings']['pronunciation_text'] = '原来实际朗读的整句话。'
    rewrite_record(root, row, settings=record['settings'])
    line_settings.save(project, row, {PATH: '新指定的另一句话。'}, [], Dependencies(project).cfg)
    project = crud.get_project(uid)
    synthetic, cfg = timing_repair.context(project, row, root, 1, Dependencies(project).cfg)
    effective = line_settings.effective_config(synthetic, row, cfg)
    assert pronunciation.spoken_text(row, effective) == '原来实际朗读的整句话。'
    record['settings'].pop('pronunciation_text')
    rewrite_record(root, row, settings=record['settings'])
    synthetic, cfg = timing_repair.context(project, row, root, 1, Dependencies(project).cfg)
    assert pronunciation.spoken_text(row, line_settings.effective_config(synthetic, row, cfg)) == row['text']


@pytest.mark.parametrize('reading', ['<你|NI3>好', '你好', '另一句朗读内容。'])
def test_manual_translation_edit_preserves_independent_reading_and_voice_settings(index_prepared, reading):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench/lines/1/params'
    assert client.put(base, json={'updates': {PATH: reading,
        'tts.indextts25.duration_factor': .8}}).status_code == 200
    assert edit(client, uid, {'start': .2}).status_code == 200
    assert client.get(base).json()['steps']['tts']['values'][PATH] == reading
    assert edit(client, uid, {'text': '您好'}).status_code == 200
    params = client.get(base).json()
    assert params['steps']['tts']['values'][PATH] == reading and params['overridden'][PATH]
    assert params['steps']['tts']['values']['tts.indextts25.duration_factor'] == .8
    line = Dependencies(crud.get_project(uid)).dubbing_lines()[0]
    effective = line_settings.effective_config(crud.get_project(uid), line, Dependencies(crud.get_project(uid)).cfg)
    assert line['text'] == '您好' and pronunciation.spoken_text(line, effective) == reading
    # 明确清空才恢复跟随当前译文，其他声音参数不受影响。
    assert client.put(base, json={'updates': {PATH: ' \n\t '}}).status_code == 200
    params = client.get(base).json()
    assert params['steps']['tts']['values'][PATH] == '' and not params['overridden'][PATH]
    assert params['steps']['tts']['values']['tts.indextts25.duration_factor'] == .8
    effective = line_settings.effective_config(crud.get_project(uid), line, Dependencies(crud.get_project(uid)).cfg)
    assert pronunciation.spoken_text(line, effective) == '您好'


def test_unmatched_upstream_identity_does_not_retarget_custom_reading(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    assert client.put(f'/api/projects/{uid}/workbench/lines/1/params',
        json={'updates': {PATH: '仅适用于原句'}}).status_code == 200
    project = crud.get_project(uid)
    assert line_settings.overrides(project, line)[PATH] == '仅适用于原句'
    for changes in ({'text': '上游新译文'}, {'start': .1}, {'id': 99}):
        assert line_settings.overrides(project, {**line, **changes}) == {}


@pytest.mark.parametrize('reset', [True, False])
def test_reset_or_empty_reading_uses_translation_and_reuses_original_audio(index_prepared, reset):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    root = crud.project_dir(uid)
    assert synthesize(client, base + '/synthesize', json={'line_ids': [1]}).status_code == 200
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    before = line_fingerprint(project, line)
    assert client.put(base + '/lines/1/params', json={'updates': {PATH: '不同朗读'}}).status_code == 200
    assert not line_cache_valid(crud.get_project(uid), line, root)
    body = {'reset': [PATH]} if reset else {'updates': {PATH: ''}}
    assert client.put(base + '/lines/1/params', json=body).status_code == 200
    project = crud.get_project(uid)
    assert line_fingerprint(project, line) == before and line_cache_valid(project, line, root)


def test_running_queue_updates_selected_slot_after_frozen_request(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    calls, start, release = [], Event(), Event()
    original = tts_indextts25.synth
    def synth(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            start.set()
            assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    project = crud.get_project(uid)
    runner.start_dubbing(uid, [1, 2])
    try:
        assert start.wait(10)
        result = client.put(base + '/lines/1/params', json={'updates': {PATH: '你，好'}, 'revision': 0})
        assert result.status_code == 200 and result.json()['execution']['state'] == 'queued'
        assert calls == ['你好']
    finally:
        release.set()
    assert finished(project).status == 'done'
    assert calls == ['你好', '你，好', '世界']
    state = client.get(base + '?probe=false').json()
    assert state['lines'][0]['selection']['take'] == 1
    assert not state['lines'][0]['wav'].get('settings_changed')
