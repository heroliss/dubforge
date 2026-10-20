"""评论全链路回归：网络和 LLM 全部替身，媒体渲染用隔离工程真实 FFmpeg。"""
import copy
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import wait_status
from dubforge import comment_studio as cs, video_info as vi
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import comment_overlay as co
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.comment_protocol import escape_text, unescape_text


def seed(uid, count=3):
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    info = {'title_original': '保留标题', 'source': {'comment_count': 3000}, 'comments': {
        'items': [{'id': str(i), 'text': f'Original opinion {i}', 'author': '观众', 'like_count': i,
                   'url': f'https://www.youtube.com/watch?v=abc&lc={i}'} for i in range(count)]}}
    atomic_write_bytes(path, json.dumps(info).encode())
    return path


@pytest.fixture
def uid(make_project):
    uid = make_project()
    seed(uid)
    return uid


def request_payload(prompt):
    """伪模型只看逐行编号；不借用领域层平台 ID 映射。"""
    comments = []
    for line in prompt.split('【待处理评论】\n', 1)[1].split('\n\n', 1)[0].splitlines():
        match = re.fullmatch(r'(\d+)\. (.*)', line)
        if match:
            comments.append({'id': match[1], 'text': unescape_text(match[2])})
    return {'comments': comments}


def wire_response(result):
    if 'translations' in result:
        return '\n'.join(f'{row["id"]}. {escape_text(row["text_zh"])}' for row in result['translations'])
    return '\n'.join(f'{row["id"]}. {row["score"]} | {str(row["eligible"]).lower()} | {escape_text(row["reason"])}'
                     for row in result['assessments'])


def validated_result(result, kwargs):
    return kwargs['validator'](kwargs['parser'](wire_response(result)))


def fake_translation(prompt, **kwargs):
    result = {'translations': [{'id': row['id'], 'text_zh': f'完整中文观点 {row["id"]}'}
                               for row in request_payload(prompt)['comments']]}
    return validated_result(result, kwargs)


def fake_rating(prompt, **kwargs):
    return validated_result({'assessments': [{'id': row['id'], 'eligible': row['text'] != 'Original opinion 0',
        'score': 20 if row['text'] == 'Original opinion 0' else 80 + int(row['id']), 'reason': '有温度的观众反馈'}
        for row in request_payload(prompt)['comments']]}, kwargs)


def plan_body(uid, **settings):
    state = cs.view(uid)
    return {'revision': state['overlay_revision'], 'items': [dict(row, display_text='已人工核对的中文', selection_origin='manual') for row in state['rows']],
            'settings': {**cs.DEFAULT_SETTINGS, 'start': .5, 'interval': 2, 'duration': 1, **settings}}


def replace_comment_text(uid, ident, text):
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    info = json.loads(path.read_text(encoding='utf-8'))
    next(row for row in info['comments']['items'] if row['id'] == ident)['text'] = text
    atomic_write_bytes(path, json.dumps(info, ensure_ascii=False).encode())
    return path


def test_review_checks_authoritative_original_even_after_display_is_sanitized(uid):
    path = replace_comment_text(uid, '0', 'The Tiananmen Square massacre')
    original_bytes = path.read_bytes()
    body = plan_body(uid)
    for item in body['items']:
        item['selection_origin'] = 'automatic'
    body['items'][0].update(text='伪造安全原文', source_text='Short original', display_text='普通中文评论')
    draft = cs.draft(uid, body)
    assert [row['id'] for row in draft['items']] == ['1', '2']
    assert draft['review_excluded'][0]['id'] == '0'
    saved = cs.save(uid, body)
    assert [row['id'] for row in saved['overlay']['items']] == ['1', '2']
    assert saved['rows'][0]['review_blocked'] and saved['rows'][0]['review_reasons']
    assert not saved['rows'][1]['review_blocked'] and saved['rows'][1]['review_reasons'] == []
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize('mode,field,text', [
    ('original', 'display_text', '天安门广场的悲剧'),
    ('translated', 'source_text', 'The Tiananmen Square massacre'),
    ('bilingual', 'source_text', '天安门广场的悲剧'),
])
def test_review_checks_both_edited_languages_even_when_one_is_hidden(uid, mode, field, text):
    body = plan_body(uid, language_mode=mode)
    for item in body['items']:
        item['selection_origin'] = 'automatic'
    body['items'][0][field] = text
    draft = cs.draft(uid, body)
    assert [row['id'] for row in draft['items']] == ['1', '2']
    assert draft['review_excluded'][0]['review_reasons']


