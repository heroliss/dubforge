"""语义分句规则独立失效与历史说明；隔离数据根，不调用模型或生产任务。"""
import hashlib
import json
import threading

import pytest

from conftest import mark_upstream_done
from dubforge.domain import crud
from dubforge.pipeline import stale_explanation as stale, text_prompts
from dubforge.pipeline.config_fingerprint import promote_compatible_fingerprints
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_translate import _chunk_key
from dubforge.pipeline.text_source import source_lines
from dubforge.runner import StepContext, compute_fingerprint, fingerprint_payload


def save_split(project, text):
    root = crud.project_dir(project.uid)
    path = root / 'artifacts/split/split.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    crud.add_artifact(project.id, 'split', 'text', 'split.txt', path, str(path.relative_to(root)))


def split_baseline(make_project):
    uid = make_project()
    mark_upstream_done(uid, 'split')
    project = crud.get_project(uid)
    save_split(project, '1. 0.000-1.000: hello\n')
    snapshot = stale.capture(Dependencies(project), 'split')
    step = crud.get_step(project.id, 'split')
    step.status = 'done'
    step.input_fingerprint = snapshot['fingerprint']
    step.input_snapshot = json.dumps(snapshot, ensure_ascii=False)
    crud.save_step(step)
    return project, snapshot


@pytest.mark.parametrize('use_llm', [True, False])
@pytest.mark.parametrize('stage', ['window-v6', 'window-adaptive-v4'])
def test_semantic_revision_only_changes_split_not_other_steps_or_translation_window(
        make_project, monkeypatch, use_llm, stage):
    project = crud.get_project(make_project(params={'step_params': {'split.use_llm': use_llm}}))
    root = crud.project_dir(project.uid)
    ctx = StepContext(project, 'translate', root, root / 'logs/translate.log', threading.Event())
    chunk = [dict(id=1, text='Welcome to the city.', start=0.0, end=2.0)]
    steps = ('download', 'preprocess', 'transcribe', 'split', 'translate', 'subtitles', 'dubtask', 'tts', 'assemble')
    before = {key: compute_fingerprint(project, key) for key in steps}
    previous_chunk = _chunk_key(ctx, chunk, stage)
    correction = text_prompts.ASR_CORRECTION_VERSION
    translation = text_prompts.TRANSLATION_PROMPT_VERSION
    monkeypatch.setattr(text_prompts, 'SEMANTIC_SEGMENTATION_VERSION', 'semantic-future-fixture')
    after = {key: compute_fingerprint(project, key) for key in steps}
    assert {key for key in steps if before[key] != after[key]} == ({'split'} if use_llm else set())
    assert _chunk_key(ctx, chunk, stage) == previous_chunk
    assert text_prompts.ASR_CORRECTION_VERSION == correction
    assert text_prompts.TRANSLATION_PROMPT_VERSION == translation


@pytest.mark.parametrize('legacy,legacy_config', [(False, False), (False, True), (True, False), (True, True)])
def test_artifact_without_semantic_revision_cannot_be_compatibly_promoted(
        make_project, legacy, legacy_config):
    project, _ = split_baseline(make_project)
    payload = fingerprint_payload(project, 'split', legacy=legacy, legacy_config=legacy_config)
    payload.pop('semantic_segmentation')
    old = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    step = crud.get_step(project.id, 'split')
    step.input_fingerprint = old
    crud.save_step(step)
    assert not Dependencies(project).matches_fingerprint('split')
    assert promote_compatible_fingerprints() == 0
    assert crud.get_step(project.id, 'split').input_fingerprint == old


def test_semantic_revision_reports_evidenced_old_and_new_values(make_project, monkeypatch):
    project, snapshot = split_baseline(make_project)
    previous = snapshot['entries']['rule.semantic_segmentation']['display']
    assert stale._valid_key('rule.semantic_segmentation')
    assert not stale._valid_key('rule.unrecognized_semantic_policy')
    monkeypatch.setattr(text_prompts, 'SEMANTIC_SEGMENTATION_VERSION', 'semantic-next-fixture')
    result = Dependencies(project).explain('split')
    change = next(row for row in result['changes'] if row['key'] == 'rule.semantic_segmentation')
    assert result['history_available'] and not result['fresh']
    assert change['category'] == 'prompt' and change['label'] == '语义分句质量规则版本'
    assert change['before_known'] and change['before'] == previous
    assert change['after'] == 'semantic-next-fixture'


