"""全文整理提示：不调用模型，验证模型拿到全部原文和真实时间约束。"""
import re

import pytest

from dubforge.pipeline import step_split as ss
from dubforge.pipeline.text_prompts import SEMANTIC_OUTPUT_SYSTEM
from dubforge.errors import StepFailedError
from dubforge.utils.srt import SubLine


@pytest.fixture(autouse=True)
def isolated(isolated_env):
    """即使未来提示构建读取配置，也不访问真实项目。"""


def word(text, start, end):
    return {'word': text, 'start': start, 'end': end}


def test_phrase_prompt_exposes_long_silence_hidden_inside_one_asr_phrase():
    source = [SubLine(1, 0, 12, 'one really cool thing')]
    words = [word('one', 0, 1.2), word('really', 7.2, 8),
             word('cool', 8, 9), word('thing', 9, 12)]
    prompt = ss._coarse_resegment_prompt(source, 50, 15, words=words, include_words=False)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    marker = '# 无台词区间 1.20-7.20（6.00秒，必须留空）'
    assert body == '0.00-12.00: one really cool thing\n' + marker
    assert prompt.count(marker) == 1
    assert '# 停顿' not in body
    assert '【长静音区间' not in prompt
    assert '不能从其前跨到其后' in prompt
    assert '真实未说完的表达按声学边界保留' in prompt


def test_word_mode_keeps_word_timeline_and_same_long_silence_fact():
    source = [SubLine(1, 10, 20, 'hello again')]
    words = [word('hello', 10, 11), word('again', 17, 20)]
    prompt = ss._coarse_resegment_prompt(source, 50, 15, words=words, include_words=True)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    marker = '# 无台词区间 11.00-17.00（6.00秒，必须留空）'
    assert body == '10.00-11.00: hello\n' + marker + '\n17.00-20.00: again'
    assert prompt.count(marker) == 1
    assert '【长静音区间' not in prompt
    assert '10.00-20.00: hello again' not in body


def test_ordinary_one_to_two_second_hesitations_remain_soft_pause_markers():
    source = [SubLine(1, 0, 7, 'first second third')]
    words = [word('first', 0, 1), word('second', 2, 3), word('third', 4.5, 7)]
    prompt = ss._coarse_resegment_prompt(source, 20, 7, words=words,
        pause_threshold=1, include_words=False)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    assert body == '0.00-7.00: first second third\n# 停顿 1.00s\n# 停顿 1.50s'
    assert '# 无台词区间' not in body
    assert '短停顿按语义判断' in prompt


@pytest.mark.parametrize('pause_threshold', [.6, 3])
@pytest.mark.parametrize('duration', [1.99, 2.0, 2.01])
@pytest.mark.parametrize('include_words', [False, True])
def test_two_second_no_speech_boundary_is_independent_of_ordinary_pause_setting(
        pause_threshold, duration, include_words):
    right_start = 1 + duration
    source = [SubLine(1, 0, right_start + 1, 'hello again')]
    words = [word('hello', 0, 1), word('again', right_start, right_start + 1)]
    prompt = ss._coarse_resegment_prompt(source, 20, 7, words=words,
        pause_threshold=pause_threshold, include_words=include_words)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    if duration >= 2:
        marker = f'# 无台词区间 1.00-{right_start:.2f}（{duration:.2f}秒，必须留空）'
        assert body.count(marker) == 1 and prompt.count(marker) == 1
        assert '# 停顿' not in body
    else:
        assert '# 无台词区间' not in body
        assert ('# 停顿 1.99s' in body) is (duration >= pause_threshold)


@pytest.mark.parametrize('duration,annotated', [(.59, False), (.6, True), (.61, True)])
def test_ordinary_pause_threshold_point_six_is_not_a_no_speech_hard_boundary(duration, annotated):
    words = [word('hello', 0, 1), word('again', 1 + duration, 2 + duration)]
    body = ss._format_word_timeline(words, pause_threshold=.6)
    assert ('# 停顿' in body) is annotated
    assert '# 无台词区间' not in body


def test_missing_word_timestamps_only_reports_observed_phrase_gaps():
    source = [SubLine(1, 0, 2, 'hello'), SubLine(2, 8, 10, 'again')]
    prompt = ss._coarse_resegment_prompt(source, 50, 15, words=[], include_words=False)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    marker = '# 无台词区间 2.00-8.00（6.00秒，必须留空）'
    assert body == '0.00-2.00: hello\n' + marker + '\n8.00-10.00: again'
    assert prompt.count(marker) == 1
    no_gap = ss._coarse_resegment_prompt([SubLine(1, 0, 10, 'hello again')],
        50, 15, words=[], include_words=False)
    assert '# 无台词区间' not in no_gap, '无声学证据时不虚构短语内部静音'