def test_review_all_excluded_automatic_selection_is_explicit_and_keeps_previous_plan(uid):
    saved = cs.save(uid, plan_body(uid))
    before = cs._path(crud.get_project(uid)).read_bytes()
    body = plan_body(uid)
    for item in body['items']:
        item['selection_origin'] = 'automatic'
        item['display_text'] = '天安门广场的悲剧'
    with pytest.raises(cs.CommentError) as exc:
        cs.save(uid, body)
    assert exc.value.code == 'COMMENTS_REVIEW_EMPTY'
    assert len(exc.value.detail['review_excluded']) == 3
    assert cs._path(crud.get_project(uid)).read_bytes() == before
    assert cs.confirmed(uid)['items'] == saved['overlay']['items']


def test_view_flags_blocked_translation_without_modifying_source_or_cache(uid):
    project = crud.get_project(uid)
    rows = cs.view(uid)['rows']
    cs._write(project, {'translations': {'1': dict(source_hash=rows[1]['source_hash'],
                                                  text_zh='天安门广场的悲剧')}})
    before = cs._path(project).read_bytes()
    state = cs.view(uid)
    assert state['rows'][1]['review_blocked'] and state['rows'][1]['review_reasons']
    assert not state['rows'][0]['review_blocked']
    assert state['rows'][1]['text'] == 'Original opinion 1'
    assert state['rows'][1]['text_zh'] == '天安门广场的悲剧'
    assert cs._path(project).read_bytes() == before


def test_local_review_overrides_perfect_llm_rating_before_limit_and_preserves_raw_rating(uid, monkeypatch):
    replace_comment_text(uid, '0', 'The Tiananmen Square massacre')
    def trusting_rating(prompt, **kwargs):
        system = kwargs['system']
        for phrase in ('国家制度', '主权统一', '国家象征', '过度宣教', '恐怖主义', '引用新闻',
                       '历史学习', '普通宗教祝福', '正常地名', '关键词直接否决', '不能宣称该内容违法'):
            assert phrase in system
        return validated_result({'assessments': [dict(id=row['id'], eligible=True, score=100-i,
            reason='模型未识别风险') for i, row in enumerate(request_payload(prompt)['comments'])]}, kwargs)
    monkeypatch.setattr(cs, 'chat_validated', trusting_rating)
    state = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT, limit=1)
    assert [row['id'] for row in state['filter']['items']] == ['1']
    blocked = next(row for row in state['filter']['assessments'] if row['id'] == '0')
    assert blocked['eligible'] and blocked['review_blocked'] and blocked['outcome'] == 'review_blocked'
    raw = cs._read(crud.get_project(uid))['rating_cache']['assessments'][0]
    assert raw['eligible'] and 'review_blocked' not in raw


def test_historical_confirmed_plan_is_rescreened_without_rewriting_document(uid):
    from test_configuration import save as conf_save
    conf_save('project:' + uid, {'content_review.enabled': False})
    body = plan_body(uid)
    body['items'][0]['source_text'] = 'The Tiananmen Square massacre'
    saved = cs.save(uid, body)
    assert len(saved['overlay']['items']) == 3
    conf_save('project:' + uid, {'content_review.enabled': True})
    before = cs._path(crud.get_project(uid)).read_bytes()
    state = cs.view(uid)
    assert state['rows'][0]['review_blocked']
    assert state['overlay']['items'][0]['review_blocked']
    checked = cs.confirmed(uid, state['overlay_revision'])
    assert [row['id'] for row in checked['items']] == ['1', '2']
    assert checked['review_excluded'][0]['id'] == '0'
    from dubforge.comments_auto import current_overlay
    assert current_overlay(uid)['items'] == checked['items']
    assert cs._path(crud.get_project(uid)).read_bytes() == before


