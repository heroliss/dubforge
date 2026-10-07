"""全文优先翻译及真实上下文分窗；所有模型请求均在隔离目录内模拟。"""
import hashlib
import re
from types import SimpleNamespace

import pytest

from dubforge.backends import llm
from dubforge.errors import StepFailedError
from dubforge.pipeline import step_translate as st
from dubforge.runner import StepCancelled
from dubforge.utils.lineprotocols import parse_numbered_lines, serialize_timed_lines


@pytest.fixture
def context(isolated_env, tmp_path, monkeypatch):
    values = {'translate.max_context_tokens': 200_000, 'translate.target_language': '简体中文'}
    root = tmp_path / 'project'
    output = root / 'translate'
    output.mkdir(parents=True)
    source = root / 'split.txt'
    logs, registered = [], []
    monkeypatch.setattr(llm, 'describe_selection', lambda _: '隔离模拟模型')
    profiles = [dict(name='冻结配置', base_url='https://fixture.invalid/v1', api_key='fixture',
                     model='fixture-model', params={'temperature': 0.2})]
    return SimpleNamespace(force=False, pdir=root, values=values, source=source, output=output,
        profiles=profiles, llm_profile_snapshot=lambda _selected: profiles,
        cfg=lambda key, default=None: values.get(key, default), check_cancel=lambda: None,
        check_pause=lambda: None, log=logs.append, logs=logs, llm_trace=lambda *_a, **_kw: None,
        report_progress=lambda *_a: None, upstream=lambda *_a: source,
        artifact_dir=lambda: output, register_file=lambda *args: registered.append(args),
        registered=registered)


def lines(count=81, text='source'):
    return [dict(id=i, text=f'{text}-{i}', start=i * 2.0, end=i * 2.0 + 1.5)
            for i in range(1, count + 1)]


def install_model(monkeypatch, respond=None):
    calls = []

    def chat(prompt='', **kwargs):
        assert not kwargs.get('messages'), '独立重试不能累加旧候选正文'
        marker = ('【待翻译原文与候选译文】' if '【待翻译原文与候选译文】' in prompt else
                  '【待重译片段】' if '【待重译片段】' in prompt else '【待翻译原文】')
        body = prompt.split(marker, 1)[1].split('【', 1)[0]
        ids = [int(m.group(1)) for m in re.finditer(r'^(\d+)\. ', body, re.MULTILINE)]
        info = dict(prompt=prompt, ids=ids, system=kwargs.get('system'), profile=kwargs.get('profile'),
                    review=marker == '【待翻译原文与候选译文】',
                    handoff='译文后另起一行输出交接标记' in prompt)
        calls.append(info)
        if respond:
            custom = respond(info, calls)
            if custom is not None:
                return custom
        result = '\n'.join(f'{i}. 译文{i}' for i in ids)
        if info['handoff']:
            result += f'\n{st.HANDOFF_MARKER}\n术语{ids[-1]}保持一致'
        return result

    monkeypatch.setattr(llm, 'chat', chat)
    return calls


def seed(context, rows):
    context.source.write_text(serialize_timed_lines(rows), encoding='utf-8')


def test_faithful_system_has_short_priorities_and_style_never_overrides_final_source(context):
    context.values['translate.style_prompt'] = '接地气，可以用网络流行语，网络梗等'
    system = st._base_system(context, 'florp → florp')
    priorities = [system.index(label) for label in ('同编号保真', '事实优先', '原文已定稿', '再求自然')]
    assert priorities == sorted(priorities)
    assert '没有音频，不再猜改ASR' in system
    assert '疑似实词或专名无法确定时保留原词' in system
    assert '上下文只用于消歧或确定已有指代' in system
    assert '半句保留半句' in system
    assert '不能为了流畅搬动信息' in system
    assert '不把含糊、未说完的内容变成肯定或否定' in system
    assert '不把外币泛化成无币种的钱' in system
    assert '不倒置比较方向或省略币种' in system
    assert '可纠正明显的识别错误' not in system
    assert '结合整体语境和常识' not in system
    assert system.index('风格和网络梗不能添加') < system.index('翻译风格要求：')
    assert context.values['translate.style_prompt'] in system and 'florp → florp' in system
    context.values['translate.style_prompt'] = ''
    assert len(st._base_system(context, '')) <= 1_100
    assert len(st.ADAPTIVE_ALIGNMENT_GUIDANCE) <= 120