@pytest.mark.parametrize('include_words', [False, True])
def test_nested_overlapping_word_ranges_use_speech_union_without_inventing_gaps(include_words):
    source = [SubLine(1, 0, 20, 'first second third')]
    words = [word('first', 0, 20), word('second', 2, 3), word('third', 10, 11)]
    prompt = ss._coarse_resegment_prompt(source, 20, 7, words=words, include_words=include_words)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    assert '# 无台词区间' not in body and '# 停顿' not in body


def test_full_prompt_keeps_every_source_phrase_and_uses_short_prioritized_instructions():
    source = [SubLine(i, i * 2, i * 2 + 1.5, f'phrase {i} ' + 'words ' * 8)
              for i in range(1, 181)]
    prompt = ss._coarse_resegment_prompt(source, 20, 7, include_words=False)
    guidance, body = prompt.split('【全文时间轴输入】\n', 1)
    assert len(body) > 8_000 and len(body.splitlines()) == 180
    assert 'phrase 1 ' in body and 'phrase 180 ' in body
    # 软目标与接缝纠错需要明确说明，仍限制为短指令而不削减原文输入。
    assert len(guidance) < 1_000
    priorities = ('结合全文语境和常识', '每句尽量不超过', '时间取原始锚点', '【输出】')
    positions = [guidance.index(rule) for rule in priorities]
    assert positions == sorted(positions)
    assert '全文语义整理' in guidance and '不要求逐词照抄' in guidance
    assert '结合全文语境和常识' in guidance
    assert '人名、数字和币种等识别错误' in guidance
    assert '不能凭常识' not in guidance and '只能保守纠错' not in guidance
    assert '不为内容审查删台词' in guidance
    assert '只提交一份最终编号时间轴' in guidance and '最后一条字幕结束即结束响应' in guidance
    assert '自检' not in guidance and '一次轻量核对' not in guidance
    assert '序号. 开始秒-结束秒: 原文片段' in guidance
    assert '序号仅作标记，程序按输出顺序重排' in guidance
    assert '时间递增且不重叠' in guidance
    assert '超限由程序兜底' not in guidance and '输出示例' not in guidance
    assert '10个词、7秒' in guidance and '作为软参考' in guidance
    assert '词数和时长都不是硬上限' in guidance
    assert '超过不会自动判错、截断或强制拆句' in guidance
    assert '中文按自然语义词理解' in guidance
    assert '不按字符数或模型token数硬切' in guidance
    assert prompt.count('【全文时间轴输入】') == 1
    for item in source:
        assert body.count(f'{item.start:.2f}-{item.end:.2f}: {item.text}') == 1
    assert not re.search(r'保留所有(?:台词|词)', guidance)


def test_output_contract_uses_generic_no_explanation_rule():
    """协议约束不绑定某次模型偶发的英文自我修正措辞。"""
    assert '不要输出任何说明、校对记录、自我修正文本或第二份草稿' in SEMANTIC_OUTPUT_SYSTEM
    assert 'The above line was split incorrectly' not in SEMANTIC_OUTPUT_SYSTEM
    assert 'corrected as follows' not in SEMANTIC_OUTPUT_SYSTEM


def test_full_prompt_checks_both_sides_of_a_boundary_without_deleting_starters():
    prompt = ss._coarse_resegment_prompt([SubLine(1, 0, 8, 'great I think we visited the market')],
                                        50, 15, include_words=False)
    guidance = prompt.split('【全文时间轴输入】', 1)[0]
    assert '联合理解相邻句首句尾' in guidance
    assert 'I、you、the、and' in guidance
    assert 'I mean、the train' in guidance
    assert '输入时间是识别锚点，不是输出句界' in guidance
    assert '不照留ASR碎片' in guidance and '保留实质信息而非每个识别词' in guidance
    assert '属于相邻表达时随完整搭配输出' in guidance
    assert '短暂停顿不强制拆行' in guidance
    assert '停顿边缘或片尾的落单词' in guidance
    assert '无意义识别残留时直接清理' in guidance and '漏词可补回' in guidance
    assert '有时间戳也不代表应保留或单列' in guidance
    assert '不按词表或仅凭位置、大小写、短时长删词' in guidance
    assert '真实独立应答、字母专名和有意义的重复或中断保留' in guidance
    assert '不确定时保留原意' in guidance
    assert '未提供音频，不能声称核听原音' in guidance
    assert '时间取原始锚点' in guidance
    assert '真实未说完的表达按声学边界保留' in guidance
    assert '无台词区间必须留空' in guidance
    assert '可以改变ASR行数' in guidance