def test_historical_all_blocked_snapshot_cannot_fall_back_to_auto_or_missing_source(uid):
    from test_configuration import save as conf_save
    conf_save('project:' + uid, {'content_review.enabled': False, 'comments.mode': 'auto'})
    replace_comment_text(uid, '0', 'The Tiananmen Square massacre')
    body = plan_body(uid); body['items'] = body['items'][:1]
    cs.save(uid, body)
    seed(uid, 0)
    conf_save('project:' + uid, {'content_review.enabled': True})
    before = cs._path(crud.get_project(uid)).read_bytes()
    from dubforge.comments_auto import confirmed_or_auto, current_overlay
    for operation in (cs.confirmed, confirmed_or_auto, current_overlay):
        with pytest.raises(DubForgeError) as exc:
            operation(uid)
        assert exc.value.code == 'COMMENTS_REVIEW_EMPTY'
    assert cs._path(crud.get_project(uid)).read_bytes() == before


def test_review_uses_project_resolved_custom_terms_and_new_authoritative_text(uid):
    from test_configuration import save as conf_save
    conf_save('global', {'content_review.blocked_terms': 'Original'})
    conf_save('project:' + uid, {'content_review.blocked_terms': 'opinion 0'})
    state = cs.view(uid)
    assert state['rows'][0]['review_blocked'] and not state['rows'][1]['review_blocked']
    body = plan_body(uid)
    for item in body['items']:
        item['selection_origin'] = 'automatic'
    saved = cs.save(uid, body)
    assert [row['id'] for row in saved['overlay']['items']] == ['1', '2']
    replace_comment_text(uid, '1', 'The Tiananmen Square massacre')
    assert [row['id'] for row in cs.confirmed(uid)['items']] == ['2']


def test_filter_policy_version_invalidates_failed_rating_cache_only():
    profiles = [{'name': 'fixture'}]
    old_translation = cs._hash(['comment-lines-v1', 'translate', '', {}, profiles])
    old_filter = cs._hash(['comment-lines-v1', 'filter', cs.FILTER_PROMPT, {}, profiles])
    assert cs._processing_key('translate', cs.FILTER_PROMPT, {}, profiles) == old_translation
    assert cs._processing_key('filter', cs.FILTER_PROMPT, {}, profiles) != old_filter


def test_outline_saved_with_revision_and_frozen_in_task_snapshot(uid):
    saved = cs.save(uid, plan_body(uid, outline_color='#12AB34', outline=4.5))
    first = cs.for_task(uid, enabled=True, revision=saved['overlay_revision'])
    assert first['settings']['outline_color'] == '#12AB34' and first['settings']['outline'] == 4.5
    changed = cs.save(uid, plan_body(uid, outline_color='#FFFFFF', outline=0))
    assert changed['overlay_revision'] != saved['overlay_revision']
    assert first['settings']['outline'] == 4.5
    assert cs.for_task(uid, enabled=True, revision=changed['overlay_revision'])['settings']['outline'] == 0


