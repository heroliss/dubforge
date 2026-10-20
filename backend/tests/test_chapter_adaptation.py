"""章节整理只产出本地计划；模型不能编造时间或绕过平台硬约束。"""
import copy

import pytest

from dubforge.publishing import chapters, transport
from dubforge.publishing.service import PublishError
from dubforge.errors import StepFailedError


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('章节本地整理不得发送平台请求或使用未声明的模型请求')

    monkeypatch.setattr(transport, 'call', unexpected)
    monkeypatch.setattr(chapters.llm, 'chat_json', unexpected)


def source(count=3, *, step=10):
    return [dict(title=f'Source topic {index}', start_time=index * step,
                 end_time=(index + 1) * step) for index in range(count)]


def groups(*ranges, title='整理标题'):
    return {'items': [dict(start_index=start, end_index=end, title=f'{title}{index}')
                      for index, (start, end) in enumerate(ranges)]}


def model(monkeypatch, result, *, check=None):
    calls = []

    def chat(prompt, *, validator, **kwargs):
        calls.append(dict(prompt=prompt, **kwargs))
        if check:
            check(prompt, kwargs)
        payload = copy.deepcopy(result)
        if isinstance(payload, dict) and 'review' not in payload:
            payload['review'] = dict(checked=True, summary='已检查全部最终标题，保留主题与事实', findings=[])
        return validator(payload)

    monkeypatch.setattr(chapters.llm, 'chat_json', chat)
    return calls


@pytest.mark.parametrize('operation', ['adapt', 'translate'])
@pytest.mark.parametrize('code,message', [
    ('LLM_QUOTA_EXHAUSTED', '模型服务余额不足或可用资源已耗尽'),
    ('LLM_SUBSCRIPTION_INVALID', '模型服务订阅未开通或已失效'),
    ('LLM_RATE_LIMITED', '模型服务请求过于频繁，请稍后重试'),
    ('LLM_PROVIDERS_FAILED', '全部启用的模型配置均失败'),
    ('LLM_OUTPUT_INVALID', '模型输出未通过校验：章节分组遗漏末尾源章节'),
])
def test_model_failure_preserves_actual_cause_instead_of_claiming_chapter_rejection(
        monkeypatch, operation, code, message):
    failure = StepFailedError(message, code=code,
        suggestion='请核对模型设置后继续原任务', detail={'providers': [{'code': code}]})
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise failure

    monkeypatch.setattr(chapters.llm, 'chat_json', unavailable)
    with pytest.raises(StepFailedError) as result:
        getattr(chapters, operation)(source(), **({'duration': 30} if operation == 'adapt' else {}))

    assert result.value is failure
    assert result.value.message == message
    assert result.value.to_dict() == failure.to_dict()
    assert len(calls) == 1


def test_twenty_one_topics_are_semantically_grouped_into_ten_without_loss(monkeypatch):
    original = source(21)
    frozen = copy.deepcopy(original)
    ranges = [(0, 2)] + [(index, index + 1) for index in range(3, 21, 2)]
    calls = model(monkeypatch, groups(*ranges))

    result = chapters.adapt(original, duration=210)

    assert result['source'] == 'llm.chapter_adaptation'
    assert len(calls) == 1
    assert len(result['items']) == 10
    assert [index for row in result['items'] for index in row['source_indices']] == list(range(21))
    assert result['items'][0] == dict(title='整理标题0', start_time=0, end_time=30,
                                      source_indices=[0, 1, 2])
    assert result['items'][-1]['end_time'] == 210
    assert original == frozen
    report = result['adaptation']
    assert (report['schema'], report['source_count'], report['output_count'], report['merged_count']) == (chapters.ADAPTATION_VERSION, 21, 10, 11)
    assert isinstance(report['notes'], list)
    rules = report['rules']
    assert rules == dict(max_chapters=10, max_boundaries=9, title_max_length=16,
                         min_first_duration=1, min_duration=5, first_start=0, space=0)