def test_legacy_context_is_read_only_and_not_duplicated_in_body():
    source = [SubLine(1, 2, 4, 'owned speech')]
    prompt = ss._coarse_resegment_prompt(source, 50, 15,
        '0.00-2.00: previous reference', '4.00-6.00: following reference', include_words=False)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    assert body == '2.00-4.00: owned speech'
    assert '【前文参考，只用于理解，不输出】' in prompt
    assert '【后文参考，只用于理解，不输出】' in prompt


def test_custom_word_goal_is_soft_and_legacy_character_value_does_not_change_llm_guidance():
    source = [SubLine(1, 0, 8, 'we visited the market and enjoyed a really lovely meal together')]
    first = ss._coarse_resegment_prompt(source, 20, 7, target_word_count=13, include_words=False)
    changed_characters = ss._coarse_resegment_prompt(source, 80, 7, target_word_count=13, include_words=False)
    assert first == changed_characters
    guidance = first.split('【全文时间轴输入】', 1)[0]
    assert '13个词、7秒' in guidance and '作为软参考' in guidance
    assert '不按字符数或模型token数硬切' in guidance
    default = ss._coarse_resegment_prompt(source, 20, 7, include_words=False)
    assert '10个词、7秒' in default and '13个词' not in default


@pytest.mark.parametrize('invalid', [
    word('backward', .5, .4), word('negative', -1, 0),
    word('not-finite', float('nan'), 3), word('not-finite', 2, float('inf')),
    word('not-numeric', 'bad', 3),
])
def test_invalid_source_word_axis_reports_typed_chinese_failure(invalid):
    with pytest.raises(StepFailedError) as error:
        ss._format_word_timeline([word('valid', 0, 1), invalid])
    assert error.value.code == 'SPLIT_SOURCE_TIMELINE'
    assert '时间轴无效' in str(error.value)
    assert '语音识别' in error.value.suggestion


@pytest.mark.parametrize('include_words', [False, True])
def test_prompt_rejects_observable_reversed_source_times_instead_of_inventing_gap(include_words):
    source = [SubLine(1, 0, 4, 'hello again')]
    words = [word('hello', 0, 1), word('again', 2, 1.5)]
    with pytest.raises(StepFailedError) as error:
        ss._coarse_resegment_prompt(source, 20, 7, words=words, include_words=include_words)
    assert error.value.code == 'SPLIT_SOURCE_TIMELINE'


def test_repair_reuses_full_source_and_constraints_once_with_bounded_diagnostic():
    source = [SubLine(1, 0, 12, 'one really cool thing')]
    words = [word('one', 0, 1.2), word('really cool thing', 7.2, 12)]
    original = ss._coarse_resegment_prompt(source, 50, 15, words=words, include_words=False)
    retry = ss._split_repair_prompt(source, '不应再次插入的独立时间轴',
        ValueError('跨越6秒长静音；' + '诊断' * 5_000), original)
    assert retry.endswith(original)
    assert len(retry) - len(original) < 1_000
    assert retry.count('【全文时间轴输入】') == 1
    assert retry.count('# 无台词区间 1.20-7.20（6.00秒，必须留空）') == 1
    assert '【长静音区间' not in retry
    assert '不应再次插入的独立时间轴' not in retry
    assert '仅针对上述问题修订，返回完整完成稿' in retry
    assert '上次输出' not in retry


def test_repair_without_saved_prompt_preserves_supplied_timeline_and_protocol():
    timeline = '0.00-1.00: hello\n# 停顿 6.00s\n7.00-8.00: again'
    retry = ss._split_repair_prompt([SubLine(1, 0, 8, 'hello again')],
        timeline, ValueError('编号不连续'))
    assert retry.endswith(timeline)
    assert '每行“序号. 开始秒-结束秒: 原文片段”' in retry
    assert '结合全文语境和常识' in retry
    assert '不要求逐词照抄' in retry
    assert '保留实质信息而非每个识别词' in retry
    assert '有时间戳也不代表应保留或单列' in retry
    assert '不确定时保留原意' in retry
    assert len(retry.split('【全文时间轴输入】', 1)[0]) < 600
