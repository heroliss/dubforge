"""自适应翻译回归：隔离数据根并模拟模型，不读取或改写真实项目。"""
import json
import hashlib
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from dubforge.backends import llm
from dubforge.errors import StepFailedError
from dubforge.pipeline import step_translate as st
from dubforge.runner import StepCancelled


@pytest.fixture
def context(isolated_env, tmp_path, monkeypatch):
    assert Path(st.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    logs, traces = [], []
    monkeypatch.setattr(llm, 'describe_selection', lambda _: '隔离模拟模型')
    return SimpleNamespace(force=False, pdir=tmp_path / 'project',
        cfg=lambda key, default=None: 200_000 if key == 'translate.max_context_tokens' else default,
        check_cancel=lambda: None, check_pause=lambda: None,
        log=logs.append, llm_trace=lambda event, content='', **meta: traces.append((event, content, meta)),
        logs=logs, traces=traces)


def lines(first=1, count=8):
    return [dict(id=i, text=f'source-{i}', start=i * 2.0, end=i * 2.0 + 1.25)
            for i in range(first, first + count)]


def request(prompt='', **kwargs):
    messages = kwargs.get('messages')
    text = messages[-1]['content'] if messages else prompt
    phase = 'review' if '【待翻译原文与候选译文】' in text else 'translate'
    marker = ('【待翻译原文与候选译文】' if phase == 'review' else
              '【待重译片段】' if '【待重译片段】' in text else '【待翻译原文】')
    body = text.split(marker, 1)[1].split('【', 1)[0]
    ids = [int(m.group(1)) for m in re.finditer(r'^(\d+)\. source-\d+', body, re.MULTILINE)]
    return dict(text=text, ids=tuple(ids), phase=phase, fresh=messages is None,
                handoff='译文后另起一行输出交接标记' in text)


def output(info, prefix='新译', summary=None):
    body = '\n'.join(f'{i}. {prefix}{i}' for i in info['ids'])
    if info['handoff']:
        body += f'\n{st.HANDOFF_MARKER}\n' + (summary or f'成功交接{info["ids"][-1]}')
    return body


def install(monkeypatch, reply):
    calls, attempts = [], Counter()

    def chat(prompt='', **kwargs):
        info = request(prompt, **kwargs)
        calls.append(info)
        key = (info['phase'], info['ids'])
        attempts[key] += 1
        return reply(info, attempts[key])

    monkeypatch.setattr(llm, 'chat', chat)
    return calls, attempts


def caches(context):
    return [json.loads(path.read_text(encoding='utf-8'))
            for path in sorted((context.pdir / 'cache' / 'translate').glob('*.json'))]


def smallest_fitting_context(context, monkeypatch, budget_inputs):
    """按真实预算函数找临界值，避免测试依赖具体提示词长度。"""
    limit = [200_000]
    monkeypatch.setattr(context, 'cfg', lambda key, default=None:
                        limit[0] if key == 'translate.max_context_tokens' else default)
    low, high = 1, 200_000
    while low < high:
        limit[0] = (low + high) // 2
        if all(st._full_context_retry_fits(context, *inputs) for inputs in budget_inputs):
            high = limit[0]
        else:
            low = limit[0] + 1
    limit[0] = low
    return low


def test_original_valid_window_stays_one_request_and_keeps_v6_cache(context, monkeypatch):
    chunk = lines()
    calls, _ = install(monkeypatch, lambda info, _: output(info))
    result, summary = st._translate_window(context, chunk, '系统', '', 1, 1)
    assert result == {i: f'新译{i}' for i in range(1, 9)} and summary == ''
    assert len(calls) == 1 and len(caches(context)) == 1
    again = st._translate_window(context, chunk, '系统', '', 1, 1)
    assert again == (result, summary) and len(calls) == 1
    assert not any(meta.get('mode') == 'adaptive' for _, _, meta in context.traces)


def test_structure_failure_retranslates_children_and_preserves_global_ids(context, monkeypatch):
    chunk = lines(193, 8)
    full = tuple(line['id'] for line in chunk)
    calls, attempts = install(monkeypatch, lambda info, _: '没有编号的失败响应'
                              if info['ids'] == full else output(info))
    result, summary = st._translate_window(context, chunk, '系统', '上一个窗口摘要', 1, 1)
    assert list(result) == list(full)
    assert all(text.startswith('新译') for text in result.values())
    assert attempts[('translate', full)] == 3 and len(calls) == 7
    translated = [row for row in calls if row['phase'] == 'translate']
    reviewed = [row for row in calls if row['phase'] == 'review']
    assert len(reviewed) == 2 and all(row['ids'] != full for row in reviewed)
    left, right = translated[3], translated[4]
    assert left['ids'] + right['ids'] == full
    assert left['handoff'] is True and right['handoff'] is False and summary == ''
    assert f'成功交接{left["ids"][-1]}' in right['text']
    assert '上一个窗口摘要' in left['text']
    assert '仅供理解，无需返回' in left['text']
    assert '不从 1 重新编号' in right['text']
    assert all('[时长 1.25 秒]' in row['text'] for row in (left, right))
    assert len(caches(context)) == 3


def test_nonfinal_parent_gets_handoff_from_last_successful_child(context, monkeypatch):
    chunk = lines()
    full = tuple(line['id'] for line in chunk)
    calls, _ = install(monkeypatch, lambda info, _: '协议失败' if info['ids'] == full
                        else output(info, summary=f'交接到{info["ids"][-1]}'))
    result, summary = st._translate_window(context, chunk, '系统', '', 2, 3)
    assert len(result) == len(chunk)
    translated = [row for row in calls if row['phase'] == 'translate']
    assert translated[3]['handoff'] and translated[4]['handoff']
    assert summary == f'交接到{chunk[-1]["id"]}'
    assert f'交接到{translated[3]["ids"][-1]}' in translated[4]['text']


@pytest.mark.parametrize('exception', [StepCancelled('模拟取消'),
                                     StepFailedError('模拟网络失败', code='LLM_REQUEST_FAILED')])
def test_cancel_or_network_failure_never_starts_adaptive_children(context, monkeypatch, exception):
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        raise exception

    monkeypatch.setattr(llm, 'chat', chat)
    with pytest.raises(type(exception)) as caught:
        st._translate_window(context, lines(), '系统', '', 1, 1)
    assert caught.value is exception and len(calls) == 1
    assert not caches(context)
    assert not any(meta.get('mode') == 'adaptive' for _, _, meta in context.traces)


def test_empty_translation_failure_is_not_relabeled_as_structural_split(context, monkeypatch):
    calls, _ = install(monkeypatch, lambda info, _: '\n'.join(f'{i}. ' for i in info['ids']))
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, lines(), '系统', '', 1, 1)
    assert caught.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 3
    assert not caches(context)
    assert not any(meta.get('mode') == 'adaptive' for _, _, meta in context.traces)


