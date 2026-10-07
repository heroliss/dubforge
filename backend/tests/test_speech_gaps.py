"""统一无台词区边界：真实并集、零时长文字与安全输入，全部离线。"""
import pytest

from dubforge.pipeline.speech_gaps import SpeechGap, speech_gaps
from dubforge.runner import StepCancelled


@pytest.fixture(autouse=True)
def isolated_gap_environment(isolated_env):
    """保证领域模块的间接导入只使用隔离配置和数据根目录。"""


def _words(*times):
    return [dict(start=start, end=end, word=f"word{index}")
            for index, (start, end) in enumerate(times)]


@pytest.mark.parametrize("duration,expected", [(1.999, False), (2.0, True), (2.5, True)])
def test_two_seconds_is_the_inclusive_shared_boundary(duration, expected):
    result = speech_gaps(_words((0, 1), (1 + duration, 2 + duration)))
    assert result == ([SpeechGap(1, 1 + duration, 0, 1, 1)] if expected else [])


def test_decimal_two_second_boundary_is_not_lost_to_binary_float_rounding():
    assert 9.11 - 7.11 < 2
    assert speech_gaps(_words((6, 7.11), (9.11, 10))) == [SpeechGap(7.11, 9.11, 0, 1, 1)]


def test_nested_words_do_not_create_a_false_gap_or_shorten_coverage():
    words = _words((0, 10), (1, 2), (5, 6), (12, 13))
    assert speech_gaps(words) == [SpeechGap(10, 12, 0, 3, 3)]
    assert words == _words((0, 10), (1, 2), (5, 6), (12, 13))


def test_overlapping_words_extend_the_union_before_the_next_gap():
    assert speech_gaps(_words((0, 4), (2, 7), (9, 10))) == [SpeechGap(7, 9, 1, 2, 2)]


def test_zero_duration_words_do_not_create_or_extend_speech():
    assert speech_gaps(_words((0, 1), (4, 4), (8, 9))) == [SpeechGap(1, 8, 0, 2, 2)]
    assert speech_gaps(_words((0, 0), (5, 5), (9, 9))) == []
    assert speech_gaps(_words((0, 1), (8, 8))) == []


def test_zero_at_right_boundary_keeps_its_text_index_on_the_right():
    assert speech_gaps(_words((0, 1), (3, 3), (3, 3), (3, 4))) == [
        SpeechGap(1, 3, 0, 3, 1)]


def test_zero_at_left_boundary_keeps_its_text_index_on_the_left():
    assert speech_gaps(_words((0, 1), (1, 1), (3, 4))) == [SpeechGap(1, 3, 0, 2, 2)]


def test_caller_can_use_a_larger_threshold_without_changing_union_semantics():
    assert speech_gaps(_words((0, 1), (3, 4), (8, 9)), threshold=3) == [
        SpeechGap(4, 8, 1, 2, 2)]


@pytest.mark.parametrize("words", [
    [{"start": 0}], [{"start": "bad", "end": 1}],
    _words((-1, 1)), _words((1, 0)), _words((0, float("inf"))),
    _words((float("nan"), 1)), _words((1, 2), (0, 1)),
])
def test_invalid_word_timing_fails_instead_of_sorting_or_inventing_a_gap(words):
    with pytest.raises(ValueError, match="时间轴无效"):
        speech_gaps(words)


@pytest.mark.parametrize("threshold", [0, -1, float("inf"), float("nan"), "bad", None])
def test_invalid_gap_threshold_is_rejected(threshold):
    with pytest.raises(ValueError, match="有限的正秒数"):
        speech_gaps([], threshold)


def test_cancellation_during_a_long_word_axis_propagates_unchanged():
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise StepCancelled("用户取消")

    with pytest.raises(StepCancelled, match="用户取消"):
        speech_gaps(_words(*[(index * 3, index * 3 + 1) for index in range(200)]),
                    cancel_check=cancel)
