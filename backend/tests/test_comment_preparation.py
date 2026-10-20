"""准备数量只改草稿，人工意图、缓存与失败边界均使用隔离工程。"""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_preparation as cp, comment_studio as cs, configuration
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from test_comment_studio import seed, fake_rating, fake_translation, replace_comment_text


@pytest.fixture
def uid(make_project):
    uid = make_project()
    seed(uid, 5)
    return uid


def request_body(uid, target=2, **values):
    state = cs.view(uid)
    return {**dict(target=target, prompt=cs.FILTER_PROMPT, settings=state['defaults']), **values}


def cache(uid, *, translated=(), issues=None):
    project = crud.get_project(uid)
    rows = cs.view(uid)['rows']
    doc = cs._read(project)
    doc['rating_cache'] = dict(version=cs.FILTER_VERSION, prompt=cs.FILTER_PROMPT,
        context_hash=cs._hash(cs._context(project)), assessments=[dict(id=row['id'], source_hash=row['source_hash'],
            eligible=True, score=max(0, 100-index), reason='有效反馈') for index, row in enumerate(rows)])
    doc['translations'] = {row['id']: dict(source_hash=row['source_hash'], text_zh='已有译文 ' + row['id'])
                           for row in rows if row['id'] in translated}
    if issues:
        fingerprint = cs._processing_key('translate', '', cs._context(project), cs._profiles())
        doc['ai_issues'] = {'translate': {ident: dict(source_hash=rows[int(ident)]['source_hash'],
            fingerprint=fingerprint, status=status, code='LLM_OUTPUT_INVALID', message='测试已有问题')
            for ident, status in issues.items()}}
    cs._write(project, doc)


def selected(uid, ids, *, origin='manual', text='人工已核对文字'):
    rows = {row['id']: row for row in cs.view(uid)['rows']}
    return [dict(rows[ident], source_text=rows[ident]['text'], display_text=text, selection_origin=origin) for ident in ids]


def forbid(*args, **kwargs):
    pytest.fail('有效缓存或人工文字不能再次请求模型')