def test_last_missing_error_does_not_split_after_earlier_protocol_error(context, monkeypatch):
    calls, _ = install(monkeypatch, lambda info, attempt: '无编号响应' if attempt == 1
                        else '\n'.join(f'{i}. ' for i in info['ids']))
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, lines(), '系统', '', 1, 1)
    assert caught.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 3
    assert not any(meta.get('mode') == 'adaptive' for _, _, meta in context.traces)


def test_depth_is_bounded_at_two_and_single_line_is_not_split(context, monkeypatch):
    calls, _ = install(monkeypatch, lambda _info, _attempt: '无编号响应')
    with pytest.raises(StepFailedError):
        st._translate_window(context, lines(), '系统', '', 1, 1)
    assert len(calls) == 9
    assert [len(calls[i]['ids']) for i in (0, 3, 6)] == [8, 4, 2]
    assert not caches(context)
    calls.clear()
    with pytest.raises(StepFailedError):
        st._translate_window(context, lines(316, 1), '系统', '', 1, 1)
    assert len(calls) == 3 and {row['ids'] for row in calls} == {(316,)}


def test_full_adaptive_tree_never_exceeds_seven_windows_thirty_three_rounds(context, monkeypatch):
    calls, attempts = install(monkeypatch, lambda info, attempt: '结构无效'
        if len(info['ids']) > 2 or attempt < 3 else output(info))
    result, _ = st._translate_window(context, lines(), '系统', '', 1, 1)
    assert len(result) == 8
    assert len(calls) == 33
    assert sum(phase == 'translate' for phase, _ in attempts) == 7
    assert sum(phase == 'review' for phase, _ in attempts) == 4
    assert sum(row['phase'] == 'translate' for row in calls) == 21
    assert sum(row['phase'] == 'review' for row in calls) == 12
    assert all(count == 3 for count in attempts.values())
    assert len(caches(context)) == 7


