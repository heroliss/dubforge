"""句子与成片实际输入对照；参数待配音不伪称成片声音已改变。"""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as av, assembly_sentence_changes as changes
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from test_assemble import _prepare_outputs_project
from test_assembly_versions import wait
from test_configuration import save
from test_subtitle_document import edit
from test_dubbing_tasks import finished


@pytest.fixture
def baseline(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    runner.start_step(uid, 'tts')
    assert finished(crud.get_project(uid)).status == 'done'
    definitions = [dict(id='translated', name='中文字幕基准', dubbing=False, burn_subtitles=True,
                       include_comments=False, overrides={'assemble.subtitle_track': 'trans'}),
                   dict(id='source', name='原文字幕基准', dubbing=False, burn_subtitles=True,
                       include_comments=False, overrides={'assemble.subtitle_track': 'source'}),
                   dict(id='voice', name='配音基准', dubbing=True, burn_subtitles=True,
                       include_comments=False, overrides={'assemble.subtitle_track': 'trans'})]
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=definitions))
    request = dict(version_ids=[v['id'] for v in definitions])
    request['fingerprint'] = av.plan(uid, request)['fingerprint']
    runner.start_versions(uid, request)
    wait(uid)
    with runner._lock:
        futures = list(runner._futures)
    for future in futures:
        future.result(timeout=30)
    return TestClient(app), uid, root, definitions


def rows(result):
    return {r['id']: r for r in result['sentences']}


def test_success_saves_bound_snapshot_and_unchanged_api_defaults_to_named_baseline(baseline, monkeypatch):
    client, uid, _, _ = baseline
    stored = av.results(uid)['voice']
    assert json.loads(stored.snapshot)['sentence_inputs']['schema'] == 1
    monkeypatch.setattr(hashlib, 'file_digest', lambda *a, **kw: pytest.fail('查询不能重新hash媒体'))
    result = client.get(f'/api/projects/{uid}/assembly-versions/sentence-changes').json()
    assert result['baseline']['name'] == '配音基准'
    assert result['baseline']['artifact_id'] == stored.artifact_id
    assert result['baseline']['artifact_hash'] == stored.artifact_hash
    assert result['baseline']['created_at']
    assert result['coverage'] == 'known' and result['summary']['changed'] == 0
    assert {r['status'] for r in result['sentences']} == {'unchanged'}


def test_regenerated_same_artifact_id_has_new_hash_and_becomes_latest_default(baseline):
    from datetime import datetime, timedelta, timezone
    from dubforge.domain.models import Artifact
    from dubforge.pipeline import assembly_partial
    _, uid, _, _ = baseline
    record = av.results(uid)['translated']
    previous = changes.view(uid, 'translated')['baseline']
    with crud.session() as session:
        artifact = session.get(Artifact, record.artifact_id)
        artifact.sha256 = 'updated-successful-output'
        artifact.created_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        stored = session.get(AssemblyResult, uid + ':translated')
        stored.artifact_hash = artifact.sha256
        session.add(artifact)
        session.add(stored)
        session.commit()
    result = changes.view(uid)
    assert result['baseline']['version_id'] == 'translated'
    assert result['baseline']['artifact_id'] == previous['artifact_id']
    assert result['baseline']['artifact_hash'] != previous['artifact_hash']
    assert changes.view(uid, 'voice')['baseline']['version_id'] == 'voice'
    partial = assembly_partial.context(uid)['baselines'][0]
    assert partial['version_id'] == 'translated'
    assert partial['artifact_hash'] == result['baseline']['artifact_hash']
    assert partial['created_at'] == result['baseline']['created_at']


def test_translation_change_is_only_visible_track_and_restore_removes_difference(baseline):
    client, uid, _, _ = baseline
    assert edit(client, uid, {'text': '修改后的中文字幕'}).status_code == 200
    result = changes.view(uid, 'translated')
    assert rows(result)[1]['change_kinds'] == ['subtitle']
    assert rows(result)[2]['status'] == 'unchanged'
    assert result['suggestion']['line_ids'] == [1] and result['suggestion']['can_partial']
    assert not result['suggestion']['replace_audio']
    source = changes.view(uid, 'source')
    assert source['summary']['changed'] == 0 and source['summary']['dubbing_pending'] == 0
    assert edit(client, uid, {}, reset=True).status_code == 200
    assert changes.view(uid, 'translated')['summary']['changed'] == 0


