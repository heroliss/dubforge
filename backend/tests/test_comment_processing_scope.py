"""局部评论处理保留其他产物，失败范围与实际请求保持一致。"""
import pytest
import time

from dubforge import comment_studio as cs
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from test_comment_studio import seed, fake_rating, fake_translation, replace_comment_text


@pytest.fixture
def uid(make_project):
    uid = make_project()
    seed(uid)
    return uid


@pytest.mark.parametrize('ids,code', [
    ([], 'COMMENTS_SCOPE_INVALID'), (['0', '0'], 'COMMENTS_SCOPE_INVALID'),
    ([True], 'COMMENTS_SCOPE_INVALID'), ('0', 'COMMENTS_SCOPE_INVALID'),
    (['missing'], 'COMMENTS_SCOPE_CHANGED'),
])
def test_scope_is_validated_before_any_request(uid, monkeypatch, ids, code):
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('非法范围不能请求模型'))
    with pytest.raises(cs.CommentError) as exc:
        cs.run(uid, 'translate', ids=ids)
    assert exc.value.code == code


def test_partial_forced_rating_preserves_other_valid_ratings(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', fake_rating)
    cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)
    before = cs._read(crud.get_project(uid))['rating_cache']['assessments']
    state = cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT, ids=['1'], force=True)
    assert state['job']['total'] == 1 and state['rating_progress']['saved'] == 3
    after = cs._read(crud.get_project(uid))['rating_cache']['assessments']
    assert [r for r in after if r['id'] != '1'] == [r for r in before if r['id'] != '1']
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('完整评分均应复用'))
    assert cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)['job']['cached'] == 3


def test_changed_rules_do_not_restore_out_of_scope_old_ratings(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', fake_rating)
    cs.run(uid, 'filter', prompt=cs.FILTER_PROMPT)
    cs.run(uid, 'filter', prompt='新的评分要求', ids=['1'])
    assert [r['id'] for r in cs._read(crud.get_project(uid))['rating_cache']['assessments']] == ['1']


def test_only_requested_translations_are_generated_and_cached(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    state = cs.run(uid, 'translate', ids=['2'])
    assert [r['id'] for r in state['rows'] if r['text_zh']] == ['2']
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('已有译文不必请求'))
    state = cs.run(uid, 'translate', ids=['2'])
    assert state['job']['cached'] == 1 and state['job']['total'] == 0


def test_rejected_scope_returns_partial_for_replenishment_without_fake_text(uid, monkeypatch):
    def rejected(*a, **kw):
        raise DubForgeError('模型拒绝本条', code='LLM_CONTENT_REJECTED')
    monkeypatch.setattr(cs, 'chat_validated', rejected)
    state = cs.run(uid, 'translate', ids=['1'], failure_policy='skip')
    assert state['job']['status'] == 'partial' and state['job']['skipped'] == 1
    assert state['rows'][1]['text_zh'] == ''
    assert state['rows'][1]['ai_issues']['translate']['status'] == 'blocked'
    with pytest.raises(DubForgeError):
        cs.run(uid, 'translate', ids=['0'], failure_policy='stop')


def test_scope_never_swallows_service_failure(uid, monkeypatch):
    def unavailable(*a, **kw):
        raise DubForgeError('服务暂时不可用', code='LLM_CONNECTION')
    monkeypatch.setattr(cs, 'chat_validated', unavailable)
    with pytest.raises(DubForgeError) as exc:
        cs.run(uid, 'translate', ids=['1'], failure_policy='skip')
    assert exc.value.code == 'LLM_CONNECTION'
    assert cs.view(uid)['job']['status'] == 'failed'


def test_expired_shared_budget_stops_before_the_next_candidate_request(uid, monkeypatch):
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('阶段时限耗尽不能再请求'))
    with pytest.raises(DubForgeError) as exc:
        cs.run(uid, 'translate', ids=['1'], budget_deadline=time.monotonic()-1)
    assert exc.value.code == 'COMMENTS_TIMEOUT'
    assert cs.view(uid)['job']['processed'] == 0


def test_single_oversized_comment_keeps_original_and_does_not_block_others(uid, monkeypatch):
    path = replace_comment_text(uid, '0', 'x' * 12001)
    source = path.read_bytes()
    monkeypatch.setattr(cs, 'chat_validated', fake_translation)
    state = cs.run(uid, 'translate', failure_policy='skip')
    assert state['job']['processed'] == 2 and state['job']['skipped'] == 1
    assert state['rows'][0]['ai_issues']['translate']['code'] == 'COMMENTS_TOO_LONG'
    assert state['rows'][0]['text_zh'] == '' and path.read_bytes() == source


def test_strict_policy_stops_before_oversized_comment_requests(uid, monkeypatch):
    replace_comment_text(uid, '0', 'x' * 12001)
    monkeypatch.setattr(cs, 'chat_validated', lambda *a, **kw: pytest.fail('严格模式应提前停止'))
    with pytest.raises(DubForgeError) as exc:
        cs.run(uid, 'translate', failure_policy='stop')
    assert exc.value.code == 'COMMENTS_TOO_LONG'