def test_history_before_semantic_revision_does_not_invent_previous_rule(make_project):
    project, snapshot = split_baseline(make_project)
    old_payload = fingerprint_payload(project, 'split')
    old_payload.pop('semantic_segmentation')
    old = hashlib.sha256(json.dumps(old_payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    snapshot['fingerprint'] = old
    snapshot['entries'].pop('rule.semantic_segmentation')
    step = crud.get_step(project.id, 'split')
    step.input_fingerprint = old
    step.input_snapshot = json.dumps(snapshot, ensure_ascii=False)
    crud.save_step(step)
    result = Dependencies(project).explain('split')
    change = next(row for row in result['changes'] if row['key'] == 'rule.semantic_segmentation')
    assert result['history_available'] and not result['fresh']
    assert change['before'] is None and not change['before_known']
    assert change['after'] == text_prompts.SEMANTIC_SEGMENTATION_VERSION
    assert '旧记录未保存此项' in change['message']


def test_regenerated_source_content_changes_translation_fingerprint_and_window(make_project, monkeypatch):
    project, _ = split_baseline(make_project)
    root = crud.project_dir(project.uid)
    ctx = StepContext(project, 'translate', root, root / 'logs/translate.log', threading.Event())
    previous_step = compute_fingerprint(project, 'translate')
    previous_window = _chunk_key(ctx, source_lines(ctx), 'window-v6')
    monkeypatch.setattr(text_prompts, 'SEMANTIC_SEGMENTATION_VERSION', 'semantic-next-fixture')
    assert compute_fingerprint(project, 'translate') == previous_step
    assert _chunk_key(ctx, source_lines(ctx), 'window-v6') == previous_window
    save_split(project, '1. 0.000-0.500: hello\n2. 0.500-1.000: world\n')
    assert compute_fingerprint(project, 'translate') != previous_step
    assert _chunk_key(ctx, source_lines(ctx), 'window-v6') != previous_window


@pytest.mark.parametrize('use_llm', [True, False])
def test_pause_threshold_changes_split_only_when_semantic_model_consumes_it(make_project, use_llm):
    from dubforge import config

    project = crud.get_project(make_project(params={'step_params': {'split.use_llm': use_llm}}))
    previous = compute_fingerprint(project, 'split')
    translation = compute_fingerprint(project, 'translate')
    config.set_key('asr.pause_threshold_seconds', 1.2)
    assert (compute_fingerprint(project, 'split') != previous) is use_llm
    assert compute_fingerprint(project, 'translate') == translation


def test_translation_window_revision_changes_only_translation_and_has_chinese_explanation(make_project, monkeypatch):
    project = crud.get_project(make_project())
    before = {key: compute_fingerprint(project, key) for key in ('download', 'preprocess', 'transcribe', 'split', 'translate')}
    snapshot = stale.capture(Dependencies(project), 'translate')
    row = crud.get_step(project.id, 'translate')
    row.status = 'done'
    row.input_fingerprint = snapshot['fingerprint']
    row.input_snapshot = json.dumps(snapshot, ensure_ascii=False)
    crud.save_step(row)
    monkeypatch.setattr(text_prompts, 'TRANSLATION_WINDOW_VERSION', 'proactive-future-test')
    after = {key: compute_fingerprint(project, key) for key in before}
    assert {key for key in before if before[key] != after[key]} == {'translate'}
    explanation = Dependencies(project).explain('translate')
    change = next(item for item in explanation['changes'] if item['key'] == 'rule.translation_windows')
    assert change['label'] == '翻译分块与衔接策略版本'
    assert change['before_known'] and change['after'] == 'proactive-future-test'


def test_translation_without_window_policy_cannot_be_compatibly_promoted(make_project):
    project = crud.get_project(make_project())
    payload = fingerprint_payload(project, 'translate', legacy_config=True)
    payload.pop('translation_windows')
    old = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    row = crud.get_step(project.id, 'translate')
    row.status = 'done'
    row.input_fingerprint = old
    crud.save_step(row)
    assert promote_compatible_fingerprints() == 0
    assert crud.get_step(project.id, 'translate').input_fingerprint == old


@pytest.mark.parametrize('use_llm', [True, False])
def test_unused_window_override_never_invalidates_full_document_segmentation(make_project, use_llm):
    from dubforge import config
    project = crud.get_project(make_project(params={'step_params': {'split.use_llm': use_llm}}))
    previous = compute_fingerprint(project, 'split')
    config.set_key('split.window_max_chars', 1000)
    assert compute_fingerprint(project, 'split') == previous


@pytest.mark.parametrize('use_llm', [True, False])
def test_thinking_mode_only_invalidates_model_segmentation(make_project, use_llm):
    from dubforge import config
    project = crud.get_project(make_project(params={'step_params': {'split.use_llm': use_llm}}))
    before = {key: compute_fingerprint(project, key) for key in ('transcribe', 'split', 'translate')}
    config.set_key('split.thinking_mode', 'follow_profile')
    after = {key: compute_fingerprint(project, key) for key in before}
    assert {key for key in before if before[key] != after[key]} == ({'split'} if use_llm else set())


def test_fast_mode_fingerprint_uses_actual_request_parameters(make_project):
    from dubforge import config
    project = crud.get_project(make_project())
    config.set_key('llm.profiles', [dict(name='ark', enabled=True, api_key='fixture',
                    base_url='https://ark.cn-beijing.volces.com/api/plan/v3',
                    model='ark-code-latest', params={'reasoning_effort': 'high'})])
    previous = compute_fingerprint(project, 'split')
    config.set_key('llm.profiles', [dict(name='ark', enabled=True, api_key='fixture',
                    base_url='https://ark.cn-beijing.volces.com/api/plan/v3',
                    model='ark-code-latest', params={'reasoning_effort': 'medium'})])
    assert compute_fingerprint(project, 'split') == previous


@pytest.mark.parametrize('step', ['split', 'translate'])
def test_low_and_old_fast_alias_share_effective_fingerprint(make_project, step):
    from dubforge import config
    project = crud.get_project(make_project())
    config.set_key(f'{step}.thinking_mode', 'low')
    previous = compute_fingerprint(project, step)
    config.set_key(f'{step}.thinking_mode', 'fast')
    assert compute_fingerprint(project, step) == previous


def test_translation_low_mode_changes_only_translation(make_project):
    from dubforge import config
    project = crud.get_project(make_project())
    before = {key: compute_fingerprint(project, key) for key in ('transcribe', 'split', 'translate')}
    config.set_key('translate.thinking_mode', 'follow_profile')
    after = {key: compute_fingerprint(project, key) for key in before}
    assert {key for key in before if before[key] != after[key]} == {'translate'}