def test_original_text_change_only_affects_original_visible_track(baseline):
    client, uid, _, _ = baseline
    assert edit(client, uid, {'source_text': 'Corrected original sentence'}).status_code == 200
    assert changes.view(uid, 'translated')['summary']['changed'] == 0
    assert rows(changes.view(uid, 'source'))[1]['status'] == 'changed'


def test_global_style_change_is_not_suggested_as_sentence_only_fix(baseline):
    _, uid, _, _ = baseline
    save('project:' + uid, {'assemble.subtitle_color': '#00FF00'})
    result = changes.view(uid, 'translated')
    assert result['global_changes'] and not result['suggestion']['can_partial']
    assert '完整生成' in result['suggestion']['reason']


def test_legacy_missing_snapshot_is_unknown_without_backfilling(baseline):
    _, uid, _, _ = baseline
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        snapshot = json.loads(record.snapshot)
        snapshot.pop('sentence_inputs')
        record.snapshot = json.dumps(snapshot)
        session.add(record)
        session.commit()
    before = av.results(uid)['translated'].snapshot
    result = changes.view(uid, 'translated')
    assert result['coverage'] == 'unknown' and all(r['status'] == 'unknown' for r in result['sentences'])
    assert result['suggestion']['line_ids'] == [] and not result['suggestion']['can_partial']
    assert av.results(uid)['translated'].snapshot == before


def test_unapplied_tts_setting_is_pending_but_does_not_change_actual_assembly_audio(baseline):
    client, uid, _, _ = baseline
    save('project:' + uid, {'tts.indextts25.temperature': .321})
    result = changes.view(uid, 'voice')
    assert result['summary']['changed'] == 0 and rows(result)[1]['dubbing_pending']
    assert not rows(result)[1]['dubbing_blocking']
    assert not result['suggestion']['replace_audio']
    assert not result['needs_update'] and not result['whole_render_changed']
    assert result['full_rebuild_reasons'] == []
    assert '无需更新' in result['suggestion']['reason']
    # 有效旧声音仍可沿用；只改原文字幕时不应泛化阻止局部修订。
    assert edit(client, uid, {'source_text': 'New original subtitle'}).status_code == 200
    # voice仅显示译文，因此该变化也不影响其成片。
    assert changes.view(uid, 'voice')['summary']['changed'] == 0
    from dubforge.pipeline.pronunciation import PATH
    spoken = Dependencies(crud.get_project(uid)).dubbing_lines()[0]['text']
    assert client.put(f'/api/projects/{uid}/workbench/lines/1/params',
                      json={'updates': {PATH: spoken}}).status_code == 200
    assert edit(client, uid, {'text': '只更新显示的第一句'}).status_code == 200
    result = changes.view(uid, 'voice')
    assert rows(result)[1]['dubbing_pending'] and not rows(result)[1]['dubbing_blocking']
    assert rows(result)[1]['change_kinds'] == ['subtitle'] and result['suggestion']['can_partial']


def test_stale_spoken_text_blocks_automatic_silent_replacement(baseline):
    client, uid, _, _ = baseline
    assert edit(client, uid, {'text': '需要重新配音的全新文字'}).status_code == 200
    result = changes.view(uid, 'voice')
    assert rows(result)[1]['dubbing_blocking']
    assert not result['suggestion']['can_partial'] and result['suggestion']['blocked_line_ids'] == [1]