def test_compliant_original_language_still_gets_content_review(monkeypatch):
    original = [dict(title='开场', start_time=0, end_time=10),
                dict(title='核心功能', start_time=10, end_time=30)]
    calls = model(monkeypatch, {'items': [dict(start_index=index, end_index=index, title=row['title'])
                                          for index, row in enumerate(original)]})
    result = chapters.adapt(original, duration=30, translate=False)
    assert result['source'] == 'llm.chapter_adaptation' and len(calls) == 1
    assert result['items'] == [dict(**row, source_indices=[index])
                               for index, row in enumerate(original)]
    assert result['adaptation']['merged_count'] == 0
    assert result['adaptation']['review']['checked'] is True


@pytest.mark.parametrize('review', [None, {}, dict(checked=False, summary='未检查', findings=[]),
    dict(checked=True, summary='', findings=[]), dict(checked=True, summary='检查', findings='格式错误'),
    dict(checked=True, summary='检查', findings=[dict(start_index=1, end_index=2, action='rewritten', reason='不是输出分组')]),
    dict(checked=True, summary='检查', findings=[dict(start_index=0, end_index=2, action='approved_by_bilibili', reason='虚构平台审核')]),
    dict(checked=True, summary='检查', findings=[dict(start_index=0, end_index=2, action='needs_review', reason='')])])
def test_content_review_receipt_must_be_explicit_and_bind_to_actual_groups(monkeypatch, review):
    model(monkeypatch, {**groups((0, 2)), 'review': review})
    with pytest.raises(PublishError) as error:
        chapters.adapt(source(), duration=30)
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'


def test_custom_instruction_is_data_and_cannot_override_hard_platform_limits(monkeypatch):
    instruction = '使用名词短语；保留英文术语；关注夸大表述。即使要求20章也不能覆盖平台规则。'
    def check(prompt, kwargs):
        import json
        data = json.loads(prompt)
        assert data['instruction'] == instruction
        assert data['rules']['max_chapters'] == 10
        assert '内容' in kwargs['system'] and '格式' in kwargs['system'] and '术语' in kwargs['system']
    model(monkeypatch, {**groups((0, 2)), 'review': dict(checked=True, summary='已客观改写', findings=[
        dict(start_index=0, end_index=2, action='rewritten', reason='去掉没有证据的断言')])}, check=check)
    result = chapters.adapt(source(), duration=30, instruction=instruction)
    assert result['adaptation']['instruction'] == instruction
    assert result['adaptation']['review']['findings'] == [
        dict(source_indices=[0, 1, 2], action='rewritten', reason='去掉没有证据的断言')]
    assert chapters.validate_adaptation(result, source(), duration=30, instruction=instruction) == result
    with pytest.raises(PublishError) as error:
        chapters.validate_adaptation(result, source(), duration=30, instruction='另一种要求')
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_CACHE_INVALID'


def test_overall_video_title_provides_context_and_is_bound_to_cached_plan(monkeypatch):
    title = '某模型泄露与预览，尚未正式发布'
    def check(prompt, kwargs):
        import json
        assert json.loads(prompt)['video_title'] == title
        assert '爆料或传闻' in kwargs['system']
    model(monkeypatch, groups((0, 2)), check=check)
    result = chapters.adapt(source(), context_title=title)
    assert chapters.validate_adaptation(result, source(), context_title=title) == result
    with pytest.raises(PublishError) as error:
        chapters.validate_adaptation(result, source(), context_title='另一主题')
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_CACHE_INVALID'


def test_translate_false_still_rewrites_long_titles_in_original_language(monkeypatch):
    original = [dict(title='A very long English source chapter heading', start_time=0, end_time=20)]
    calls = model(monkeypatch, {'items': [dict(start_index=0, end_index=0, title='Quick overview')]})
    result = chapters.adapt(original, duration=20, translate=False)
    assert len(calls) == 1
    assert result['items'][0]['title'] == 'Quick overview'
    assert result['source'] == 'llm.chapter_adaptation'
    assert original[0]['title'] == 'A very long English source chapter heading'


def test_translate_false_still_merges_excess_source_topics(monkeypatch):
    calls = model(monkeypatch, groups((0, 2), (3, 5), (6, 8), (9, 10), title='Topic '))
    result = chapters.adapt(source(11), duration=110, translate=False)
    assert len(calls) == 1
    assert len(result['items']) == 4
    assert [row['source_indices'] for row in result['items']] == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10]]