def test_real_run_keeps_doubtful_final_source_and_full_document_single_call(context, monkeypatch):
    """验证传入模型的事实与词没有被本地猜改；不把模拟输出当模型质量证明。"""
    texts = ['we have two choices', 'for this trip', 'A costs two dollars more than B',
             'it is not ready', 'definitely florp and', 'the neru is lighter than the florp']
    rows = [{**row, 'text': text} for row, text in zip(lines(len(texts)), texts)]
    seed(context, rows); calls = install_model(monkeypatch)
    st.run(context)
    assert len(calls) == 1 and not calls[0]['review'] and not calls[0]['handoff']
    body = calls[0]['prompt'].split('【待翻译原文】\n', 1)[1].split('\n\n【', 1)[0]
    assert body == '\n'.join(st._timed_source(row) for row in rows)
    assert '同编号保真' in calls[0]['system'] and '没有音频，不再猜改ASR' in calls[0]['system']
    assert list(parse_numbered_lines((context.output / 'translate.txt').read_text(encoding='utf-8'))) == list(range(1, 7))
    assert len(context.registered) == 1


def test_full_translation_explains_function_words_but_preserves_source_ids_and_times(context, monkeypatch):
    """验证请求契约，不把模拟的译文当作真实模型准确率证据。"""
    texts = ["I'll try the", 'duck', 'Cheers, cheers, I', 'got the drink',
             'I think', 'The Hague', 'grade A', 'type I', 'unknown florp']
    rows = [{**row, 'text': text} for row, text in zip(lines(len(texts)), texts)]
    seed(context, rows)
    before = context.source.read_bytes()
    calls = install_model(monkeypatch)
    st.run(context)
    assert len(calls) == 1
    assert calls[0]['ids'] == list(range(1, len(rows) + 1))
    assert context.source.read_bytes() == before
    body = calls[0]['prompt'].split('【待翻译原文】\n', 1)[1].split('\n\n【', 1)[0]
    assert body == '\n'.join(st._timed_source(row) for row in rows)
    system = calls[0]['system']
    assert '联合前后原文理解the/a/an、I、and' in system
    assert '例如译成中文时，冠词通常不必单独落字' in system
    assert '不能借下一句补出本句缺少的名词、动词或事实' in system
    assert '长静音两侧不强接' in system
    assert 'The Hague' in system and 'grade A' in system and 'type I' in system
    assert '不能套用功能词省略规则' in system
    assert list(parse_numbered_lines((context.output / 'translate.txt').read_text(encoding='utf-8'))) == calls[0]['ids']


def test_planner_keeps_realistic_316_lines_as_one_full_document(context):
    rows = lines(316)
    _, budget = st._request_text_budget(context, '系统', st.HANDOFF_RESERVE_TOKENS)
    chunks = st._plan_translation_windows(context, rows, '系统', budget)
    assert [len(chunk) for chunk in chunks] == [316]
    assert [row['id'] for chunk in chunks for row in chunk] == list(range(1, 317))
    assert sum(st._line_cost(row) for row in rows) > 2_000


def test_long_document_with_more_than_2000_source_tokens_stays_whole_when_it_fits(context):
    rows = lines(30, 'long source text ' * 25)
    chunks = st._plan_translation_windows(context, rows, '系统', 150_000)
    assert len(chunks) == 1
    assert sum(st._line_cost(row) for row in rows) > 2_000
    assert [row['id'] for chunk in chunks for row in chunk] == list(range(1, 31))


def test_context_hard_limit_accounts_for_system_boundary_and_reserved_handoff(context):
    context.values['translate.max_context_tokens'] = 12_000
    rows = lines(70, 'natural source ' * 15)
    chunks = st._plan_translation_windows(context, rows, '系统提示' * 70, 20_000)
    assert len(chunks) > 1
    first = 0
    for index, chunk in enumerate(chunks, 1):
        last = first + len(chunk)
        prompt = st._window_prompt(chunk, '' if first == 0 else '交' * st.HANDOFF_SUMMARY_MAX_CHARS,
            index, len(chunks), final_window=last == len(rows), context_safe=True,
            reference_lines=st._boundary_lines(rows, first, last))
        assert st._full_context_retry_fits(context, '系统提示' * 70, prompt, '', st.CONTEXT_RETRY_HINT)
        first = last