def test_partial_snapshot_preserves_unselected_base_sentences(baseline):
    client, uid, _, definitions = baseline
    from dubforge.pipeline import assembly_partial
    assert edit(client, uid, {'text': '局部修订后的第一句'}).status_code == 200
    request = dict(base_version_id='translated', line_ids=[1], replace_audio=False)
    request['fingerprint'] = assembly_partial.plan(uid, request)['fingerprint']
    child = assembly_partial.create(uid, request)['version']
    # 用户再改范围外第二句，局部成片仍必须保留旧基准第二句。
    assert edit(client, uid, {'text': '尚未纳入的第二句'}, line_id=2).status_code == 200
    deps = Dependencies(crud.get_project(uid))
    version = av.resolve(deps, child)
    snapshot = changes.completed(deps, version)
    previous = json.loads(av.results(uid)['translated'].snapshot)['sentence_inputs']
    assert snapshot['lines']['2']['subtitle'] == previous['lines']['2']['subtitle']
    assert snapshot['lines']['2']['document'] == previous['lines']['2']['document']
    assert snapshot['lines']['2']['document']['text'] != '尚未纳入的第二句'
    assert snapshot['lines']['1']['subtitle'][0]['text'] == '局部修订后的第一句'
    assert snapshot['lines']['1']['document']['text'] == '局部修订后的第一句'
    assert snapshot['coverage'] == 'known' and not snapshot['mixed_globals']


def test_legacy_partial_never_invents_complete_old_sentence_knowledge(baseline):
    client, uid, _, _ = baseline
    deps = Dependencies(crud.get_project(uid))
    version = av.resolve(deps, av.catalog(uid)['versions'][0])
    old = copy.deepcopy(version)
    version['partial'] = {'plan_id': 'a' * 32}
    plan = dict(base_version=old, ranges=[dict(start=.5, end=2)], replace_audio=False)
    from unittest.mock import patch
    with patch('dubforge.pipeline.assembly_partial.load_plan', return_value=plan):
        snapshot = changes.completed(deps, version)
    assert snapshot['coverage'] == 'partial' and all(not row['known'] for row in snapshot['lines'].values())
    old['sentence_inputs'] = snapshot
    with patch('dubforge.pipeline.assembly_partial.load_plan', return_value=plan):
        second = changes.completed(deps, version)
    assert second['coverage'] == 'partial' and all(not row['known'] for row in second['lines'].values())


def test_audio_redaction_mode_is_part_of_actual_global_contract(baseline):
    _, uid, _, definitions = baseline
    save('project:' + uid, {'content_review.blocked_ranges': '0.5-1.2:人工审查',
                          'content_review.audio_mode': 'voice_only'})
    deps = Dependencies(crud.get_project(uid))
    before = changes._globals(deps, av.resolve(deps, definitions[0]))
    save('project:' + uid, {'content_review.audio_mode': 'all_audio'})
    deps = Dependencies(crud.get_project(uid))
    after = changes._globals(deps, av.resolve(deps, definitions[0]))
    assert before['values'] == after['values']
    assert before['audio_redaction'] != after['audio_redaction']


def test_comment_contract_uses_same_reviewed_overlay_as_renderer(baseline):
    _, uid, _, definitions = baseline
    from test_content_review_publication_comments import _overlay, _item
    from dubforge.pipeline import content_redaction
    save('project:' + uid, {'content_review.blocked_terms': '屏蔽词'})
    overlay = _overlay([_item(id='safe'), _item(id='blocked', display_text='屏蔽词内容')])
    deps = Dependencies(crud.get_project(uid))
    definition = {**definitions[0], 'include_comments': True}
    version = av.resolve(deps, definition, comment_plan=overlay)
    rendered = {**version, 'comment_overlay': content_redaction.safe_overlay(deps.for_version(version), version['comment_overlay'])}
    assert len(rendered['comment_overlay']['items']) == 1
    assert changes._globals(deps, version)['comments'] == changes._globals(deps, rendered)['comments']


@pytest.mark.parametrize('setting', ['assemble.sentence_leveling', 'assemble.loudnorm', 'assemble.final_loudness_mode'])
def test_cross_sentence_audio_processing_binds_all_actual_voices_without_pcm_scan(baseline, monkeypatch, setting):
    _, uid, _, definitions = baseline
    save('project:' + uid, {setting: 'normalize' if setting.endswith('mode') else True})
    deps = Dependencies(crud.get_project(uid))
    version = av.resolve(deps, definitions[2])
    from dubforge.utils import audio
    monkeypatch.setattr(audio, 'load_mono_32k', lambda *a, **kw: pytest.fail('比较不应扫描PCM'))
    before = changes.capture(deps, version)
    media = changes._media
    def changed_media(dep, step, name):
        return dict(sha256='new-actual-voice', size=1) if step == 'tts' and name == '1.wav' else media(dep, step, name)
    monkeypatch.setattr(changes, '_media', changed_media)
    after = changes.capture(deps, version)
    assert before['global_inputs']['audio_mix_inputs'] != after['global_inputs']['audio_mix_inputs']
    assert before['lines']['2']['audio'] == after['lines']['2']['audio']