def test_prepare_reuses_existing_and_only_translates_shortfall_then_returns_unsaved_draft(uid, monkeypatch):
    cache(uid, translated=['0'])
    calls = []
    def translate(prompt, **kwargs):
        calls.append(prompt)
        return fake_translation(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', translate)
    result = cp.prepare(uid, request_body(uid))
    prepared = result['preparation']
    assert len(calls) == 1
    assert [row['id'] for row in prepared['draft']['items']] == ['0', '1']
    assert all(row['selection_origin'] == 'automatic' for row in prepared['draft']['items'])
    assert prepared['summary']['ready'] == 2 and prepared['summary']['shortfall'] == 0
    assert cs.view(uid)['overlay'] is None
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    assert cp.prepare(uid, request_body(uid))['preparation']['summary']['ready'] == 2


def test_preparation_summary_counts_whole_thirty_to_sixty_run_without_double_counting_batches(uid, monkeypatch):
    seed(uid, 500)
    cache(uid, translated=[str(index) for index in range(30)])
    rows = cs.view(uid)['rows']
    previous = [dict(row, display_text=row['text_zh'], source_text=row['text'], selection_origin='automatic')
                for row in rows[:30]]
    batches = []
    def translate(prompt, **kwargs):
        batches.append(prompt)
        return fake_translation(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', translate)
    result = cp.prepare(uid, request_body(uid, 60, items=previous))['preparation']
    assert len(batches) == 3 and len(result['draft']['items']) == 60
    assert {key: result['summary'][key] for key in ('new_translations', 'reused_translations', 'new_ratings', 'reused_ratings')} == {
        'new_translations': 30, 'reused_translations': 30, 'new_ratings': 0, 'reused_ratings': 500}
    assert cs.view(uid)['overlay'] is None
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    followup = cp.prepare(uid, request_body(uid, 60, items=result['draft']['items']))['preparation']
    assert followup['summary']['new_translations'] == 0 and followup['summary']['reused_translations'] == 60


def test_empty_caches_score_then_translate_in_same_preparation(uid, monkeypatch):
    actions = []
    def model(prompt, **kwargs):
        action = 'filter' if '【评分要求】' in prompt else 'translate'
        actions.append(action)
        return (fake_rating if action == 'filter' else fake_translation)(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', model)
    result = cp.prepare(uid, request_body(uid))
    assert actions == ['filter', 'translate']
    assert result['prepare_job']['status'] == 'done'
    assert result['job']['action'] == 'translate'
    assert result['preparation']['summary']['new_ratings'] == 5
    assert result['preparation']['summary']['new_translations'] == 2
    assert result['preparation']['summary']['reused_ratings'] == 0


def test_prepare_reduces_only_automatic_and_preserves_manual_order_text_and_exclusion(uid, monkeypatch):
    cache(uid, translated=[str(i) for i in range(5)])
    manual = selected(uid, ['3'])
    old_auto = selected(uid, ['4'], origin='automatic')
    excluded = [{'id': '0', 'source_hash': cs.view(uid)['rows'][0]['source_hash']}]
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    result = cp.prepare(uid, request_body(uid, 2, items=manual + old_auto, excluded_ids=excluded, order_mode='manual'))['preparation']
    assert [row['id'] for row in result['draft']['items']] == ['3', '1']
    assert result['draft']['items'][0]['display_text'] == '人工已核对文字'
    assert result['draft']['excluded_ids'] == excluded
    assert result['summary']['reused_translations'] == 1  # 人工自写文字不冒充缓存译文复用。
    reduced = cp.prepare(uid, request_body(uid, 1, **result['draft']))['preparation']
    assert [row['id'] for row in reduced['draft']['items']] == ['3']


def test_kept_automatic_custom_text_does_not_require_a_translation(uid, monkeypatch):
    cache(uid)
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    result = cp.prepare(uid, request_body(uid, 1, items=selected(uid, ['0'], origin='automatic')))['preparation']
    assert result['draft']['items'][0]['display_text'] == '人工已核对文字'
    assert result['summary']['ready'] == 1


def test_manual_count_above_target_or_capacity_is_kept_and_reported(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    result = cp.prepare(uid, request_body(uid, 1, items=selected(uid, ['3', '1']), order_mode='manual'), capacity=1)['preparation']
    assert [row['id'] for row in result['draft']['items']] == ['3', '1']
    assert result['summary']['manual_over_target'] == 1
    assert result['summary']['capacity_exceeded'] is True


def test_long_manual_is_ready_and_never_replaced_by_high_score(uid, monkeypatch):
    cache(uid, translated=[str(i) for i in range(5)])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    result = cp.prepare(uid, request_body(uid, 1, items=selected(uid, ['4'], text='字' * 241)))['preparation']
    assert [row['id'] for row in result['draft']['items']] == ['4']
    assert result['summary']['ready'] == 1 and result['summary']['invalid_manual'] == 0
    assert result['invalid_manual'] == []
    assert any('超过建议字数' in warning for warning in result['warnings'])
    assert result['draft']['items'][0]['display_text'] == '字' * 241


def test_only_blank_manual_text_is_filled_and_original_language_needs_no_translation(uid, monkeypatch):
    cache(uid)
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    result = cp.prepare(uid, request_body(uid, 1, items=selected(uid, ['4'], text='')))['preparation']
    assert result['draft']['items'][0]['display_text']
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    body = request_body(uid, 2)
    body['settings']['language_mode'] = 'original'
    result = cp.prepare(uid, body)['preparation']
    assert result['summary']['ready'] == 2
    assert result['draft']['items'][0]['display_text'] == ''


def test_failures_blocked_and_deferred_are_not_resubmitted_by_normal_prepare(uid, monkeypatch):
    cache(uid, translated=['3', '4'], issues={'0': 'blocked', '1': 'failed', '2': 'deferred'})
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    result = cp.prepare(uid, request_body(uid, 2))['preparation']
    assert [row['id'] for row in result['draft']['items']] == ['3', '4']
    assert result['summary']['excluded_automatic'] == 3


def test_old_saved_items_default_automatic_and_may_be_replaced(uid, monkeypatch):
    cache(uid, translated=[str(i) for i in range(5)])
    state = cs.view(uid)
    old = selected(uid, ['4'])
    old[0].pop('selection_origin')
    saved = cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'], items=old))
    path = cs._path(crud.get_project(uid))
    before = path.read_bytes()
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    result = cp.prepare(uid, request_body(uid, 1))['preparation']
    assert result['summary']['manual'] == result['summary']['invalid_manual'] == 0
    assert result['summary']['protected'] == result['summary']['invalid_protected'] == 0
    assert [row['id'] for row in result['draft']['items']] == ['0']
    assert cs._read(crud.get_project(uid))['overlay'] == saved['overlay']
    assert saved['overlay']['items'][0]['selection_origin'] == 'automatic'


def test_strict_prepare_does_not_silently_skip_a_known_pending_manual_translation(uid, monkeypatch):
    cache(uid, issues={'0': 'deferred'})
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    with pytest.raises(cs.CommentError) as exc:
        cp.prepare(uid, request_body(uid, 1, items=selected(uid, ['0'], text=''), failure_policy='stop'))
    assert exc.value.code == 'COMMENTS_PENDING'
    assert cs.view(uid)['prepare_job']['status'] == 'failed'


def test_prepare_uses_project_failure_policy_when_not_specified_in_request(uid, monkeypatch):
    cfg = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=cfg['revision'], updates={'comments.failure_policy': 'stop'}))
    cache(uid, issues={'0': 'blocked'})
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    with pytest.raises(cs.CommentError) as exc:
        cp.prepare(uid, request_body(uid, 1, items=selected(uid, ['0'], text='')))
    assert exc.value.code == 'COMMENTS_BLOCKED'


def test_long_scoring_does_not_consume_translation_stage_budget(uid, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cp.time, 'monotonic', lambda: now[0])
    def model(prompt, **kwargs):
        if '【评分要求】' in prompt:
            now[0] = 1700.0
            return fake_rating(prompt, **kwargs)
        now[0] = 2200.0
        return fake_translation(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', model)
    result = cp.prepare(uid, request_body(uid, 1))
    assert result['preparation']['summary']['ready'] == 1
    assert result['prepare_job']['status'] == 'done'


def test_translation_service_failure_keeps_scores_saved_plan_and_manual_draft(uid, monkeypatch):
    state = cs.view(uid)
    manual = selected(uid, ['4'])
    cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'], items=manual))
    old_overlay = copy.deepcopy(cs.view(uid)['overlay'])
    body = request_body(uid, 2, items=manual)
    original_body = copy.deepcopy(body)
    def model(prompt, **kwargs):
        if '【评分要求】' in prompt:
            return fake_rating(prompt, **kwargs)
        raise DubForgeError('测试服务暂时不可用', code='LLM_CONNECTION')
    monkeypatch.setattr(cs, 'chat_validated', model)
    with pytest.raises(DubForgeError) as exc:
        cp.prepare(uid, body)
    assert exc.value.code == 'LLM_CONNECTION'
    state = cs.view(uid)
    assert state['overlay'] == old_overlay and body == original_body
    assert state['rating_progress']['saved'] == 4
    assert state['prepare_job']['status'] == 'failed' and uid not in cs._prepare_cancels


def test_save_persists_intent_and_target_but_intent_metadata_does_not_stale_render(uid):
    state = cs.view(uid)
    items = selected(uid, ['1'], origin='automatic')
    excluded = [{'id': '0', 'source_hash': state['rows'][0]['source_hash']}]
    body = dict(revision=state['overlay_revision'], settings=state['defaults'], items=items, excluded_ids=excluded, target=60)
    result = cs.save(uid, body)
    assert result['configuration']['values']['comments.count'] == 60
    assert result['overlay']['items'][0]['selection_origin'] == 'automatic'
    assert result['overlay']['excluded_ids'] == excluded
    same_picture = copy.deepcopy(result['overlay'])
    same_picture['items'][0]['selection_origin'] = 'manual'
    same_picture['excluded_ids'] = []
    assert cs.render_fingerprint_plan(same_picture) == cs.render_fingerprint_plan(result['overlay'])


def test_prepare_progress_and_cancel_cover_between_model_phases_and_release_ownership(uid, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, **kwargs):
        entered.set()
        assert release.wait(5)
        return fake_rating(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cp.prepare, uid, request_body(uid))
        assert entered.wait(5)
        progress = cs.progress(uid)
        preparation = progress['prepare_job']
        assert preparation['status'] == 'running' and preparation['stage'] == 'scoring'
        assert progress['job']['action'] == 'filter'
        with pytest.raises(DubForgeError) as exc:
            cs.run(uid, 'translate', ids=['0'])
        assert exc.value.code in {'VIDEO_INFO_BUSY', 'CONFIGURATION_BUSY'}
        cs.cancel(uid, preparation['id'])
        release.set()
        with pytest.raises(DubForgeError) as exc:
            future.result(timeout=5)
        assert exc.value.code == 'COMMENTS_CANCELLED'
    assert cs.progress(uid)['prepare_job']['status'] == 'cancelled'
    assert uid not in cs._prepare_cancels and uid not in cs._cancels
    assert cs.view(uid)['busy'] is None and cs.view(uid)['overlay'] is None


@pytest.mark.parametrize('body', [dict(target=0), dict(target=True), dict(target=101), dict(target=1, prompt='')])
def test_prepare_api_validates_before_model_or_saved_plan_changes(uid, monkeypatch, body):
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    response = TestClient(app).post(f'/api/projects/{uid}/comments/prepare', json=body)
    assert response.status_code == 400
    assert cs.view(uid)['overlay'] is None


def test_missing_origin_inherits_saved_automatic_through_prepare_preview_and_save(uid, monkeypatch):
    cache(uid, translated=[str(i) for i in range(5)])
    state = cs.view(uid)
    saved = cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'],
                             items=selected(uid, ['0', '1'], origin='automatic')))
    omitted = copy.deepcopy(saved['overlay']['items'])
    for row in omitted:
        row.pop('selection_origin')
    before = copy.deepcopy(omitted)
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 1, items=omitted))['preparation']
    assert [row['id'] for row in prepared['draft']['items']] == ['0']
    assert prepared['summary']['manual'] == 0
    assert prepared['summary']['automatic'] == 1
    preview = cs.draft(uid, dict(settings=state['defaults'], items=omitted))
    assert all(row['selection_origin'] == 'automatic' for row in preview['items'])
    reread = cs.save(uid, dict(revision=saved['overlay_revision'], settings=state['defaults'], items=omitted))
    assert all(row['selection_origin'] == 'automatic' for row in reread['overlay']['items'])
    assert omitted == before