def test_translation_preserves_originals_and_retries_only_unfinished(uid, monkeypatch):
    path = seed(uid, 12)
    original = path.read_bytes()
    calls = []
    def incomplete(prompt, **kwargs):
        rows = request_payload(prompt)['comments']; calls.append([r['id'] for r in rows])
        if len(calls) == 2:
            raise RuntimeError('模拟网络错误，私密诊断不回显')
        return fake_translation(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', incomplete)
    with pytest.raises(cs.CommentError, match='评论 AI 处理失败'):
        cs.run(uid, 'translate')
    state = cs.view(uid)
    assert sum(bool(r['text_zh']) for r in state['rows']) == 10
    assert state['job']['status'] == 'failed' and not state['busy']
    assert path.read_bytes() == original
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    state = cs.run(uid, 'translate')
    assert all(r['text_zh'] for r in state['rows'])
    assert state['job']['total'] == 2
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **k: pytest.fail('已翻译不能重复调用 AI'))
    assert cs.run(uid, 'translate')['job']['total'] == 0
    assert state['overlay'] is None


@pytest.mark.parametrize('payload', [
    {'translations': []}, {'translations': [{'id': 'fake', 'text_zh': '假造'}]},
    {'translations': [{'id': '0', 'text_zh': ''}]},
    {'translations': [{'id': '0', 'text_zh': '一'}, {'id': '0', 'text_zh': '重复'}]},
    {'assessments': [{'id': '0', 'reason': '理由', 'score': True, 'eligible': True}]},
    {'assessments': [{'id': '0', 'reason': '理由', 'score': float('nan'), 'eligible': True}]},
    {'assessments': []},
    {'assessments': [{'id': '0', 'reason': '理由', 'score': 90, 'eligible': 'yes'}]},
])
def test_ai_output_is_validated(payload):
    with pytest.raises(ValueError):
        cs._validate_result(payload, [{'id': '0', 'text': 'Original'}], 'filter' if 'assessments' in payload else 'translate')


def test_source_change_invalidates_translation_and_filter_not_confirmed_snapshot(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    cs.run(uid, 'translate')
    saved = cs.save(uid, plan_body(uid))
    def suggest(prompt, **kwargs):
        return validated_result({'assessments': [{'id': row['id'], 'eligible': row['text'] == 'Original opinion 1', 'score': 90 if row['text'] == 'Original opinion 1' else 20, 'reason': '具体体验'} for row in request_payload(prompt)['comments']]}, kwargs)
    monkeypatch.setattr(cs, 'chat_validated', suggest)
    state = cs.run(uid, 'filter', prompt='真实体验', limit=1)
    assert state['filter']['items'][0]['id'] == '1'
    assert state['overlay'] == saved['overlay']
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    doc = json.loads(path.read_text()); doc['comments']['items'][1]['text'] = 'Changed original'
    atomic_write_bytes(path, json.dumps(doc).encode())
    state = cs.view(uid)
    assert state['rows'][0]['text_zh'] and not state['rows'][1]['text_zh']
    assert state['filter_stale'] and state['overlay'] == saved['overlay']
    # 已确认快照可继续编辑，即使下一次平台采样已经不包含这条评论。
    seed(uid, 0)
    body = {'items': saved['overlay']['items'], 'settings': saved['overlay']['settings'], 'revision': state['overlay_revision']}
    assert len(cs.save(uid, body)['overlay']['items']) == 3


def test_every_comment_scored_once_limit_change_reuses_cache_and_keeps_all_reasons(uid, monkeypatch):
    calls = []
    def rate(prompt, **kwargs):
        calls.append(request_payload(prompt))
        assert '必须有具体信息' in kwargs['system']
        assert '宗教祝福' in kwargs['system'] and '不能为了只保留好评' in kwargs['system']
        assert '不是筛选知识资料' in kwargs['system']
        assert '所有批次使用相同标尺' in kwargs['system']
        return fake_rating(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', rate)
    first = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT, limit=1)
    assert len(calls) == 1
    assert len(first['filter']['assessments']) == 3
    assert first['filter']['items'][0]['id'] == '2'
    assert {row['outcome'] for row in first['filter']['assessments']} == {'selected', 'excluded', 'limit'}
    second = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT, limit=50)
    assert len(calls) == 1 and second['job']['total'] == 0
    assert [row['id'] for row in second['filter']['items']] == ['2', '1']
    assert second['overlay'] is None
    # 原文、标准不变时，无需 LLM；新增评论只补一条。
    seed(uid, 4)
    third = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT, limit=50)
    assert len(calls) == 2 and [row['text'] for row in calls[-1]['comments']] == ['Original opinion 3']
    assert third['job']['total'] == 1
    cs.run(uid, 'filter', prompt='只看有理有据的批评', limit=50)
    assert len(calls) == 3 and len(calls[-1]['comments']) == 4
    cs.run(uid, 'filter', prompt='只看有理有据的批评', limit=50, force=True)
    assert len(calls) == 4