def _comment_baseline(uid, definitions):
    from dubforge import comment_studio
    from test_content_review_publication_comments import _overlay, _item
    overlay = _overlay([_item(id='first'), _item(id='second', display_text='第二条原有评论')])
    save('project:' + uid, {'comments.' + key: value for key, value in overlay['settings'].items()})
    definitions[0]['include_comments'] = True
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=definitions))
    project = crud.get_project(uid)
    comment_studio._write(project, dict(overlay=overlay))
    deps = Dependencies(project)
    version = av.resolve(deps, definitions[0], comment_plan=comment_studio.effective_overlay(uid, overlay))
    version['sentence_inputs'] = changes.capture(deps, version)
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        record.snapshot = json.dumps(version)
        session.add(record)
        session.commit()
    return project, overlay


def test_changes_alias_exposes_five_groups_and_original_text_before_after(baseline):
    client, uid, _, _ = baseline
    assert edit(client, uid, {'source_text': 'Corrected English words'}).status_code == 200
    result = client.get(f'/api/projects/{uid}/assembly-versions/changes?version_id=source').json()
    assert [g['kind'] for g in result['groups']] == ['subtitle', 'audio', 'comment', 'cuts', 'settings']
    entry = result['groups'][0]['changes'][0]
    assert entry['before']['source_text'] != entry['current']['source_text'] == 'Corrected English words'
    assert result['summary']['total_changes'] == 1 and not result['full_rebuild_reasons']
    assert result['sentences'][0]['before'] == entry['before']


def test_legacy_manual_candidates_are_pending_review_not_proven_assembly_changes(baseline):
    client, uid, _, _ = baseline
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':source')
        snapshot = json.loads(record.snapshot)
        snapshot.pop('sentence_inputs')
        record.snapshot = json.dumps(snapshot)
        session.add(record)
        session.commit()
    assert edit(client, uid, {'source_text': 'New manually corrected original'}).status_code == 200
    result = changes.view(uid, 'source')
    assert result['summary']['changed'] == 0 and result['groups'][0]['status'] == 'unknown'
    assert result['whole_render_changed'] and result['full_rebuild_reasons']
    assert result['sentences'][0]['before'] is None
    assert result['manual_candidates'][0]['id'] == 1
    assert result['manual_candidates'][0]['fields'] == ['source_text']
    assert result['manual_candidates'][0]['reason'] == '手工修改待核对'


def test_comment_only_update_uses_old_and_new_intervals_without_audio_or_sentence_ids(baseline):
    from dubforge import comment_studio
    _, uid, _, definitions = baseline
    project, overlay = _comment_baseline(uid, definitions)
    assert changes.view(uid, 'translated')['summary']['total_changes'] == 0
    overlay['items'][0]['display_text'] = '修改后的上屏评论'
    comment_studio._write(project, dict(overlay=overlay))
    result = changes.view(uid, 'translated')
    assert result['comments'][0]['before'][0]['parts'] == ['建筑很漂亮']
    assert result['comments'][0]['current'][0]['parts'] == ['修改后的上屏评论']
    assert result['groups'][2]['status'] == 'changed' and result['groups'][2]['count'] == 1
    assert not result['global_changes'] and not result['suggestion']['replace_audio']
    assert result['suggestion']['can_partial'] and result['suggestion']['line_ids'] == []
    assert result['suggestion']['ranges'] == [dict(start=.1, end=1.1)]