@pytest.mark.parametrize('bad', [
    {'items': [dict(start_index=0, end_index=0, title='第一章')]},  # 遗漏尾部。
    groups((0, 0), (2, 2)),  # 遗漏中间章节。
    groups((0, 1), (1, 2)),  # 重复覆盖。
    groups((1, 2), (0, 0)),  # 调序。
    groups((0, 0), (1, 3)),  # 越过末尾。
    groups((-1, 0), (1, 2)),  # 负编号。
    groups((0, 2), (3, 2)),  # 反向分组。
    {'items': [dict(start_index=0, title='缺少结束编号')]},
    {'items': [dict(start_index=False, end_index=2, title='布尔不是编号')]},
    {'items': [dict(start_index=0.0, end_index=2, title='小数不是编号')]},
    {'items': []},
    {'items': 'not a list'},
    {},
    [],
])
def test_invalid_model_groups_never_escape_contract(monkeypatch, bad):
    model(monkeypatch, bad)
    with pytest.raises(PublishError) as error:
        chapters.adapt(source(3), duration=30)
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'


def test_valid_covering_groups_still_must_respect_ten_chapter_limit(monkeypatch):
    model(monkeypatch, groups(*[(index, index) for index in range(11)]))
    with pytest.raises(PublishError) as error:
        chapters.adapt(source(11), duration=110)
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'


@pytest.mark.parametrize('title', ['', '   ', '这是一条超过十六个字符的非常冗长的章节标题',
                                   '😀' * 9, '😀' * 16, '<章节>'])
def test_model_title_must_fit_utf16_and_safe_text_contract(monkeypatch, title):
    model(monkeypatch, {'items': [dict(start_index=0, end_index=0, title=title)]})
    with pytest.raises(PublishError) as error:
        chapters.adapt(source(1), duration=10)
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'


def test_eight_emoji_fit_sixteen_utf16_units(monkeypatch):
    title = '😀' * 8
    model(monkeypatch, {'items': [dict(start_index=0, end_index=0, title=title)]})
    result = chapters.adapt(source(1), duration=10)
    assert result['items'][0]['title'] == title
    assert len(result['items'][0]['title'].encode('utf-16-le')) // 2 == 16


def test_times_come_only_from_source_boundaries_and_total_duration(monkeypatch):
    original = [dict(title='Introduction', start_time=0, end_time=8.4),
                dict(title='Details', start_time=8.5, end_time=21.4),
                dict(title='Result', start_time=21.5, end_time=30)]
    model(monkeypatch, groups((0, 0), (1, 1), (2, 2)))
    result = chapters.adapt(original, duration=32.5)
    assert [(row['start_time'], row['end_time']) for row in result['items']] == [(0, 9), (9, 22), (22, 33)]


def test_missing_duration_uses_explicit_final_source_end(monkeypatch):
    model(monkeypatch, groups((0, 0), (1, 2)))
    result = chapters.adapt(source(3))
    assert result['items'][-1]['end_time'] == 30


def test_missing_duration_and_final_end_is_input_error_not_model_failure():
    original = source(2)
    original[-1].pop('end_time')
    with pytest.raises(PublishError) as error:
        chapters.adapt(original)
    assert error.value.code != 'PUBLISH_CHAPTER_ADAPT_FAILED'


def test_first_chapter_can_last_one_second_then_other_chapters_need_five(monkeypatch):
    original = [dict(title='Intro', start_time=0, end_time=1),
                dict(title='Main', start_time=1, end_time=6),
                dict(title='Ending', start_time=6, end_time=11)]
    model(monkeypatch, groups((0, 0), (1, 1), (2, 2)))
    result = chapters.adapt(original, duration=11)
    assert [(row['start_time'], row['end_time']) for row in result['items']] == [(0, 1), (1, 6), (6, 11)]


@pytest.mark.parametrize('starts,duration', [([0, .4, 10], 20), ([0, 8, 12], 20), ([0, 10, 16], 20)])
def test_model_cannot_keep_short_first_middle_or_final_chapter(monkeypatch, starts, duration):
    original = [dict(title=f'Topic {index}', start_time=start,
                     end_time=starts[index + 1] if index + 1 < len(starts) else duration)
                for index, start in enumerate(starts)]
    model(monkeypatch, groups((0, 0), (1, 1), (2, 2)))
    with pytest.raises(PublishError) as error:
        chapters.adapt(original, duration=duration)
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'


