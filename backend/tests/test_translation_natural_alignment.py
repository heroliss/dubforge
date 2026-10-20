"""自然译文与编号保真回归：离线模型桩只验证程序契约，不证明真实模型质量。

复用仓库隔离夹具，所有资料、产物与缓存均在 tmp_path，不请求外部服务。
"""
import json
from dubforge.pipeline import step_translate as st
from dubforge.utils.lineprotocols import parse_numbered_lines, parse_timed_lines


from test_translate_full_document import context as run_context, install_model, lines, seed


def _seed_sources(context, texts):
    rows = [{**row, 'text': text} for row, text in zip(lines(len(texts)), texts)]
    seed(context, rows)
    return rows, context.source.read_bytes()


def _assert_source_and_result(context, rows, original_bytes, expected):
    assert context.source.read_bytes() == original_bytes
    # 编号、时间与原文一起检查，不能仅确认源文件名没有改变。
    assert parse_timed_lines(context.source.read_text(encoding='utf-8')) == rows
    output = context.output / 'translate.txt'
    result = parse_numbered_lines(output.read_text(encoding='utf-8'))
    assert list(result) == [row['id'] for row in rows]
    assert result == expected
    assert [row[0] for row in context.registered] == ['translate.txt', 'alignment_audit.json']
    assert context.registered[0][1] == output
    caches = list((context.pdir / 'cache' / 'translate').glob('*.json'))
    assert len(caches) == 1
    cached = json.loads(caches[0].read_text(encoding='utf-8'))
    assert cached['lines'] == {str(key): text for key, text in expected.items()}
    assert not list(context.output.glob('*.tmp'))
    return output


def test_real_run_preserves_natural_word_order_inside_the_same_source_id(run_context, monkeypatch):
    """中文行内换序不被本地程序改回直译，也不改变已定稿源编号与时间。"""
    rows, before = _seed_sources(run_context, [
        'The train finally arrived after a ten-minute wait',
        'We were not late',
    ])
    expected = {1: '等了十分钟，列车终于到了', 2: '我们没有迟到'}
    calls = install_model(monkeypatch, lambda info, _calls:
        '\n'.join(f'{key}. {expected[key]}' for key in info['ids']))

    st.run(run_context)

    assert len(calls) == 1 and calls[0]['ids'] == [1, 2]
    assert not calls[0]['review'] and not calls[0]['handoff']
    for row in rows:
        assert st._timed_source(row) in calls[0]['prompt']
    _assert_source_and_result(run_context, rows, before, expected)


def test_real_run_preserves_localized_idiom_without_inventing_a_joke(run_context, monkeypatch):
    """验证风格传入与自然候选原样落盘；模拟候选本身不是质量评分依据。"""
    style = '接地气，用自然口语表达，适当本地化'
    run_context.values['translate.style_prompt'] = style
    rows, before = _seed_sources(run_context, [
        'That was a piece of cake',
        'The ticket costs two dollars',
    ])
    expected = {1: '这事太轻松了', 2: '车票两美元'}
    calls = install_model(monkeypatch, lambda info, _calls:
        '\n'.join(f'{key}. {expected[key]}' for key in info['ids']))

    st.run(run_context)

    assert len(calls) == 1 and calls[0]['ids'] == [1, 2]
    assert style in calls[0]['system']
    assert not calls[0]['review'] and not calls[0]['handoff']
    _assert_source_and_result(run_context, rows, before, expected)


def test_missing_line_repair_preserves_accepted_natural_copy_and_reuses_complete_cache(
        run_context, monkeypatch):
    """局部补行不改写已采用的换序、币种、否定和口语；缓存完整结果可直接复用。"""
    style = '自然口语，不要照搬英文句式'
    run_context.values['translate.style_prompt'] = style
    rows, before = _seed_sources(run_context, [
        'The train finally arrived after a ten-minute wait',
        'A costs two dollars more than B',
        'It is not ready yet',
        'That was a piece of cake',
    ])
    expected = {
        1: '等了十分钟，列车终于到了',
        2: 'A比B贵两美元',
        3: '还没准备好呢',
        4: '这事太轻松了',
    }

    def reply(info, calls):
        if len(calls) == 1:
            assert info['ids'] == [1, 2, 3, 4]
            return '\n'.join(f'{key}. {expected[key]}' for key in (1, 2, 3)) + '\n4. '
        assert len(calls) == 2, '缺一行只应进行一次局部补齐，不重翻已采用行'
        assert info['ids'] == [4]
        assert expected[3] in info['prompt'], '邻句旧译文可作参考，但不再次要求返回'
        return f'4. {expected[4]}'

    calls = install_model(monkeypatch, reply)
    st.run(run_context)

    assert len(calls) == 2
    assert calls[1]['system'] == calls[0]['system'] and style in calls[1]['system']
    assert not any(call['review'] or call['handoff'] for call in calls)
    output = _assert_source_and_result(run_context, rows, before, expected)
    complete_bytes = output.read_bytes()

    # 第二次仍走真实 run()，已完成缓存不能重新请求或重新规范化已采用文字。
    st.run(run_context)

    assert len(calls) == 2
    assert output.read_bytes() == complete_bytes
    assert parse_numbered_lines(output.read_text(encoding='utf-8')) == expected
    assert run_context.source.read_bytes() == before


def test_alignment_review_keeps_an_already_correct_candidate(run_context, monkeypatch):
    rows, before = _seed_sources(run_context, ['It was not ready', 'We waited ten minutes'])
    expected = {1: '还没准备好', 2: '我们等了十分钟'}
    calls = install_model(monkeypatch, lambda info, _calls:
        '\n'.join(f'{key}. {expected[key]}' for key in info['ids']))
    candidate = {'lines': expected, 'handoff_summary': ''}
    reviewed = st._review_adaptive_window(
        run_context, rows, st._base_system(run_context, ''), candidate, '', True)
    assert len(calls) == 1 and calls[0]['review']
    assert '正确的译文保留' in calls[0]['prompt']
    assert '不能直接照抄' not in calls[0]['prompt']
    assert reviewed['lines'] == expected
    assert run_context.source.read_bytes() == before


def test_system_and_window_agree_on_when_handoff_is_required(run_context):
    system = st._base_system(run_context, '')
    assert '需要交接摘要时按指定格式附上' in system
    assert '最后一条所需内容结束即结束响应' in system
    first = st._window_prompt(lines(2), '', 1, 2, final_window=False, context_safe=True)
    final = st._window_prompt(lines(2), '', 2, 2, final_window=True, context_safe=True)
    assert st.HANDOFF_MARKER in first
    assert st.HANDOFF_MARKER not in final