def test_comment_projection_ignores_hidden_language_and_manual_selection_metadata(baseline):
    from dubforge import comment_studio
    _, uid, _, definitions = baseline
    project, overlay = _comment_baseline(uid, definitions)
    overlay['items'][0].update(source_text='Changed hidden English', selection_origin='manual', timestamp=123)
    comment_studio._write(project, dict(overlay=overlay))
    assert changes.view(uid, 'translated')['groups'][2]['status'] == 'unchanged'
    save('project:' + uid, {'comments.color': '#00FF00'})
    result = changes.view(uid, 'translated')
    assert result['groups'][2]['count'] == 2 and result['suggestion']['can_partial']
    assert not result['global_changes']


def _long_manual_comment_baseline(uid, definitions):
    from dubforge import comment_studio
    project, overlay = _comment_baseline(uid, definitions)
    overlay['settings']['language_mode'] = 'bilingual'
    overlay['items'] = [{**overlay['items'][0], 'selection_origin': 'manual',
                         'display_text': '中' * 321, 'source_text': 'a' * 1011, 'author': '长评作者'}]
    save('project:' + uid, {'comments.language_mode': 'bilingual'})
    comment_studio._write(project, dict(overlay=overlay))
    deps = Dependencies(project)
    version = av.resolve(deps, definitions[0], comment_plan=comment_studio.effective_overlay(uid, overlay))
    version['sentence_inputs'] = changes.capture(deps, version)
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        record.snapshot = json.dumps(version)
        session.add(record)
        session.commit()
    return project, overlay


@pytest.mark.parametrize('historical_fallback', [False, True])
def test_manual_long_comment_keeps_full_text_in_both_comparison_apis(baseline, historical_fallback):
    from dubforge import comment_studio
    client, uid, _, definitions = baseline
    project, overlay = _long_manual_comment_baseline(uid, definitions)
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        snapshot = json.loads(record.snapshot)
        contract = snapshot['sentence_inputs']['global_inputs']['comments']
        assert 'selection_origin' not in contract['items'][0], '来源仍不属于渲染指纹'
        assert snapshot['comment_overlay']['items'][0]['selection_origin'] == 'manual'
        if historical_fallback:
            snapshot['sentence_inputs'].pop('comment_inputs')
            record.snapshot = json.dumps(snapshot)
            session.add(record)
            session.commit()
    before = comment_studio._path(project).read_bytes()
    plan_response = client.post(f'/api/projects/{uid}/assembly-versions/plan',
        json=dict(version_ids=['translated'], overlay_revision=comment_studio.view(uid)['overlay_revision']))
    assert plan_response.status_code == 200, plan_response.text
    for suffix in ('changes', 'sentence-changes'):
        response = client.get(f'/api/projects/{uid}/assembly-versions/{suffix}?version_id=translated')
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['groups'][2]['status'] == 'unchanged' and result['summary']['total_changes'] == 0
        assert result['comments'] == [] and not result['needs_update']
    assert comment_studio._path(project).read_bytes() == before, '读取对照不能改写已确认方案'
    overlay['items'][0]['display_text'] += '新'
    comment_studio._write(project, dict(overlay=overlay))
    for suffix in ('changes', 'sentence-changes'):
        response = client.get(f'/api/projects/{uid}/assembly-versions/{suffix}?version_id=translated')
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['groups'][2]['count'] == 1 and result['summary']['total_changes'] == 1
        comment = result['comments'][0]
        assert comment['before'][0]['parts'] == ['中' * 321, 'a' * 1011]
        assert comment['current'][0]['parts'] == ['中' * 321 + '新', 'a' * 1011]
        assert result['needs_update'] and result['suggestion']['can_partial']


@pytest.mark.parametrize('invalid', ['automatic-long', 'manual-empty'])
def test_comment_comparison_fix_does_not_weaken_actual_generation_validation(baseline, invalid):
    from dubforge import comment_studio
    client, uid, _, definitions = baseline
    project, overlay = _long_manual_comment_baseline(uid, definitions)
    item = overlay['items'][0]
    if invalid == 'automatic-long':
        item['selection_origin'] = 'automatic'
    else:
        item['display_text'] = ' '
    comment_studio._write(project, dict(overlay=overlay))
    revision = comment_studio.view(uid)['overlay_revision']
    response = client.post(f'/api/projects/{uid}/assembly-versions/plan',
                           json=dict(version_ids=['translated'], overlay_revision=revision))
    assert response.status_code == 400, response.text
    error = response.json()
    assert error['code'] == 'COMMENTS_DISPLAY_TEXT'
    assert '上屏评论' in error['message'] and '长评作者' in error['message']
    assert error['detail']['comment_id'] == item['id']
    if invalid == 'automatic-long':
        assert '自动选择建议上限' in error['message']
    else:
        assert '文字为空' in error['message'] and '精简' not in error['suggestion']