def test_left_success_right_failure_never_caches_partial_parent(context, monkeypatch):
    chunk = lines()
    full = tuple(line['id'] for line in chunk)

    def reply(info, _attempt):
        if info['ids'] == full or info['ids'][0] > 4:
            return '协议无效'
        return output(info)

    calls, _ = install(monkeypatch, reply)
    with pytest.raises(StepFailedError):
        st._translate_window(context, chunk, '系统', '', 1, 1)
    stored = caches(context)
    assert len(stored) == 1 and sorted(map(int, stored[0]['lines'])) == [1, 2, 3, 4]
    assert not (context.pdir / 'artifacts' / 'translate' / 'translate.txt').exists()
    assert len(calls) <= 33


def test_reference_neighbor_output_is_rejected_and_not_merged(context, monkeypatch):
    chunk, neighbor = lines(10, 2), lines(9, 1)
    calls, _ = install(monkeypatch, lambda info, _: '9. 多余的邻居译文\n' + output(info))
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, chunk, '系统', '', 1, 1,
                             depth=st.MAX_ADAPTIVE_DEPTH, final_window=True, reference_lines=neighbor)
    assert caught.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 3
    assert not caches(context)


def test_adaptive_context_and_actual_handoff_have_distinct_cache_keys(context, monkeypatch):
    chunk, neighbor = lines(10, 2), lines(9, 1)
    calls, _ = install(monkeypatch, lambda info, _: output(info))
    st._translate_window(context, chunk, '系统', '', 1, 1,
                         depth=1, final_window=True, reference_lines=neighbor)
    st._translate_window(context, chunk, '系统', '', 1, 1,
                         depth=1, final_window=True, reference_lines=neighbor)
    assert len(calls) == 2
    changed = [{**neighbor[0], 'text': 'changed-source-9'}]
    st._translate_window(context, chunk, '系统', '', 1, 1,
                         depth=1, final_window=True, reference_lines=changed)
    assert len(calls) == 4
    st._translate_window(context, chunk, '系统', '', 1, 1,
                         depth=1, final_window=False, reference_lines=changed)
    assert len(calls) == 6 and calls[-1]['handoff']
    assert len(caches(context)) == 3


def test_last_uncategorized_validation_failure_cannot_reuse_earlier_typed_reason(context, monkeypatch):
    # 超长数字编号触发 Python int 的 ValueError；必须以最后一轮真实错误判断。
    too_large_id = '9' * 5000 + '. 无法解析的编号'
    calls, _ = install(monkeypatch, lambda info, attempt: '无编号响应' if attempt == 1
                        else too_large_id if len(info['ids']) == 8 else output(info))
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, lines(), '系统', '', 1, 1)
    assert caught.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 3
    assert not any(meta.get('mode') == 'adaptive' for _, _, meta in context.traces)


MISSING = object()


@pytest.mark.parametrize('invalid_summary', [
    pytest.param(MISSING, id='missing'), pytest.param(None, id='null'),
    pytest.param('', id='empty'), pytest.param({'错误类型': '不是摘要'}, id='non-string'),
    pytest.param('第一行\n第二行', id='multiline'), pytest.param('x' * 2001, id='too-long')])