def test_missing_origin_inherits_saved_manual_and_keeps_edited_text(uid, monkeypatch):
    state = cs.view(uid)
    saved = cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'],
                             items=selected(uid, ['4'])))
    omitted = copy.deepcopy(saved['overlay']['items'])
    omitted[0].pop('selection_origin')
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 1, items=omitted))['preparation']
    assert prepared['summary']['manual'] == prepared['summary']['protected'] == 1
    assert prepared['summary']['automatic'] == 0
    assert prepared['draft']['items'][0]['display_text'] == '人工已核对文字'
    assert cs.draft(uid, dict(settings=state['defaults'], items=omitted))['items'][0]['selection_origin'] == 'manual'


def test_old_origin_normalization_is_read_only_and_revision_matches_confirmation(uid):
    state = cs.view(uid)
    old = cs.draft(uid, dict(settings=state['defaults'], items=selected(uid, ['1'])))['items']
    old[0].pop('selection_origin')
    project = crud.get_project(uid)
    doc = dict(overlay=dict(items=old, settings=state['defaults'], confirmed_at='2026-10-01T00:00:00Z'))
    cs._write(project, doc)
    before = cs._path(project).read_bytes()
    current = cs.view(uid)
    assert current['overlay']['items'][0]['selection_origin'] == 'automatic'
    assert cs.confirmed(uid, current['overlay_revision']) == current['overlay']
    assert cs.render_fingerprint_plan(doc['overlay']) == cs.render_fingerprint_plan(current['overlay'])
    assert cs._path(project).read_bytes() == before
    assert 'selection_origin' not in cs._read(project)['overlay']['items'][0]
    saved = cs.save(uid, dict(revision=current['overlay_revision'], settings=state['defaults'], items=old))
    assert saved['overlay']['items'][0]['selection_origin'] == 'automatic'