def test_legacy_filter_fingerprint_is_not_mistaken_for_source_changes(uid):
    path = seed(uid, 12)
    rows = cs.view(uid)['rows']
    legacy = {'source_version': cs._hash([(row['id'], row['source_hash']) for row in rows]),
              'prompt': cs.LEGACY_FILTER_PROMPT, 'items': [{'id': '1', 'reason': '旧建议'}]}
    cs._write(crud.get_project(uid), {'filter': legacy})
    state = cs.view(uid)
    assert state['filter_outdated'] and not state['filter_stale']
    assert state['filter']['items'] == legacy['items']
    raw = json.loads(path.read_text()); raw['comments']['items'][0]['text'] = 'Updated'
    atomic_write_bytes(path, json.dumps(raw).encode())
    assert cs.view(uid)['filter_stale']


def test_platform_reordering_reuses_scores_and_only_changed_comment_is_reassessed(uid, monkeypatch):
    calls = []
    def rate(prompt, **kwargs):
        calls.append(request_payload(prompt)['comments'])
        return fake_rating(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', rate)
    first = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    raw = json.loads(path.read_text()); raw['comments']['items'].reverse()
    atomic_write_bytes(path, json.dumps(raw).encode())
    assert cs.view(uid)['source_version'] == first['source_version']
    assert not cs.view(uid)['filter_stale']
    cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)
    assert len(calls) == 1
    raw['comments']['items'][0]['text'] = 'A different opinion'
    atomic_write_bytes(path, json.dumps(raw).encode())
    second = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)
    assert len(calls) == 2 and [row['text'] for row in calls[-1]] == ['A different opinion']
    assert second['job']['total'] == 1 and len(second['filter']['assessments']) == 3


def test_scoring_failure_saves_successful_batches_for_retry(uid, monkeypatch):
    seed(uid, 12); calls = []
    def fail_second(prompt, **kwargs):
        calls.append(request_payload(prompt)['comments'])
        if len(calls) == 2:
            raise RuntimeError('模拟中断')
        return fake_rating(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', fail_second)
    with pytest.raises(cs.CommentError):
        cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)
    assert not cs.view(uid)['filter']
    assert len(cs._read(crud.get_project(uid))['rating_cache']['assessments']) == 10
    monkeypatch.setattr(cs, 'chat_validated', fake_rating)
    state = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)
    assert state['job']['total'] == 2 and len(state['filter']['assessments']) == 12


def test_bilingual_confirmation_keeps_source_and_translation_independent(uid):
    body = plan_body(uid, language_mode='bilingual', source_first=False, background_opacity=0, position='top_left')
    body['items'][0]['source_text'] = 'Short original'
    state = cs.save(uid, body)
    plan = state['overlay']; row = plan['items'][0]
    assert row['text'] == 'Original opinion 0' and row['source_text'] == 'Short original'
    assert cs.display_lines(row, plan['settings']) == ['已人工核对的中文', 'Short original']
    assert cs.display_lines(row, {**plan['settings'], 'source_first': True}) == ['Short original', '已人工核对的中文']
    ass = co.ass_document(plan, (1920, 1080))
    assert '已人工核对的中文\\NShort original' in ass
    assert '&HFF000000' in ass
    body = plan_body(uid, language_mode='bilingual'); body['items'][0]['display_text'] = ''
    with pytest.raises(cs.CommentError):
        cs.save(uid, body)
    body = plan_body(uid, language_mode='original'); body['items'][0]['display_text'] = ''
    assert cs.save(uid, body)['overlay']['items'][0]['display_text'] == ''