def test_nonfinal_adaptive_cache_requires_valid_handoff(context, monkeypatch, invalid_summary):
    chunk = lines(10, 2)
    calls, _ = install(monkeypatch, lambda info, _: output(info, summary='合法的成功交接摘要'))
    result, summary = st._translate_window(context, chunk, '系统', '', 1, 1,
                                           depth=1, final_window=False)
    assert summary == '合法的成功交接摘要' and len(calls) == 2
    cache = next((context.pdir / 'cache' / 'translate').glob('*.json'))
    entry = json.loads(cache.read_text(encoding='utf-8'))
    if invalid_summary is MISSING:
        entry.pop('handoff_summary')
    else:
        entry['handoff_summary'] = invalid_summary
    cache.write_text(json.dumps(entry, ensure_ascii=False), encoding='utf-8')
    restored = st._translate_window(context, chunk, '系统', '', 1, 1,
                                    depth=1, final_window=False)
    assert restored == (result, summary) and len(calls) == 4


@pytest.mark.parametrize('final_window', [True, False])
def test_adaptive_missing_repair_keeps_external_boundary_but_only_requests_missing_id(
        context, monkeypatch, final_window):
    chunk, neighbors = lines(10, 2), lines(9, 1) + lines(12, 1)

    def reply(info, attempt):
        if info['phase'] == 'translate' and info['ids'] == (10, 11) and attempt == 1:
            partial = {**info, 'ids': (11,)}
            return output(partial, summary='本窗口的成功交接摘要')
        return output(info, summary='本窗口的成功交接摘要')

    calls, _ = install(monkeypatch, reply)
    result, summary = st._translate_window(context, chunk, '系统', '', 1, 1,
        depth=1, final_window=final_window, reference_lines=neighbors)
    assert result == {10: '新译10', 11: '新译11'} and len(calls) == 3
    assert calls[1]['ids'] == (10,) and calls[1]['handoff'] is False
    assert 'source-9' in calls[1]['text'] and 'source-12' in calls[1]['text']
    assert '仅供' in calls[1]['text'] and '无需返回' in calls[1]['text']
    assert '待返回行号：10' in calls[1]['text']
    assert summary == ('' if final_window else '本窗口的成功交接摘要')


@pytest.mark.parametrize('final_window', [True, False])
def test_review_corrects_candidate_before_cache_and_pairs_each_source(context, monkeypatch, final_window):
    chunk = lines(10, 2)

    def reply(info, _attempt):
        if info['phase'] == 'review':
            assert not caches(context), '未校对候选不能提前缓存'
            assert '10. source-10 [时长 1.25 秒]\n当前译文：候选错译10' in info['text']
            assert '11. source-11 [时长 1.25 秒]\n当前译文：候选错译11' in info['text']
            return output(info, prefix='已校对', summary='校对后的交接摘要')
        return output(info, prefix='候选错译', summary='未经校对的候选摘要')

    calls, _ = install(monkeypatch, reply)
    result, summary = st._translate_window(context, chunk, '系统', '先前语境', 1, 1,
                                           depth=1, final_window=final_window)
    assert result == {10: '已校对10', 11: '已校对11'}
    assert summary == ('' if final_window else '校对后的交接摘要')
    assert len(calls) == 2 and calls[1]['phase'] == 'review' and calls[1]['fresh']
    assert calls[1]['handoff'] is not final_window and '先前语境' in calls[1]['text']
    stored = caches(context)
    assert len(stored) == 1 and stored[0]['lines'] == {'10': '已校对10', '11': '已校对11'}
    assert stored[0]['handoff_summary'] == summary


@pytest.mark.parametrize('invalid', ['missing', 'empty', 'duplicate', 'out-of-order'])
def test_review_invalid_output_fails_without_recursive_split_or_cache(context, monkeypatch, invalid):
    chunk = lines(10, 2)

    def reply(info, _attempt):
        if info['phase'] != 'review':
            return output(info, prefix='候选')
        return {'missing': '10. 仅一行', 'empty': '10. \n11. 十一',
                'duplicate': '10. 甲\n10. 乙\n11. 十一',
                'out-of-order': '11. 十一\n10. 十'}[invalid]

    calls, _ = install(monkeypatch, reply)
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, chunk, '系统', '', 1, 1, depth=1, final_window=True)
    assert caught.value.code == 'LLM_OUTPUT_INVALID'
    assert len(calls) == 4 and sum(row['phase'] == 'translate' for row in calls) == 1
    reviews = [row for row in calls if row['phase'] == 'review']
    assert all(row['fresh'] and row['ids'] == (10, 11) for row in reviews)
    assert not caches(context)
    assert not any(meta.get('mode') == 'adaptive' for _, _, meta in context.traces)


