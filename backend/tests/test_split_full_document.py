"""全文整理、有限重试和原始响应缓存；测试数据与生产项目隔离。"""
import json

import pytest

from dubforge.errors import StepFailedError
from dubforge.pipeline import step_split
from dubforge.runner import StepCancelled
from dubforge.utils.srt import SubLine
from test_split_semantic_quality import _ctx, _responses


def _setup(make_project, pdir, count=4):
    lines = [SubLine(i + 1, i * 3, i * 3 + 2, f'We completed task {i + 1}.')
             for i in range(count)]
    uid, ctx = _ctx(make_project, pdir, lines=[(x.start, x.end, x.text) for x in lines])
    answer = '\n'.join(f'{i}. {x.start:.2f}-{x.end:.2f}: {x.text}'
                       for i, x in enumerate(lines, 1))
    return uid, ctx, lines, answer


def test_full_document_is_sent_once_even_with_old_window_override(make_project, pdir, monkeypatch):
    from dubforge import config
    config.set_key('split.window_max_chars', 10)
    uid, ctx, lines, answer = _setup(make_project, pdir, 100)
    calls = _responses(monkeypatch, answer)
    assert len(step_split._llm_resegment(ctx, lines, 50, 15, 2, [])) == 100
    assert len(calls) == 1
    assert all(f'{x.start:.2f}-{x.end:.2f}: {x.text}' in calls[0][0] for x in lines)
    assert '窗口前上下文' not in calls[0][0]


def test_unused_character_setting_does_not_change_semantic_cache(make_project, pdir, monkeypatch):
    import threading
    from dubforge import config
    from dubforge.domain import crud
    from dubforge.runner import StepContext
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer)
    before = step_split._llm_resegment(ctx, lines, 20, 7, 0, [])
    config.set_key('split.max_split_length', 100)
    assert step_split._llm_resegment(ctx, lines, 100, 7, 0, []) == before
    assert len(calls) == 1
    config.set_key('split.target_word_count', 12)
    # 运行会固定配置快照；新设置在下一次步骤上下文中消费。
    ctx = StepContext(crud.get_project(uid), 'split', pdir(uid),
                      pdir(uid) / 'logs/split.log', threading.Event())
    assert step_split._llm_resegment(ctx, lines, 100, 7, 0, []) == before
    assert len(calls) == 2
    assert '不超过12个词、7秒' in calls[-1][0]


def test_soft_reference_warnings_do_not_count_chinese_characters_as_words():
    english = 'One two three four five six seven eight nine ten eleven'
    chinese = '这段中文包含很多汉字但自然语义词不能按单字机械计算'
    assert step_split._semantic_target_warnings([dict(text=chinese, start=0, end=7)], 10, 7) == []
    notes = step_split._semantic_target_warnings([
        dict(text=english, start=0, end=7), dict(text=chinese, start=7, end=15)], 10, 7)
    assert len(notes) == 1 and '1句超过约10词' in notes[0] and '1句超过7秒' in notes[0]
    assert '不会触发重试或机械拆分' in notes[0]


def test_uncertain_single_asr_word_deletion_warns_without_whole_document_retry(make_project, pdir, monkeypatch):
    text = 'I got the hug the duck is such a rich and flavorful bean'
    lines = [SubLine(1, 0, 6, text)]
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 6, text)])
    answer = '1. 0.00-6.00: I got the— the duck is such a rich and flavorful bean.'
    calls = _responses(monkeypatch, answer)
    final = step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert len(calls) == 1
    assert final == [dict(text=answer.split(': ', 1)[1], start=0.0, end=6.0)]
    log = (pdir(uid) / 'logs/split.log').read_text(encoding='utf-8')
    assert 'hug' in log and '无法仅凭文本确认' in log and '请结合原音核对' in log
    cached = step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert cached == final and len(calls) == 1


def test_separated_uncertain_starter_keeps_real_silence_without_full_retry(make_project, pdir, monkeypatch):
    lines = [SubLine(1, 0, 1, 'I'), SubLine(2, 7, 9, 'think this is good')]
    uid, ctx = _ctx(make_project, pdir, lines=[(x.start, x.end, x.text) for x in lines])
    words = [dict(word='I', start=0.0, end=1.0),
             dict(word='think', start=7.0, end=7.5),
             dict(word='this', start=7.5, end=8.0),
             dict(word='is', start=8.0, end=8.3),
             dict(word='good', start=8.3, end=9.0)]
    answer = '1. 0.00-1.00: I\n2. 7.00-9.00: I think this is good'
    calls = _responses(monkeypatch, answer)
    final = step_split._llm_resegment(ctx, lines, 50, 15, 2, words)
    assert len(calls) == 1
    assert final == [dict(text='I', start=0.0, end=1.0),
                     dict(text='I think this is good', start=7.0, end=9.0)]
    log = (pdir(uid) / 'logs/split.log').read_text(encoding='utf-8')
    assert '新增短词声学归属不确定' in log
    assert '已保留分句与真实空档，请复核' in log


def test_retry_keeps_full_original_and_never_expands_request_tree(make_project, pdir, monkeypatch):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    bad = answer.replace('\n2. ', '\n1. ')
    calls = _responses(monkeypatch, bad, answer)
    expected = step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert len(calls) == 2
    assert calls[0][0] in calls[1][0]
    assert calls[1][1].get('messages') is None
    assert len(list((pdir(uid) / 'cache/split').glob('*.json'))) == 1
    assert step_split._llm_resegment(ctx, lines, 50, 15, 2, []) == expected
    assert len(calls) == 2