def test_confirmed_comment_style_is_the_project_effective_setting(uid):
    body = plan_body(uid); body['settings'] = {'position': 'top', 'start': 0}
    state = cs.save(uid, body)
    assert state['overlay']['settings']['position'] == 'top'
    assert state['overlay']['settings']['language_mode'] == 'translated'
    assert state['defaults']['position'] == 'top'


def test_cancel_identity_busy_gate_and_no_result_after_cancel(uid, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow(prompt, **kwargs):
        entered.set(); assert release.wait(5)
        kwargs['cancel_check']()
        return fake_translation(prompt, **kwargs)
    monkeypatch.setattr(cs, 'chat_validated', slow)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(cs.run, uid, 'translate')
        try:
            assert entered.wait(5)
            with pytest.raises(DubForgeError):
                cs.run(uid, 'filter', prompt='test')
            with pytest.raises(cs.CommentError) as error:
                cs.cancel(uid, 'stale-job')
            assert error.value.code == 'COMMENTS_JOB_CHANGED'
            cs.cancel(uid, cs.view(uid)['job']['id'])
        finally:
            release.set()
        with pytest.raises(cs.CommentError) as error:
            future.result(5)
        assert error.value.code == 'COMMENTS_CANCELLED'
    assert cs.view(uid)['job']['status'] == 'cancelled'
    assert not cs.view(uid)['busy']
    assert not any(r['text_zh'] for r in cs.view(uid)['rows'])


@pytest.mark.parametrize('settings', [{'duration': 20, 'interval': 15}, {'start': -1}, {'interval': 0},
    {'font_size': 100}, {'position': 'left'}, {'show_author': 'false'}, {'margin': float('inf')}, {'extra': 1}])
def test_invalid_layout_is_rejected(uid, settings):
    with pytest.raises(cs.CommentError):
        cs.save(uid, plan_body(uid, **settings))
    assert cs.view(uid)['overlay'] is None


def test_manual_confirmation_validates_source_text_and_revision(uid):
    body = plan_body(uid)
    body['items'][0]['author'] = '假冒作者'
    body['items'][0]['text'] = '假冒原文'
    state = cs.save(uid, body)
    assert state['overlay']['items'][0]['author'] == '观众'
    assert state['overlay']['items'][0]['text'] == 'Original opinion 0'
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, body)
    assert error.value.code == 'COMMENTS_PLAN_CHANGED'
    body = plan_body(uid); body['items'][0]['id'] = 'fake'
    with pytest.raises(cs.CommentError, match='原文已变化'):
        cs.save(uid, body)
    body = plan_body(uid); body['items'][0]['display_text'] = '长' * 241
    assert cs.save(uid, body)['overlay']['items'][0]['display_text'] == '长' * 241
    body = plan_body(uid); body['items'] = []
    assert cs.save(uid, body)['overlay']['items'] == []


@pytest.mark.parametrize('mode,expected', [('translated', []), ('original', ['原文 720/600 字符，超出 120 字符']),
    ('bilingual', ['原文 720/600 字符，超出 120 字符'])])
def test_overlay_limits_only_apply_to_display_language(uid, mode, expected):
    item = {'display_text':'中' * 219, 'source_text':'a' * 720}
    settings = {**cs.DEFAULT_SETTINGS, 'language_mode':mode}
    assert cs.overlay_text_issues(item, settings) == expected
    assert item['source_text'] == 'a' * 720
    assert cs.view(uid)['text_limits'] == dict(translated=240, original=600)
    if expected:
        with pytest.raises(cs.CommentError, match='超出 120 字符'):
            cs.display_lines(item, settings)
    else:
        assert cs.display_lines(item, settings) == ['中' * 219]