def test_review_complementary_partial_responses_cannot_be_merged(context, monkeypatch):
    def reply(info, attempt):
        if info['phase'] == 'translate':
            return output(info)
        return '11. 十一' if attempt == 2 else '10. 十'

    calls, _ = install(monkeypatch, reply)
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, lines(10, 2), '系统', '', 1, 1, depth=1, final_window=True)
    assert caught.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 4
    assert not caches(context)
    assert all(row['ids'] == (10, 11) for row in calls)


@pytest.mark.parametrize('exception', [StepCancelled('校对期间模拟取消'),
                                     StepFailedError('校对网络失败', code='LLM_REQUEST_FAILED')])
def test_review_cancel_or_network_failure_does_not_store_candidate(context, monkeypatch, exception):
    def reply(info, _attempt):
        if info['phase'] == 'review':
            raise exception
        return output(info)

    calls, _ = install(monkeypatch, reply)
    with pytest.raises(type(exception)) as caught:
        st._translate_window(context, lines(10, 2), '系统', '', 1, 1, depth=1, final_window=True)
    assert caught.value is exception and len(calls) == 2
    assert not caches(context)


@pytest.mark.parametrize('old_stage', ['window-adaptive-v2', 'window-adaptive-v3'])
def test_old_adaptive_cache_does_not_bypass_new_review(context, monkeypatch, old_stage):
    chunk = lines(10, 2)
    system_hash = hashlib.sha256('系统'.encode('utf-8')).hexdigest()
    signature = json.dumps({'depth': 1, 'final': True, 'reference': []},
                           ensure_ascii=False, sort_keys=True)
    extra = f'1/1||system:{system_hash}|adaptive:' + hashlib.sha256(signature.encode('utf-8')).hexdigest()
    old_key = st._chunk_key(context, chunk, old_stage, extra)
    st._store(context, old_key, {'lines': {'10': '旧版候选十', '11': '旧版候选十一'}, 'handoff_summary': ''})
    calls, _ = install(monkeypatch, lambda info, _: output(info,
        prefix='新校对' if info['phase'] == 'review' else '新候选'))
    result, _ = st._translate_window(context, chunk, '系统', '', 1, 1, depth=1, final_window=True)
    assert result == {10: '新校对10', 11: '新校对11'} and len(calls) == 2
    assert calls[1]['phase'] == 'review'


@pytest.mark.parametrize('count', [81, 316])
@pytest.mark.parametrize('final_window', [True, False])
def test_large_recovery_uses_contiguous_at_most_forty_line_windows_and_reviewed_handoff(
        context, monkeypatch, count, final_window):
    chunk = lines(193, count)
    full = tuple(line['id'] for line in chunk)

    def reply(info, _attempt):
        if info['ids'] == full:
            return '整窗结构无效'
        prefix = '校对' if info['phase'] == 'review' else '候选'
        return output(info, prefix=prefix, summary=f'{prefix}交接{info["ids"][-1]}')

    calls, _ = install(monkeypatch, reply)
    result, summary = st._translate_window(context, chunk, '系统', '前窗交接', 2, 3,
                                           final_window=final_window)
    translated = [row for row in calls[3:] if row['phase'] == 'translate']
    reviewed = [row for row in calls if row['phase'] == 'review']
    expected = [full[start:start + 40] for start in range(0, count, 40)]
    assert [row['ids'] for row in translated] == expected
    assert [row['ids'] for row in reviewed] == expected
    assert len(calls) == 3 + 2 * len(expected)
    assert list(result) == list(full) and all(text.startswith('校对') for text in result.values())
    assert all(1 <= len(row['ids']) <= 40 for row in translated + reviewed)
    for index, (candidate, review) in enumerate(zip(translated, reviewed)):
        handoff = not (final_window and index == len(expected) - 1)
        assert candidate['handoff'] is handoff and review['handoff'] is handoff
        assert review['fresh'] and '仅供理解，无需返回' in review['text']
        if index:
            previous_id = expected[index - 1][-1]
            assert f'校对交接{previous_id}' in candidate['text']
            assert f'候选交接{previous_id}' not in candidate['text']
    assert summary == ('' if final_window else f'校对交接{full[-1]}')
    assert len(caches(context)) == len(expected) + 1