@pytest.mark.parametrize('invalid_text', [None, '', ' '])
def test_invalid_historical_comment_contract_is_unknown_without_inventing_text_or_origin(invalid_text):
    from dubforge import comment_studio
    from dubforge.pipeline.assembly_change_groups import saved_comments
    historical = comment_studio.render_fingerprint_plan(dict(settings=comment_studio.DEFAULT_SETTINGS,
        items=[dict(id='invalid', display_text=invalid_text)]))
    saved = dict(include_comments=True, sentence_inputs=dict(duration=100, global_inputs=dict(comments=historical)))
    assert saved_comments(saved) == dict(known=False, events=[])


@pytest.mark.parametrize('duration', ['missing', None, 0, -1, '100', True, float('nan'), float('inf')])
def test_historical_comment_contract_without_valid_duration_is_unknown(duration):
    from dubforge import comment_studio
    from dubforge.pipeline.assembly_change_groups import saved_comments
    historical = comment_studio.render_fingerprint_plan(dict(settings=comment_studio.DEFAULT_SETTINGS,
        items=[dict(id='historical', display_text='已渲染的评论')]))
    inputs = dict(duration=duration, global_inputs=dict(comments=historical))
    if duration == 'missing':
        inputs.pop('duration')
    assert saved_comments(dict(include_comments=True, sentence_inputs=inputs)) == dict(known=False, events=[])


def test_partial_comment_snapshot_inherits_unselected_old_comments(baseline, monkeypatch):
    from dubforge import comment_studio
    _, uid, _, definitions = baseline
    project, overlay = _comment_baseline(uid, definitions)
    before = json.loads(av.results(uid)['translated'].snapshot)
    overlay['items'][0]['display_text'] = '本次应用的新评论'
    overlay['items'][1]['display_text'] = '范围外尚未应用'
    comment_studio._write(project, dict(overlay=overlay))
    deps = Dependencies(crud.get_project(uid))
    version = av.resolve(deps, definitions[0], comment_plan=comment_studio.effective_overlay(uid, overlay))
    version['partial'] = dict(plan_id='c' * 32)
    plan = dict(base_version=before, ranges=[dict(start=.1, end=1.1)], replace_audio=False)
    monkeypatch.setattr('dubforge.pipeline.assembly_partial.load_plan', lambda *a, **kw: plan)
    result = changes.completed(deps, version)
    events = {event['id']: event for event in result['comment_inputs']['events']}
    assert events['first']['parts'] == ['本次应用的新评论']
    assert events['second']['parts'] == ['第二条原有评论']
    assert result['comment_inputs']['known'] and not result['mixed_globals']


def test_cuts_changes_always_explain_full_rebuild(baseline, monkeypatch):
    _, uid, _, _ = baseline
    real = av.resolve
    def current_cut(*args, **kwargs):
        result = real(*args, **kwargs)
        result['output_cuts'] = dict(schema=1, ranges=[dict(start=1, end=2)])
        return result
    monkeypatch.setattr(av, 'resolve', current_cut)
    result = changes.view(uid, 'translated')
    assert result['groups'][3]['status'] == 'changed'
    assert not result['suggestion']['can_partial']
    assert result['needs_update']
    assert any('时间轴' in reason for reason in result['full_rebuild_reasons'])
    assert all('尾帧修复' not in reason for reason in result['full_rebuild_reasons']), '初次增加剪辑应解释方案变化，不伪称旧剪辑渲染错误'