def test_mixed_legacy_defaults_to_automatic_while_explicit_manual_is_protected(uid, monkeypatch):
    cache(uid, translated=[str(i) for i in range(5)])
    items = selected(uid, ['4']) + selected(uid, ['3'], origin='legacy') + selected(uid, ['2'], origin='automatic')
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 1, items=items))['preparation']
    assert {key: prepared['summary'][key] for key in ('manual', 'protected', 'automatic')} == {
        'manual': 1, 'protected': 1, 'automatic': 0}
    assert prepared['summary']['manual_over_target'] == prepared['summary']['protected_over_target'] == 0
    assert [row['id'] for row in prepared['draft']['items']] == ['4']
    assert not any('旧方案' in warning or '来源未知' in warning for warning in prepared['warnings'])
    assert 'legacy' not in prepared['summary']


def test_origin_is_never_inherited_across_changed_source_hash():
    assert cs.selection_origin(dict(id='one', source_hash='new'),
        dict(id='one', source_hash='old', selection_origin='manual')) == 'automatic'
    assert cs.selection_origin(dict(id='other', source_hash='same'),
        dict(id='one', source_hash='same', selection_origin='automatic')) == 'automatic'


@pytest.mark.parametrize('origin', ['', None, True, 1, [], {}, 'auto'])
def test_invalid_explicit_origin_is_rejected_in_prepare_and_draft(uid, origin):
    items = selected(uid, ['0'], origin=origin)
    with pytest.raises(cs.CommentError, match='选择来源无效'):
        cp.prepare(uid, request_body(uid, 1, items=items))
    with pytest.raises(cs.CommentError, match='选择来源无效'):
        cs.draft(uid, dict(settings=cs.view(uid)['defaults'], items=items))


@pytest.mark.parametrize('origin', ['manual'])
def test_omitted_saved_protected_origin_still_checks_changed_source_and_review(uid, origin):
    state = cs.view(uid)
    saved = cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'],
                             items=selected(uid, ['0'], origin=origin)))
    omitted = copy.deepcopy(saved['overlay']['items'])
    omitted[0].pop('selection_origin')
    review = copy.deepcopy(omitted)
    review[0]['display_text'] = '天安门广场的悲剧'
    with pytest.raises(cs.CommentError) as exc:
        cs.draft(uid, dict(settings=state['defaults'], items=review))
    assert exc.value.code == 'COMMENTS_MANUAL_INVALID'
    assert exc.value.detail['invalid_manual'][0]['selection_origin'] == origin
    replace_comment_text(uid, '0', 'New source text')
    with pytest.raises(cs.CommentError) as exc:
        cs.draft(uid, dict(settings=state['defaults'], items=omitted))
    assert exc.value.code == 'COMMENTS_MANUAL_INVALID'
    assert exc.value.detail['invalid_manual'][0]['selection_origin'] == origin