def test_flat_children_share_translation_and_review_budget_and_stop_before_next_request(context, monkeypatch):
    chunk = lines(1, 316)

    def reply(info, attempt):
        return '结构无效' if len(info['ids']) > 40 or attempt < 3 else output(info)

    calls, _ = install(monkeypatch, reply)
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, chunk, '系统', '', 1, 1)
    assert caught.value.code == 'LLM_OUTPUT_INVALID'
    assert '33' in str(caught.value) and '停止新增请求' in str(caught.value)
    assert len(calls) == 33
    assert sum(row['phase'] == 'translate' for row in calls) == 18
    assert sum(row['phase'] == 'review' for row in calls) == 15
    assert all(row['ids'][0] < 201 for row in calls)
    stored = caches(context)
    assert len(stored) == 5
    assert sorted(int(line_id) for entry in stored for line_id in entry['lines']) == list(range(1, 201))
    assert not any(len(entry['lines']) == 316 for entry in stored)


def test_budget_spent_by_translation_cannot_skip_required_review_or_cache_candidate(context, monkeypatch):
    chunk = lines(1, 316)

    def reply(info, attempt):
        if len(info['ids']) > 40:
            return '整窗结构无效'
        threshold = 3
        if info['ids'][0] == 161 and info['phase'] == 'translate':
            threshold = 2
        elif info['ids'][0] == 201:
            threshold = 1
        return '结构无效' if attempt < threshold else output(info, prefix=info['phase'])

    calls, _ = install(monkeypatch, reply)
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, chunk, '系统', '', 1, 1)
    assert caught.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 33
    assert calls[-1]['phase'] == 'translate' and calls[-1]['ids'][0] == 201
    assert not any(row['phase'] == 'review' and row['ids'][0] == 201 for row in calls)
    stored = caches(context)
    assert len(stored) == 5
    assert all(max(map(int, entry['lines'])) <= 200 for entry in stored)
    assert all(text.startswith('review') for entry in stored for text in entry['lines'].values())


def test_nested_children_share_budget_across_flat_siblings_without_reviewing_merged_parents(context, monkeypatch):
    chunk = lines(1, 316)

    def reply(info, attempt):
        return '结构无效' if len(info['ids']) > 21 or attempt < 3 else output(info)

    calls, _ = install(monkeypatch, reply)
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, chunk, '系统', '', 1, 1)
    assert caught.value.code == 'LLM_OUTPUT_INVALID' and len(calls) == 33
    reviews = [row for row in calls if row['phase'] == 'review']
    assert len(reviews) == 12 and all(len(row['ids']) <= 21 for row in reviews)
    assert all(row['ids'][0] < 81 for row in calls)
    stored = caches(context)
    assert len(stored) == 6
    parents = [entry for entry in stored if len(entry['lines']) == 40]
    assert len(parents) == 2
    assert sorted(int(line_id) for entry in parents for line_id in entry['lines']) == list(range(1, 81))
    assert not any(len(entry['lines']) == 316 for entry in stored)


def test_exhausted_explicit_budget_sends_no_model_request(context, monkeypatch):
    budget = {'remaining': 0}
    calls, _ = install(monkeypatch, lambda info, _: output(info))
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, lines(201, 40), '系统', '', 1, 1,
                             depth=1, final_window=True, recovery_budget=budget)
    assert caught.value.code == 'LLM_OUTPUT_INVALID'
    assert not calls and not caches(context) and budget['remaining'] == 0


@pytest.mark.parametrize('context_limit,prefix', [
    pytest.param(1_000, '候选', id='low-context-budget'),
    pytest.param(200_000, '字' * 100_000, id='abnormally-long-candidate')])