def test_two_invalid_full_responses_fail_without_bisection(make_project, pdir, monkeypatch):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer.replace('\n2. ', '\n1. '))
    with pytest.raises(StepFailedError) as error:
        step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert error.value.code == 'LLM_OUTPUT_INVALID'
    assert len(calls) == 2
    assert not list((pdir(uid) / 'cache/split').glob('*.json'))


@pytest.mark.parametrize('error', [StepCancelled('用户取消'), StepFailedError('网络错误', code='LLM_TRANSPORT')])
def test_transport_and_cancel_do_not_add_full_requests(make_project, pdir, monkeypatch, error):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, error)
    with pytest.raises(type(error)):
        step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert len(calls) == 1
    assert not list((pdir(uid) / 'cache/split').glob('*.json'))


def test_force_bypasses_full_cache(make_project, pdir, monkeypatch):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer)
    step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    ctx.force = True
    step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert len(calls) == 2


def test_changed_source_rejects_previous_full_cache(make_project, pdir, monkeypatch):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer, answer.replace('task 1', 'job 1'))
    step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    lines[0].text = lines[0].text.replace('task', 'job')
    assert step_split._llm_resegment(ctx, lines, 50, 15, 2, [])[0]['text'] == 'We completed job 1.'
    assert len(calls) == 2


@pytest.mark.parametrize('label', ['说明', '整理说明', '处理说明', 'Notes'])
def test_explicit_trailing_explanation_does_not_require_retry(make_project, pdir, monkeypatch, label):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer + f'\n\n{label}：\n- 仅整理原台词。')
    assert len(step_split._llm_resegment(ctx, lines, 50, 15, 2, [])) == 4
    assert len(calls) == 1


@pytest.mark.parametrize('raw', [
    '1. 0.00-1.00: first\n说明：没有明确空行分隔',
    '1. 0.00-1.00: first\n\n说明：\n2. 1.00-2.00: second',
    '1. 0.00-1.00: first\n\nUnmarked untrusted text',
])
def test_ambiguous_explanation_never_discards_numbered_content(raw):
    with pytest.raises(ValueError):
        step_split._parse_timed_protocol(raw, 0, 2)


def test_response_cache_is_revalidated_against_numbered_structure(make_project, pdir, monkeypatch):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer)
    expected = step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    cache = next((pdir(uid) / 'cache/split').glob('*.json'))
    cache.write_text(json.dumps({'kind': 'response', 'raw': answer.replace('\n2. ', '\n1. ')}), encoding='utf-8')
    assert step_split._llm_resegment(ctx, lines, 50, 15, 2, []) == expected
    assert len(calls) == 2
    assert json.loads(cache.read_text(encoding='utf-8'))['raw'] == answer


def test_postprocessing_cannot_change_already_accepted_model_candidate(make_project, pdir, monkeypatch):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer)
    prepare = step_split._prepare_semantic_parts

    def change_number(*args, **kwargs):
        parts, aligned = prepare(*args, **kwargs)
        parts[0]['text'] = parts[0]['text'].replace('task 1', 'task 5')
        return parts, aligned

    monkeypatch.setattr(step_split, '_prepare_semantic_parts', change_number)
    with pytest.raises(StepFailedError) as error:
        step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert error.value.code == 'LLM_OUTPUT_INVALID'
    assert len(calls) == 2
    assert not list((pdir(uid) / 'cache/split').glob('*.json'))


@pytest.mark.parametrize('source,corrected', [
    ('The train ticket costs 10 yen.', 'The train ticket costs 10 yuan.'),
    ('The museum opened in fifteen eighty.', 'The museum opened in sixteen eighty.'),
    ('they would go until we are all on', "they won't go until we are all on"),
    ('kwa jude serves pecking duck', 'Quanjude serves Peking duck'),
    ('This is very hot.', 'This is somewhat hot.'),
])
def test_model_asr_corrections_are_accepted_once_and_not_reverted_by_postprocess(
        make_project, pdir, monkeypatch, source, corrected):
    """校验允许模型纠错；模拟响应不能证明这些修订符合真实原音。"""
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 6, source)])
    lines = [SubLine(1, 0, 6, source)]
    answer = '1. 0.00-6.00: ' + corrected
    calls = _responses(monkeypatch, answer)
    final = step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert final == [dict(text=corrected, start=0.0, end=6.0)]
    assert len(calls) == 1
    assert step_split._llm_resegment(ctx, lines, 50, 15, 2, []) == final
    assert len(calls) == 1
    cached = next((pdir(uid) / 'cache/split').glob('*.json'))
    assert json.loads(cached.read_text(encoding='utf-8'))['raw'] == answer


def test_cancel_during_cached_validation_preserves_cache(make_project, pdir, monkeypatch):
    uid, ctx, lines, answer = _setup(make_project, pdir)
    calls = _responses(monkeypatch, answer)
    step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    cache = next((pdir(uid) / 'cache/split').glob('*.json'))
    before = (cache.read_bytes(), cache.stat().st_mtime_ns)
    prepare = step_split._prepare_semantic_parts

    def then_cancel(*args, **kwargs):
        result = prepare(*args, **kwargs)
        ctx._cancel.set()
        return result

    monkeypatch.setattr(step_split, '_prepare_semantic_parts', then_cancel)
    with pytest.raises(StepCancelled):
        step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert len(calls) == 1
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before
