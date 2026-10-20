"""仅验证本地证据提示规则；模型桩和概率不证明真实音频内容。"""
from copy import deepcopy

import pytest

from dubforge.pipeline.semantic_recognition_hints import (
    MAX_RECOGNITION_HINTS, recognition_hints,
)
from dubforge.utils.srt import SubLine


@pytest.fixture(autouse=True)
def isolated_recognition_environment(isolated_env):
    """所有领域导入使用隔离数据根目录。"""


def word(text=" Yes.", start=0, end=.2, **evidence):
    return dict(word=text, start=start, end=end, **evidence)


def test_legacy_and_platform_without_evidence_are_unknown():
    line = SubLine(1, 0, .4, "Yes.")
    assert recognition_hints([line], []) == {}
    assert recognition_hints([line], [word()]) == {}


def test_zero_duration_short_answer_is_not_deleted_or_retimed():
    lines = [SubLine(7, 1, 1.4, "Yes!")]
    words = [word(" Yes!", 1, 1)]
    original = deepcopy((lines, words))
    hints = recognition_hints(lines, words)
    assert set(hints) == {7}
    assert "零时长" in hints[7][0]
    assert "不能据此确定" in hints[7][0]
    assert "不能单凭这项信息删除文字或补造时间" in hints[7][0]
    assert (lines, words) == original


@pytest.mark.parametrize("evidence", [
    dict(probability=.1), dict(no_speech_prob=.9),
    dict(probability=.1, no_speech_prob=.2),
    dict(probability=.8, no_speech_prob=.9),
    dict(probability=.35, no_speech_prob=.9),
    dict(probability=.1, no_speech_prob=.6),
])
def test_short_time_or_one_probability_does_not_judge_a_word(evidence):
    assert recognition_hints([SubLine(1, 0, .4, "Yes.")],
                             [word(end=.01, **evidence)]) == {}


def test_joint_evidence_remains_a_review_hint_for_possible_quiet_speech():
    lines = [SubLine(2, 3, 3.4, "All right.")]
    words = [word(" All", 3, 3.1, probability=.2, no_speech_prob=.8),
             word(" right.", 3.1, 3.2, probability=.3, no_speech_prob=.8)]
    original = deepcopy((lines, words))
    hint = recognition_hints(lines, words)[2][0]
    assert "联合识别疑点" in hint and "不能确认无声或误词" in hint
    assert (lines, words) == original


def test_partial_joint_evidence_does_not_pollute_the_whole_short_phrase():
    lines = [SubLine(1, 0, .4, "All right.")]
    words = [word(" All", 0, .1, probability=.2, no_speech_prob=.8),
             word(" right.", .1, .2, probability=.9, no_speech_prob=.8)]
    assert recognition_hints(lines, words) == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1.1, True, ".1"])
def test_invalid_optional_probability_is_ignored(value):
    lines = [SubLine(1, 0, .4, "Yes.")]
    assert recognition_hints(lines, [word(probability=value, no_speech_prob=.9)]) == {}
    assert recognition_hints(lines, [word(probability=.1, no_speech_prob=value)]) == {}


def test_mapping_uses_original_line_id_and_exact_source_text():
    lines = [SubLine(9, 0, .4, "Yes."), SubLine(15, 2, 2.4, "No.")]
    words = [word(probability=.1, no_speech_prob=.9),
             word(" No.", 2, 2.2, probability=.8, no_speech_prob=.1)]
    assert set(recognition_hints(lines, words)) == {9}
    assert recognition_hints([SubLine(9, 0, .4, "Another word.")], words) == {}


def test_ambiguous_overlap_or_shared_boundary_does_not_attach_neighbor_evidence():
    words = [word(start=1, end=1, probability=.1, no_speech_prob=.9)]
    assert recognition_hints([SubLine(1, .8, 1, "Yes."),
                              SubLine(2, 1, 1.4, "Yes.")], words) == {}
    assert recognition_hints([SubLine(1, 1, 1.4, "Yes."),
                              SubLine(2, .9, 1.3, "Yes.")], words) == {}


def test_millisecond_srt_rounding_is_allowed_without_widening_audio():
    lines = [SubLine(1, 1, 1.2, "Yes.")]
    words = [word(start=.9996, end=1.2004, probability=.1, no_speech_prob=.9)]
    assert 1 in recognition_hints(lines, words)
    assert words[0]["start"] == .9996 and words[0]["end"] == 1.2004


def test_four_word_source_and_malformed_timing_do_not_create_hints():
    lines = [SubLine(1, 0, 1, "One two three four.")]
    words = [word(text, i * .2, i * .2 + .1, probability=.1, no_speech_prob=.9)
             for i, text in enumerate([" One", " two", " three", " four."])]
    assert recognition_hints(lines, words) == {}
    assert recognition_hints([SubLine(1, 0, .4, "Yes.")],
                             [word(start=float("nan"))]) == {}
    assert recognition_hints([SubLine(1, 0, .4, "Yes.")],
                             [word(start=.3, end=.1)]) == {}


def test_total_limit_does_not_change_or_remove_unreported_source_items():
    lines = [SubLine(i + 1, i * 2, i * 2 + .4, "I") for i in range(40)]
    words = [word(" I", i * 2, i * 2, probability=.1, no_speech_prob=.9)
             for i in range(40)]
    original = deepcopy((lines, words))
    hints = recognition_hints(lines, words)
    assert sum(map(len, hints.values())) == MAX_RECOGNITION_HINTS
    assert all(len(values) <= 2 for values in hints.values())
    assert (lines, words) == original


def test_duplicate_line_ids_are_not_guessed():
    assert recognition_hints([SubLine(1, 0, .4, "Yes."), SubLine(1, 2, 2.4, "Yes.")],
                             [word(start=0, end=0), word(start=2, end=2)]) == {}