def test_review_context_budget_guard_never_sends_oversize_request_or_caches_candidate(
        context, monkeypatch, context_limit, prefix):
    monkeypatch.setattr(context, 'cfg', lambda key, default=None:
                        context_limit if key == 'translate.max_context_tokens' else default)
    budget = {'remaining': 5}
    calls, _ = install(monkeypatch, lambda info, _: output(info, prefix=prefix))
    with pytest.raises(StepFailedError) as caught:
        st._translate_window(context, lines(10, 2), '系统', '', 1, 1,
                             depth=1, final_window=True, recovery_budget=budget)
    assert caught.value.code == 'LLM_CONTEXT_BUDGET'
    assert '已停止发送' in str(caught.value)
    assert len(calls) == 1 and calls[0]['phase'] == 'translate'
    assert not caches(context) and budget['remaining'] == 4
    assert not any(meta.get('mode') == 'adaptive' for _, _, meta in context.traces)


def test_review_long_wrong_order_error_retries_use_bounded_fresh_prompt(context, monkeypatch):
    chunk = lines(193, 40)
    candidate = {'lines': {line['id']: f'候选{line["id"]}' for line in chunk}, 'handoff_summary': ''}
    reversed_output = output({'ids': tuple(line['id'] for line in reversed(chunk)), 'handoff': False})
    with pytest.raises(st._TranslationStructureError) as caught:
        st._parse_numbered_output(reversed_output, chunk, True)
    long_error = str(caught.value)
    assert len(long_error) > 500

    def reply(info, attempt):
        return reversed_output if attempt < 3 else output(info, prefix='校对')

    probe, _ = install(monkeypatch, reply)
    st._review_adaptive_window(context, chunk, '系统', candidate, '', True)
    prompt = probe[0]['text']
    retry_hint = probe[1]['text'][len(prompt):].lstrip('\n')
    assert retry_hint and len(retry_hint) < 100
    smallest_fitting_context(context, monkeypatch,
        [('系统', row['text'], '', '') for row in probe] + [('系统', prompt, '', retry_hint)])
    assert not st._full_context_retry_fits(context, '系统', prompt, '', long_error)

    calls, _ = install(monkeypatch, reply)
    reviewed = st._review_adaptive_window(context, chunk, '系统', candidate, '', True)
    assert reviewed['lines'] == {line['id']: f'校对{line["id"]}' for line in chunk}
    assert len(calls) == 3 and all(row['fresh'] and len(row['ids']) == 40 for row in calls)
    for row in calls:
        assert long_error not in row['text'] and '行号顺序错误:' not in row['text']
        assert st._full_context_retry_fits(context, '系统', row['text'], '', '')
    assert calls[1]['text'] == calls[2]['text'] == prompt + '\n\n' + retry_hint
    assert not caches(context)


def test_review_first_request_reserves_short_retry_hint_at_context_boundary(context, monkeypatch):
    chunk = lines(193, 40)
    candidate = {'lines': {line['id']: f'候选{line["id"]}' for line in chunk}, 'handoff_summary': ''}

    def reply(info, attempt):
        if attempt == 1:
            return output({**info, 'ids': tuple(reversed(info['ids']))})
        return output(info)

    probe, _ = install(monkeypatch, reply)
    st._review_adaptive_window(context, chunk, '系统', candidate, '', True)
    prompt = probe[0]['text']
    retry_hint = probe[1]['text'][len(prompt):].lstrip('\n')
    assert retry_hint
    smallest_fitting_context(context, monkeypatch, [('系统', prompt, '', '')])
    assert st._full_context_retry_fits(context, '系统', prompt, '', '')
    assert not st._full_context_retry_fits(context, '系统', prompt, '', retry_hint)

    calls, _ = install(monkeypatch, reply)
    budget = {'remaining': 5}
    with pytest.raises(StepFailedError) as caught:
        st._review_adaptive_window(context, chunk, '系统', candidate, '', True, recovery_budget=budget)
    assert caught.value.code == 'LLM_CONTEXT_BUDGET'
    assert not calls and not caches(context) and budget['remaining'] == 5
