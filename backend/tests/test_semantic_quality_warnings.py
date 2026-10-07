"""内容启发式只提示复核；明确时间问题仍阻断，全为隔离离线数据。"""
from copy import deepcopy

import pytest

from dubforge.pipeline.semantic_quality import (
    semantic_boundary_warnings,
    semantic_pause_issues,
    semantic_quality_issues,
)


@pytest.fixture(autouse=True)
def isolated(isolated_env):
    """不读取生产配置或产物，也不请求模型。"""


def part(text, start, end):
    return {"text": text, "start": start, "end": end}


@pytest.mark.parametrize("ending", ["the", "a", "I", "and", "because", "in", "with"])
def test_neighboring_word_shapes_only_warn_with_timing_evidence(ending):
    parts = [part("we stopped " + ending, 0, 1), part("China", 1.1, 1.4)]
    original = deepcopy(parts)
    assert semantic_quality_issues(parts, .6) == []
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 2
    assert f"句尾{ending.lower()!r}可能接续第2句" in warnings[0]
    assert "间隔0.100秒" in warnings[0]
    assert "0.300秒" in warnings[1] and "不足0.5秒" in warnings[1]
    assert all("不能确认错误" in warning for warning in warnings)
    assert parts == original


@pytest.mark.parametrize("text", ["so", "China", "继续", "然后"])
def test_short_connected_expression_is_not_a_draft_failure(text):
    parts = [part(text, 0, .2), part("we continue the journey", .2, 2)]
    assert semantic_quality_issues(parts, .6) == []
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 1
    assert "第1句（0.200秒" in warnings[0]
    assert "正常短表达" in warnings[0]


@pytest.mark.parametrize("text", ["yes", "no", "嗯", "好", "42", "China!", "Who am I?"])
def test_independent_answers_and_complete_punctuation_do_not_need_review(text):
    parts = [part(text, 0, .2), part("we continue the journey", .2, 2)]
    assert semantic_quality_issues(parts, .6) == []
    assert semantic_boundary_warnings(parts, .6) == []


@pytest.mark.parametrize("text", ["the", "I", "and", "in"])
def test_real_pause_does_not_make_an_isolated_word_an_inferred_boundary_error(text):
    parts = [part(text, 0, .2), part("we continue the journey", 3, 5)]
    assert semantic_quality_issues(parts, .6) == []
    assert semantic_boundary_warnings(parts, .6) == []


def test_complete_preposition_at_end_is_not_inferred_to_belong_to_next_sentence():
    parts = [part("these are drinks you can mix it with", 0, 2),
             part("we took a seat and looked outside", 2, 4)]
    assert semantic_quality_issues(parts, .6) == []
    assert semantic_boundary_warnings(parts, .6) == []


def test_true_overlap_is_still_a_hard_issue_even_with_only_short_content():
    parts = [part("hello", 0, 1), part("again", .98, 2)]
    issues = semantic_quality_issues(parts, .6)
    assert len(issues) == 1 and "与上一句时间重叠" in issues[0]


@pytest.mark.parametrize("text", ["this whole sentence cannot fit in a tenth of a second",
                                     "这段话明显无法在如此短暂的时间内正常说完"])
def test_extreme_density_has_timing_evidence_but_does_not_reject_the_draft(text):
    parts = [part(text, 0, .1)]
    assert semantic_quality_issues(parts, .6) == []
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 1 and "文字密度较高" in warnings[0]
    assert "/0.100秒" in warnings[0] and "不能确认错误" in warnings[0]


def test_fast_but_plausible_short_phrase_is_not_an_extreme_density_failure():
    assert semantic_quality_issues([part("we made it", 0, .2)], .6) == []


def test_boundary_warning_count_is_bounded_without_changing_any_parts():
    parts = [part("the", i * .2, (i + 1) * .2) for i in range(30)]
    original = deepcopy(parts)
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 8
    assert "第1句" in warnings[0] and "第8句" in warnings[-1]
    assert all("不能确认错误" in warning for warning in warnings)
    assert parts == original


def test_short_tail_and_short_time_are_one_bounded_warning_per_part():
    parts = [part("I", 0, .2), part("think", .2, .4)]
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 2
    assert "不足0.5秒" in warnings[0] and "句尾'i'" in warnings[0]
    assert warnings[0].count("不能确认错误") == 1


@pytest.mark.parametrize("pause", [.1, .6, 1, 2])
@pytest.mark.parametrize("gap,hard_boundary", [(1.999, False), (2, True), (2.001, True)])
def test_silence_quality_uses_independent_two_second_boundary(pause, gap, hard_boundary):
    words = [{"word": "one", "start": 0, "end": 1},
             {"word": "again", "start": 1 + gap, "end": 2 + gap}]
    parts = [part("one again", 0, 2 + gap)]
    issues = semantic_pause_issues(parts, words, pause)
    assert bool(issues) is hard_boundary


def test_overlapping_nested_word_ranges_do_not_create_false_silence_quality_failure():
    words = [{"word": "first", "start": 0, "end": 20},
             {"word": "second", "start": 2, "end": 3},
             {"word": "third", "start": 10, "end": 11}]
    assert semantic_pause_issues([part("first second third", 0, 20)], words, .6) == []


def test_real_gap_after_nested_word_ranges_starts_after_all_known_speech():
    words = [{"word": "first", "start": 0, "end": 20},
             {"word": "second", "start": 2, "end": 3},
             {"word": "third", "start": 10, "end": 11},
             {"word": "last", "start": 23, "end": 24}]
    issues = semantic_pause_issues([part("first second third last", 0, 24)], words, .6)
    assert len(issues) == 1
    assert "20.00–23.00秒" in issues[0] and "的3.00秒" in issues[0]