@pytest.mark.parametrize('count', [4, 316])
def test_real_run_whole_document_one_request_and_registers_complete_output(context, monkeypatch, count):
    seed(context, lines(count)); calls = install_model(monkeypatch)
    st.run(context)
    assert len(calls) == 1 and calls[0]['ids'] == list(range(1, count + 1))
    assert not calls[0]['handoff']
    assert '参考第' not in calls[0]['prompt'] and not calls[0]['review']
    assert list(parse_numbered_lines((context.output / 'translate.txt').read_text(encoding='utf-8'))) == list(range(1, count + 1))
    assert len(context.registered) == 1


def test_full_document_fit_overrides_conservative_body_budget(context):
    """决定分窗的是真实完整请求，不是预先扣掉摘要的粗估正文预算。"""
    rows = lines(316)
    assert st._plan_translation_windows(context, rows, '系统', 1) == [rows]


def test_full_request_keeps_long_silence_between_original_ids(context, monkeypatch):
    """真实长空档跟随全文发送，不能靠跨静音补词把两条台词连成新事实。"""
    rows = [dict(id=1, text='I am not sure', start=0.0, end=1.0),
            dict(id=2, text='A new topic begins here', start=14.89, end=17.0),
            dict(id=3, text='and continues', start=17.1, end=18.0)]
    seed(context, rows); calls = install_model(monkeypatch)
    st.run(context)
    assert len(calls) == 1 and calls[0]['ids'] == [1, 2, 3]
    prompt = calls[0]['prompt']
    assert '原句间静音 13.89 秒（非台词）' in prompt
    assert '不把前句残词补到下一编号' in prompt
    assert prompt.index('1. I am not sure') < prompt.index('原句间静音') < prompt.index('2. A new topic')
    assert prompt.count('原句间静音') == 1
    assert list(parse_numbered_lines((context.output / 'translate.txt').read_text(encoding='utf-8'))) == [1, 2, 3]


def test_sparse_repair_does_not_treat_omitted_rows_as_silence():
    """修复只取缺失行时，中间未发送的台词不能误标成静音。"""
    rows = [dict(id=1, text='first', start=0.0, end=1.0),
            dict(id=3, text='third', start=20.0, end=21.0)]
    prompt = st._repair_prompt(rows, '', 1, 1, [1, 3])
    assert '1. first' in prompt and '3. third' in prompt
    assert '原句间静音' not in prompt


@pytest.mark.parametrize('gap,marked', [(1.999, False), (2.0, True), (2.001, True)])
def test_translation_marks_the_same_two_second_speech_gap_as_split(gap, marked):
    rows = [dict(id=1, text='I might', start=0.0, end=1.0),
            dict(id=2, text='another topic', start=1.0 + gap, end=5.0)]
    body = st._source_body(rows)
    assert ('原句间静音' in body) is marked
    assert body.count('1. I might') == body.count('2. another topic') == 1


def test_capacity_planner_counts_actual_silence_hints(context):
    context.values['translate.max_context_tokens'] = 12_000
    rows = [dict(id=i, text='source', start=i * 8.0, end=i * 8.0 + 1.0) for i in range(1, 201)]
    chunks = st._plan_translation_windows(context, rows, '系统', 150_000)
    assert len(chunks) > 1
    first = 0
    for index, chunk in enumerate(chunks, 1):
        last = first + len(chunk)
        prompt = st._window_prompt(chunk, '' if first == 0 else '交' * st.HANDOFF_SUMMARY_MAX_CHARS,
            index, len(chunks), final_window=last == len(rows), context_safe=True,
            reference_lines=st._boundary_lines(rows, first, last))
        assert '原句间静音 7.00 秒' in prompt
        assert st._full_context_retry_fits(context, '系统', prompt, '', st.CONTEXT_RETRY_HINT)
        first = last