def test_overlay_unicode_boundaries_and_missing_text():
    settings = {**cs.DEFAULT_SETTINGS, 'language_mode':'bilingual'}
    assert not cs.overlay_text_issues(dict(display_text=' 🎉' * 120, source_text='a' * 600), settings)
    assert not cs.overlay_text_issues(dict(display_text='🎉' * 240, source_text='a' * 600), settings)
    assert cs.overlay_text_issues(dict(display_text='🎉' * 241, source_text='\n '), settings) == [
        '中文 241/240 字符，超出 1 字符', '原文上屏文字为空']


def test_failed_atomic_save_keeps_confirmed_document(uid, monkeypatch):
    state = cs.save(uid, plan_body(uid))
    path = cs._path(crud.get_project(uid)); before = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('模拟磁盘写入失败')
    monkeypatch.setattr(cs, 'atomic_write_bytes', fail)
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, plan_body(uid, start=10))
    assert error.value.code == 'COMMENTS_SAVE_FAILED'
    assert path.read_bytes() == before and cs.view(uid)['overlay'] == state['overlay']


@pytest.mark.parametrize('doc', [{'translations': []}, {'filter': {'items': [None]}}, {'overlay': {'items': [], 'settings': []}}])
def test_corrupt_document_reports_stable_error(uid, doc):
    atomic_write_bytes(cs._path(crud.get_project(uid)), json.dumps(doc).encode())
    response = TestClient(app).get(f'/api/projects/{uid}/comments')
    assert response.status_code == 400 and response.json()['code'] == 'COMMENTS_DOCUMENT_INVALID'


def test_opt_in_and_task_snapshot_do_not_follow_later_edits(uid, monkeypatch):
    state = cs.save(uid, plan_body(uid))
    assert cs.for_task(uid) is None
    for enabled, rev, variants in [(False, None, ['src_comments']), ('true', state['overlay_revision'], []), (True, None, [])]:
        with pytest.raises(cs.CommentError):
            cs.for_task(uid, enabled, rev, variants)
    tasks = []
    monkeypatch.setattr(runner, '_start_step', lambda *a, **k: tasks.append(k['task_data']) or {'status': 'started'})
    runner.start_assemble_outputs(uid, ['src_comments'], include_comments=True, overlay_revision=state['overlay_revision'])
    runner.start_assemble_preview(uid, 1, 3, include_comments=True, overlay_revision=state['overlay_revision'])
    body = plan_body(uid); body['items'][0]['display_text'] = '修改后的内容'
    cs.save(uid, body)
    assert all(task['comment_overlay']['items'][0]['display_text'] == '已人工核对的中文' for task in tasks)
    with pytest.raises(cs.CommentError) as error:
        runner.start_assemble_outputs(uid, ['src_comments'], include_comments=True, overlay_revision=state['overlay_revision'])
    assert error.value.code == 'COMMENTS_PLAN_CHANGED'


def test_ass_escapes_commands_and_clips_original_timeline():
    plan = {'settings': {**cs.DEFAULT_SETTINGS, 'start': 5, 'interval': 15}, 'items': [
        {'display_text': '{\\pos(0,0)}中文\n评论'}, {'display_text': '第二条'}]}
    text = co.ass_document(plan, (1920, 1080), start=7, end=24)
    assert '{\\pos' not in text and '｛＼pos' in text
    assert '0:00:00.00,0:00:04.00' in text
    assert '0:00:13.00,0:00:17.00' in text
    assert text.count('Dialogue:') == 2
    assert 'Dialogue:' not in co.ass_document(plan, (1920, 1080), start=40, end=45)
    portrait = copy.deepcopy(plan); portrait['items'] = [{'display_text': '长' * 240}]
    assert '长' in co.ass_document(portrait, (608, 1080))