def test_unchanged_cut_baseline_does_not_require_permanent_full_update(baseline, monkeypatch):
    from dubforge.pipeline.output_cuts import RENDER_VERSION
    _, uid, _, _ = baseline
    cuts = dict(schema=1, ranges=[dict(start=1, end=2)])
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        snapshot = json.loads(record.snapshot)
        snapshot['output_cuts'] = cuts
        snapshot['sentence_inputs']['global_inputs']['output_cuts'] = cuts
        snapshot['sentence_inputs']['global_inputs']['output_cuts_renderer'] = RENDER_VERSION
        record.snapshot = json.dumps(snapshot)
        session.add(record)
        session.commit()
    real = av.resolve
    def same_cut(*args, **kwargs):
        result = real(*args, **kwargs)
        result['output_cuts'] = cuts
        return result
    monkeypatch.setattr(av, 'resolve', same_cut)
    result = changes.view(uid, 'translated')
    assert result['coverage'] == 'known' and result['summary']['total_changes'] == 0
    assert not result['whole_render_changed'] and not result['needs_update']
    assert result['full_rebuild_reasons'] == [] and not result['suggestion']['can_partial']
    assert any('局部替换暂不支持' in reason for reason in result['partial_unavailable_reasons'])
    assert '无需更新' in result['suggestion']['reason']


def test_old_cut_renderer_is_explicit_change_until_rebuilt_with_current_rules(baseline, monkeypatch):
    from dubforge.pipeline import output_cuts
    _, uid, _, _ = baseline
    cuts = dict(schema=1, ranges=[dict(start=1, end=2)])
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        snapshot = json.loads(record.snapshot)
        snapshot['output_cuts'] = cuts
        snapshot['sentence_inputs']['global_inputs']['output_cuts'] = cuts
        snapshot['sentence_inputs']['global_inputs'].pop('output_cuts_renderer', None)
        record.snapshot = json.dumps(snapshot)
        session.add(record)
        session.commit()
    real = av.resolve
    def same_cut(*args, **kwargs):
        result = real(*args, **kwargs)
        result['output_cuts'] = cuts
        return result
    monkeypatch.setattr(av, 'resolve', same_cut)
    result = changes.view(uid, 'translated')
    assert result['needs_update'] and result['whole_render_changed']
    assert result['groups'][3]['changes'][0]['id'] == 'output_cuts_renderer'
    assert any('尾帧修复' in reason for reason in result['full_rebuild_reasons'])
    assert all('时间轴已变化' not in reason for reason in result['global_changes'])
    assert result['summary']['changed'] == 0, '渲染器修复不能伪装成字幕内容变化'


def test_fresh_legacy_unknown_can_optionally_establish_comparison_without_claiming_update(baseline):
    _, uid, _, _ = baseline
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        snapshot = json.loads(record.snapshot)
        snapshot.pop('sentence_inputs')
        record.snapshot = json.dumps(snapshot)
        session.add(record)
        session.commit()
    result = changes.view(uid, 'translated')
    assert result['coverage'] == 'unknown'
    assert not result['whole_render_changed'] and not result['needs_update']
    assert result['full_rebuild_reasons'] == []
    assert result['partial_unavailable_reasons'] and '按需' in result['suggestion']['reason']


def test_missing_baseline_requires_generation_even_without_changed_inputs(baseline):
    _, uid, _, definitions = baseline
    deps = Dependencies(crud.get_project(uid))
    art = av.result_artifact(deps, definitions[0])
    deps.path(art.step_key, art.name).unlink()
    result = changes.view(uid, 'translated')
    assert not result['whole_render_changed'] and result['needs_update']
    assert '基准成片文件缺失。' in result['full_rebuild_reasons']


def test_unknown_comment_evidence_checks_whole_render_before_claiming_current(baseline, monkeypatch):
    _, uid, _, definitions = baseline
    _comment_baseline(uid, definitions)
    with crud.session() as session:
        record = session.get(AssemblyResult, uid + ':translated')
        snapshot = json.loads(record.snapshot)
        snapshot['sentence_inputs']['comment_inputs']['known'] = False
        record.snapshot = json.dumps(snapshot)
        session.add(record)
        session.commit()
    checked = []
    monkeypatch.setattr(av, 'is_current', lambda *args, **kwargs: checked.append(True) or False)
    result = changes.view(uid, 'translated')
    assert checked and result['coverage'] == 'known'
    assert result['groups'][2]['status'] == 'unknown'
    assert result['whole_render_changed'] and result['needs_update']