def test_translation_prepares_low_profiles_without_mutating_frozen_chain(context, monkeypatch):
    from copy import deepcopy

    context.profiles[:] = [
        dict(name='套餐', base_url='https://ark.cn-beijing.volces.com/api/plan/v3', api_key='fixture',
             model='ark-code-latest', params={'reasoning_effort': 'high', 'thinking': {'type': 'enabled'}}),
        dict(name='备用', base_url='https://fixture.invalid/v1', api_key='fixture',
             model='other', params={'thinking': {'type': 'enabled'}}),
    ]
    original = deepcopy(context.profiles)
    seed(context, lines(3)); calls = install_model(monkeypatch)
    st.run(context)
    assert len(calls) == 1
    effective = calls[0]['profile']
    assert [p['name'] for p in effective] == ['套餐', '备用']
    assert effective[0]['params'] == {'reasoning_effort': 'low'}
    assert effective[1]['params'] == original[1]['params']
    assert context.profiles == original
    assert '只提交一份最终译文' in calls[0]['system']
    assert '最后一行结束即结束响应' in calls[0]['system']
    assert 'fixture' not in '\n'.join(context.logs)
    st.run(context)
    assert len(calls) == 1
    context.values['translate.thinking_mode'] = 'follow_profile'
    st.run(context)
    assert len(calls) == 2 and calls[-1]['profile'] == original


def test_low_alias_fast_preserves_same_cache_and_request(context, monkeypatch):
    seed(context, lines(3)); calls = install_model(monkeypatch)
    context.values['translate.thinking_mode'] = 'fast'
    st.run(context)
    context.values['translate.thinking_mode'] = 'low'
    st.run(context)
    assert len(calls) == 1


def test_invalid_translation_thinking_mode_stops_before_request(context, monkeypatch):
    seed(context, lines(3)); calls = install_model(monkeypatch)
    context.values['translate.thinking_mode'] = 'unsupported'
    with pytest.raises(StepFailedError) as error:
        st.run(context)
    assert error.value.code == 'TRANSLATE_THINKING_MODE'
    assert not calls and not context.registered


def test_real_run_first_windows_have_two_sided_reference_and_serial_handoff(context, monkeypatch):
    context.values['translate.max_context_tokens'] = 12_000
    rows = lines(81, 'natural source ' * 10)
    seed(context, rows); calls = install_model(monkeypatch)
    st.run(context)
    assert len(calls) > 1
    assert [i for call in calls for i in call['ids']] == list(range(1, 82))
    assert all(not call['review'] for call in calls), '正常成功不增加额外QA请求'
    for pos, call in enumerate(calls):
        first, last = call['ids'][0] - 1, call['ids'][-1]
        expected = st._boundary_lines(rows, first, last)
        actual_refs = [int(n) for n in re.findall(r'参考第 (\d+) 句：', call['prompt'])]
        assert actual_refs == [line['id'] for line in expected]
        if pos:
            assert f"术语{calls[pos - 1]['ids'][-1]}保持一致" in call['prompt']
        assert call['handoff'] == (pos < len(calls) - 1)
        assert st._full_context_retry_fits(context, call['system'], call['prompt'], '', st.CONTEXT_RETRY_HINT)
    assert '交接只记录需要延续的术语' in calls[0]['prompt']
    assert '不要概括台词或用摘要代替后续原文' in calls[0]['prompt']
    assert len(parse_numbered_lines((context.output / 'translate.txt').read_text(encoding='utf-8'))) == 81
    before = len(calls)
    st.run(context)
    assert len(calls) == before, '成功窗口可恢复缓存，不再调用模型'


def test_reference_or_actual_system_changes_invalidate_window_cache(context, monkeypatch):
    rows = lines(4); calls = install_model(monkeypatch)
    kwargs = dict(reference_lines=[rows[3]], context_safe=True)
    st._translate_window(context, rows[:3], '术语：station=车站', '', 1, 1, **kwargs)
    st._translate_window(context, rows[:3], '术语：station=车站', '', 1, 1, **kwargs)
    assert len(calls) == 1
    st._translate_window(context, rows[:3], '术语：station=站点', '', 1, 1, **kwargs)
    kwargs['reference_lines'] = [{**rows[3], 'text': 'changed neighbour'}]
    st._translate_window(context, rows[:3], '术语：station=站点', '', 1, 1, **kwargs)
    assert len(calls) == 3