def test_comments_api_never_auto_queues_and_errors_preserve_state(uid, monkeypatch):
    api = TestClient(app)
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    result = api.post(f'/api/projects/{uid}/comments/translate')
    assert result.status_code == 200 and result.json()['rows'][0]['text_zh']
    response = api.put(f'/api/projects/{uid}/comments/overlay', json=plan_body(uid))
    assert response.status_code == 200
    assert crud.latest_job(crud.get_project(uid).id, 'assemble') is None
    bad = api.post(f'/api/projects/{uid}/steps/assemble/output_plan', json={'variants': ['src_comments']})
    assert bad.status_code == 400 and bad.json()['code'] == 'COMMENTS_NOT_ENABLED'
    plan = api.post(f'/api/projects/{uid}/steps/assemble/output_plan', json={
        'variants': ['src_comments'], 'include_comments': True, 'overlay_revision': response.json()['overlay_revision']})
    assert plan.status_code == 200 and plan.json()['comments_count'] == 3
    assert api.get(f'/api/projects/{uid}/comments').headers['cache-control'] == 'no-store'


def test_original_comment_movie_needs_neither_tts_nor_subtitles(make_project):
    from test_assemble import _make_fixture_media
    uid = make_project(); seed(uid)
    root = crud.project_dir(uid); _make_fixture_media(root)
    state = cs.save(uid, plan_body(uid))
    assert [(step, name) for step, name, _ in Dependencies(crud.get_project(uid), assemble_variants=['src_comments']).inputs('assemble')] == [('download', 'video.mp4')]
    # 本地夹具将视频登记为有效下载产物。
    project = crud.get_project(uid)
    video = root / 'media/video.mp4'
    crud.add_artifact(project.id, 'download', 'video', 'video.mp4', video, 'media/video.mp4')
    from conftest import mark_upstream_done
    mark_upstream_done(uid, 'preprocess')
    runner.start_assemble_outputs(uid, ['src_comments'], include_comments=True, overlay_revision=state['overlay_revision'])
    wait_status(uid, 'assemble', {'done'}, timeout=30)
    assert len(list((root / 'output').glob('final_src_comments_*.mp4'))) == 1
    assert not list(root.rglob('.comments_*.ass'))
    assert not (root / 'audio/dub_full.wav').exists()
    assert crud.latest_job(project.id, 'tts') is None
    assert not Dependencies(project).fresh('assemble')
    assert json.loads(crud.latest_job(project.id, 'assemble').task_data)['comment_overlay'] == state['overlay']
    active = co.preview_frame(video, state['overlay'], 1)
    empty = copy.deepcopy(state['overlay']); empty['settings']['start'] = 4
    hidden = co.preview_frame(video, empty, 1)
    assert active.startswith(b'\x89PNG') and active != hidden


def test_comments_and_subtitles_share_full_and_preview_rendering(make_project, monkeypatch):
    from test_assemble import _prepare_outputs_project
    uid, root = _prepare_outputs_project(make_project, monkeypatch); seed(uid)
    state = cs.save(uid, plan_body(uid))
    runner.start_step(uid, 'tts'); wait_status(uid, 'tts', {'done'})
    runner.start_assemble_outputs(uid, ['dub', 'dub_hardsub', 'src_hardsub'], include_comments=True, overlay_revision=state['overlay_revision'])
    wait_status(uid, 'assemble', {'done'}, timeout=30)
    assert len(list((root / 'output').glob('final_*_comments_*.mp4'))) == 3
    assert not Dependencies(crud.get_project(uid)).fresh('assemble')
    runner.start_assemble_preview(uid, 1, 4, include_comments=True, overlay_revision=state['overlay_revision'])
    wait_status(uid, 'assemble', {'done'}, timeout=30)
    assert len(list((root / 'output').glob('preview_comments_*.mp4'))) == 1
    assert not list(root.rglob('.comments_*.ass'))
    runner.start_assemble_outputs(uid, ['dub'])
    wait_status(uid, 'assemble', {'done'}, timeout=30)
    assert not json.loads(crud.latest_job(crud.get_project(uid).id, 'assemble').task_data).get('comment_overlay')
    assert len(list((root / 'output').glob('final_*.mp4'))) == 4