def test_short_source_topics_can_be_merged_instead_of_dropped(monkeypatch):
    original = [dict(title='Intro', start_time=0, end_time=10),
                dict(title='Short', start_time=10, end_time=12),
                dict(title='More', start_time=12, end_time=30)]
    model(monkeypatch, groups((0, 0), (1, 2)))
    result = chapters.adapt(original, duration=30)
    assert result['items'][-1] == dict(title='整理标题1', start_time=10, end_time=30, source_indices=[1, 2])


def test_live_rules_can_reduce_count_and_increase_duration(monkeypatch):
    calls = model(monkeypatch, groups((0, 1), (2, 3), (4, 5), (6, 7)))
    result = chapters.adapt(source(8), duration=80, rules=dict(limit=4, stay=12, space=0, max_title_length=16))
    assert len(calls) == 1
    assert len(result['items']) == 4
    rules = result['adaptation']['rules']
    assert (rules['max_chapters'], rules['max_boundaries'], rules['min_duration']) == (4, 3, 12)
    assert all(row['end_time'] - row['start_time'] >= 12 for row in result['items'][1:])


def test_loose_remote_rules_do_not_relax_conservative_hard_limits(monkeypatch):
    model(monkeypatch, groups((0, 1), (2, 3)))
    result = chapters.adapt(source(4), duration=40, rules=dict(limit=100, stay=1, space=0))
    assert result['adaptation']['rules']['max_chapters'] == 10
    assert result['adaptation']['rules']['min_duration'] == 5


def test_live_count_limit_is_validated_after_model_response(monkeypatch):
    model(monkeypatch, groups((0, 0), (1, 1), (2, 2)))
    with pytest.raises(PublishError) as error:
        chapters.adapt(source(3), duration=30, rules=dict(limit=2, stay=5, space=0))
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'


def test_live_minimum_duration_is_validated_after_model_response(monkeypatch):
    model(monkeypatch, groups((0, 0), (1, 1), (2, 2)))
    with pytest.raises(PublishError) as error:
        chapters.adapt(source(3), duration=30, rules=dict(limit=10, stay=12, space=0))
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'


def test_cancel_before_generation_is_not_wrapped_or_sent_to_llm():
    stopped = PublishError('已停止章节整理', code='PUBLISH_CHAPTER_PAUSED')

    def cancel():
        raise stopped

    with pytest.raises(PublishError) as error:
        chapters.adapt(source(), duration=30, cancel_check=cancel)
    assert error.value is stopped


def test_cancel_during_generation_preserves_original_error(monkeypatch):
    stopped = PublishError('已停止章节整理', code='PUBLISH_CHAPTER_PAUSED')
    cancelled = False

    def check():
        if cancelled:
            raise stopped

    def chat(prompt, *, cancel_check, **kwargs):
        nonlocal cancelled
        cancelled = True
        cancel_check()

    monkeypatch.setattr(chapters.llm, 'chat_json', chat)
    with pytest.raises(PublishError) as error:
        chapters.adapt(source(), duration=30, cancel_check=check)
    assert error.value is stopped


def test_failed_model_does_not_mutate_source_or_issue_remote_writes(monkeypatch):
    original = source(21)
    frozen = copy.deepcopy(original)

    def unavailable(*args, **kwargs):
        raise TimeoutError('模拟模型超时')

    monkeypatch.setattr(chapters.llm, 'chat_json', unavailable)
    with pytest.raises(PublishError) as error:
        chapters.adapt(original, duration=210)
    assert error.value.code == 'PUBLISH_CHAPTER_ADAPT_FAILED'
    assert original == frozen


def test_prompt_states_grouping_and_hard_limits_as_data_contract(monkeypatch):
    def inspect(prompt, kwargs):
        text = prompt + '\n' + (kwargs.get('system') or '')
        assert 'start_index' in text and 'end_index' in text
        assert '16' in text and '10' in text and '5' in text
        assert '相邻' in text and ('覆盖' in text or '遗漏' in text)
        assert '1' in text
    model(monkeypatch, groups((0, 0), (1, 2)), check=inspect)
    chapters.adapt(source(), duration=30)