@pytest.mark.parametrize('old_stage', ['window-v6', 'window-proactive-v1'])
def test_old_cache_not_adopted_by_new_full_document_strategy(context, monkeypatch, old_stage):
    import json

    rows = lines(3); system = '系统'
    extra = '1/1||system:' + hashlib.sha256(system.encode('utf-8')).hexdigest()
    if old_stage == 'window-proactive-v1':
        old_context = json.dumps({'depth': 0, 'final': True, 'reference': []},
                                 ensure_ascii=False, sort_keys=True)
        extra += '|context:' + hashlib.sha256(old_context.encode('utf-8')).hexdigest()
        extra += '|strategy:proactive-40-lines-2000-tokens-v1'
    st._store(context, st._chunk_key(context, rows, old_stage, extra),
              {'lines': {row['id']: '旧译文' for row in rows}, 'handoff_summary': ''})
    calls = install_model(monkeypatch)
    translated, _ = st._translate_window(context, rows, system, '', 1, 1, context_safe=True)
    assert len(calls) == 1 and translated[1] == '译文1'


@pytest.mark.parametrize('failure', ['tiny-context', 'huge-system', 'huge-single-source'])
def test_impossible_request_fails_before_any_network_call(context, monkeypatch, failure):
    rows = lines(2)
    if failure == 'tiny-context': context.values['translate.max_context_tokens'] = 1_000
    if failure == 'huge-system': context.values['translate.style_prompt'] = '长提示' * 100_000
    if failure == 'huge-single-source':
        context.values['translate.max_context_tokens'] = 8_000
        rows = lines(1, 'really long source ' * 1_000)
    seed(context, rows); calls = install_model(monkeypatch)
    with pytest.raises(StepFailedError) as error: st.run(context)
    assert error.value.code == 'LLM_CONTEXT_BUDGET'
    assert not calls and not context.registered


def test_failed_later_window_keeps_success_cache_and_retries_only_missing_window(context, monkeypatch):
    context.values['translate.max_context_tokens'] = 12_000
    rows = lines(81, 'natural source ' * 10)
    system = st._base_system(context, '')
    _, budget = st._request_text_budget(context, system, st.HANDOFF_RESERVE_TOKENS)
    planned = st._plan_translation_windows(context, rows, system, budget)
    assert len(planned) > 1
    seed(context, rows); failing = [True]
    second_first = planned[1][0]['id']
    def respond(info, _calls):
        if info['ids'][0] >= second_first and failing[0]: return ''
    calls = install_model(monkeypatch, respond)
    with pytest.raises(StepFailedError): st.run(context)
    assert not context.registered
    assert list(parse_numbered_lines((context.output / 'translate.txt').read_text(encoding='utf-8'))) == [line['id'] for line in planned[0]]
    assert len(list((context.pdir / 'cache' / 'translate').glob('*.json'))) == 1
    failing[0] = False; previous = len(calls)
    st.run(context)
    assert len(calls) == previous + len(planned) - 1
    assert all(call['ids'][0] >= second_first for call in calls[previous:])
    assert len(context.registered) == 1


def test_structure_failure_uses_existing_bounded_children_and_fresh_qa(context, monkeypatch):
    rows = lines(40)
    def respond(info, _calls):
        if len(info['ids']) == 40: return '1. 错序\n1. 重复'
    calls = install_model(monkeypatch, respond)
    translated, _ = st._translate_window(context, rows, '系统', '', 1, 1, context_safe=True)
    assert list(translated) == list(range(1, 41))
    assert len(calls) == 7 and sum(call['review'] for call in calls) == 2
    assert [call['ids'] for call in calls if call['review']] == [list(range(1, 21)), list(range(21, 41))]
    assert '参考第 21 句' in calls[3]['prompt'] and '参考第 20 句' in calls[5]['prompt']
    assert all('错序' not in call['prompt'] for call in calls[1:])


def test_full_document_structure_failure_limits_recovery_source_tokens_and_total_calls(context, monkeypatch):
    rows = lines(81, 'natural source ' * 15)
    def respond(info, _calls):
        if len(info['ids']) == len(rows): return '1. 错序\n1. 重复'
    calls = install_model(monkeypatch, respond)
    translated, _ = st._translate_window(context, rows, '系统', '', 1, 1, context_safe=True)
    assert list(translated) == list(range(1, 82))
    child_calls = calls[3:]
    assert child_calls and len(calls) <= st.MAX_ADAPTIVE_VALIDATION_ATTEMPTS
    for call in child_calls:
        assert len(call['ids']) <= st.MAX_ADAPTIVE_WINDOW_LINES
        assert sum(st._line_cost(rows[i - 1]) for i in call['ids']) <= st.MAX_ADAPTIVE_SOURCE_TOKENS
    assert sum(call['review'] for call in calls) == len(child_calls) // 2


def test_missing_edge_line_repair_keeps_both_window_reference_and_valid_handoff(context, monkeypatch):
    rows = lines(42)
    def respond(info, calls):
        if len(calls) == 1:
            return '\n'.join(f'{i}. 译文{i}' for i in range(1, 40)) + f'\n40. \n{st.HANDOFF_MARKER}\n列车称谓保持一致'
    calls = install_model(monkeypatch, respond)
    translated, summary = st._translate_window(context, rows[:40], '系统', '', 1, 2,
        context_safe=True, reference_lines=rows[40:])
    assert len(calls) == 2 and calls[1]['ids'] == [40]
    assert '参考第 41 句：source-41' in calls[1]['prompt']
    assert '参考第 42 句：source-42' in calls[1]['prompt']
    assert '既有译文: 译文39' in calls[1]['prompt']
    assert len(translated) == 40 and summary == '列车称谓保持一致'


def test_huge_diagnostic_not_sent_or_accumulated_in_fresh_retry(context, monkeypatch):
    rows = lines(3); real_validator = st._valid_window
    def validator(*args, **kwargs):
        valid = real_validator(*args, **kwargs)
        failures = [0]
        def wrapped(raw):
            failures[0] += 1
            if failures[0] < 3: raise st._TranslationStructureError('很长的诊断' * 20_000)
            return valid(raw)
        for name in ('reset', 'required_ids', 'require_handoff', 'collected', 'response_line_counts', 'last_warnings'):
            setattr(wrapped, name, getattr(valid, name))
        return wrapped
    monkeypatch.setattr(st, '_valid_window', validator)
    calls = install_model(monkeypatch)
    translated, _ = st._translate_window(context, rows, '系统', '', 1, 1, context_safe=True)
    assert len(calls) == 3 and len(translated) == 3
    assert all('很长的诊断' not in call['prompt'] for call in calls)
    assert all(len(call['prompt']) < 5_000 for call in calls)
    assert st.CONTEXT_RETRY_HINT in calls[1]['prompt']


def test_cancellation_inside_parser_does_not_store_candidate(context, monkeypatch):
    rows = lines(3); cancelled = [False]; real_validator = st._valid_window
    def check():
        if cancelled[0]: raise StepCancelled('隔离解析取消')
    context.check_cancel = check
    def validator(*args, **kwargs):
        valid = real_validator(*args, **kwargs)
        def wrapped(raw):
            result = valid(raw); cancelled[0] = True
            return result
        for name in ('reset', 'required_ids', 'require_handoff', 'collected', 'response_line_counts', 'last_warnings'):
            setattr(wrapped, name, getattr(valid, name))
        return wrapped
    monkeypatch.setattr(st, '_valid_window', validator)
    calls = install_model(monkeypatch)
    with pytest.raises(StepCancelled):
        st._translate_window(context, rows, '系统', '', 1, 1, context_safe=True)
    assert len(calls) == 1 and not list((context.pdir / 'cache' / 'translate').glob('*.json'))


def test_cancellation_during_response_does_not_store_or_register(context, monkeypatch):
    seed(context, lines(3)); cancelled = [False]
    def check():
        if cancelled[0]: raise StepCancelled('隔离测试取消')
    context.check_cancel = check
    def respond(_info, _calls): cancelled[0] = True
    calls = install_model(monkeypatch, respond)
    with pytest.raises(StepCancelled): st.run(context)
    assert len(calls) == 1 and not context.registered
    assert not list((context.pdir / 'cache' / 'translate').glob('*.json'))
