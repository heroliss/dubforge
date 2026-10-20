"""边缘纠错和零词清理的离线回归，不以 ASR 孤词重造独立字幕。"""
from copy import deepcopy

import pytest

from dubforge.pipeline import step_split
from dubforge.pipeline.semantic_pauses import PauseAlignmentError, repair_long_pauses
from dubforge.pipeline.speech_gaps import speech_gaps
from dubforge.utils.srt import SubLine


@pytest.fixture(autouse=True)
def isolated(isolated_env):
    """所有词轴均为内存夹具，不读取或修改生产资料。"""


def words(spans):
    return [dict(word=text, start=start, end=end) for text, start, end in spans]


def part(text, start, end):
    return dict(text=text, start=start, end=end)


def assert_empty_gaps(result, tokens):
    for gap in speech_gaps(tokens):
        assert not any(p['start'] < gap.end - .011 and p['end'] > gap.start + .011
                       for p in result)


def prepare(candidates, tokens, warnings):
    lines = [SubLine(i, w['start'], w['end'], w['word']) for i, w in enumerate(tokens, 1)]
    return step_split._prepare_semantic_parts(
        candidates, lines, ' '.join(w['word'] for w in tokens), tokens,
        20, 7, .6, warnings=warnings)[0]


def test_job299_the_line_keeps_candidate_phrase_instead_of_recreating_the():
    # 时间来自 #299 的原词轴；它是识别参考，不能据此声称核听过音频。
    tokens = words([('we', 101.66, 102.22), ('have', 102.22, 102.34),
                    ('20', 102.34, 102.58), ('minutes', 102.58, 102.86),
                    ('to', 102.86, 103.10), ('go', 103.10, 103.30),
                    ('the', 103.30, 104.50), ('line', 109.66, 109.98),
                    ("we're", 109.98, 110.20), ('about', 110.20, 110.34)])
    candidates = [part('We have 20 minutes to go', 101.66, 103.30),
                  part('The line', 109.66, 109.98),
                  part("we're about", 109.98, 110.34)]
    original, original_words = deepcopy(candidates), deepcopy(tokens)
    warnings = []
    result = prepare(candidates, tokens, warnings)
    assert result == original
    assert len(result) == 3 and result[1]['text'] == 'The line'
    assert_empty_gaps(result, tokens)
    assert candidates == original and tokens == original_words
    assert any('边缘文字的识别与声学归属仍需复核' in w for w in warnings)


def test_job299_and_just_like_that_trims_only_empty_leading_interval():
    tokens = words([('definitely', 413.03, 413.77), ('durian', 413.77, 414.83),
                    ('and', 414.83, 416.47), ('just', 422.58, 422.76),
                    ('like', 422.76, 423.00), ('that', 423.00, 423.22),
                    ("we've", 423.22, 423.62), ('made', 423.62, 423.86),
                    ('it', 423.86, 424.02)])
    candidates = [part('definitely not durian', 413.03, 414.83),
                  part('And just like that', 416.47, 423.22),
                  part("we've made it", 423.22, 424.02)]
    original = deepcopy(candidates)
    warnings = []
    result = prepare(candidates, tokens, warnings)
    assert [p['text'] for p in result] == [p['text'] for p in original]
    assert len(result) == 3 and result[1] == part('And just like that', 422.58, 423.22)
    assert result[0]['text'] == 'definitely not durian'
    assert_empty_gaps(result, tokens)
    assert candidates == original
    assert any('边缘文字的识别与声学归属仍需复核' in w for w in warnings)


@pytest.mark.parametrize('prefix', ['Station', 'Route', 'Ｃａｆｅ\u0301'])
def test_single_prefix_correction_uses_sound_region_without_a_function_word_whitelist(prefix):
    tokens = words([(prefix, 0, 1), ('entrance', 8, 9)])
    candidate = part(prefix + ' entrance.', 8, 9)
    warnings = []
    result = repair_long_pauses([candidate], tokens, .6, align_all=True, warnings=warnings)
    assert result == [candidate]
    assert_empty_gaps(result, tokens)
    assert len(warnings) == 1


def test_single_same_region_edge_replacement_preserves_all_model_characters():
    tokens = words([('first', 0, 1), ('the', 8, 8.2), ('entrance', 8.2, 9)])
    candidates = [part('first', 0, 1), part('An entrance!', 8, 9)]
    warnings = []
    result = repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=warnings)
    assert result == candidates
    assert_empty_gaps(result, tokens)
    assert warnings


def test_small_start_error_within_known_empty_gap_is_trimmed_to_anchor():
    tokens = words([('I', 0, 1), ('think', 8, 9)])
    candidates = [part('I', 0, 1), part('I think', 7.98, 9)]
    result = repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])
    assert result == [part('I', 0, 1), part('I think', 8, 9)]
    assert_empty_gaps(result, tokens)


def test_real_voice_on_both_sides_still_splits_and_preserves_text():
    tokens = words([('I', 0, 1), ('think', 8, 8.5), ('that', 8.5, 9)])
    text = 'I think that!'
    result = repair_long_pauses([part(text, 0, 9)], tokens, .6, align_all=True, warnings=[])
    assert [p['text'] for p in result] == ['I ', 'think that!']
    assert [(p['start'], p['end']) for p in result] == [(0, 1), (8, 9)]
    assert ''.join(p['text'] for p in result) == text
    assert_empty_gaps(result, tokens)


def test_single_side_exception_cannot_hide_a_second_gap_inside_the_phrase():
    tokens = words([('I', 0, 1), ('think', 8, 9), ('now', 16, 17)])
    text = 'I think now'
    result = repair_long_pauses([part(text, 8, 17)], tokens, .6, align_all=True, warnings=[])
    assert len(result) >= 2
    assert ''.join(p['text'] for p in result) == text
    assert_empty_gaps(result, tokens)


@pytest.mark.parametrize('text', ['I surely think', 'unknown new think', 'unknown think'])
def test_unknown_addition_does_not_get_the_known_edge_exception(text):
    tokens = words([('I', 0, 1), ('think', 8, 9)])
    with pytest.raises(PauseAlignmentError, match='长停顿'):
        repair_long_pauses([part(text, 8, 9)], tokens, .6, align_all=True, warnings=[])


def test_two_unique_words_cannot_be_transferred_from_the_other_sound_region():
    tokens = words([('book', 0, .5), ('tickets', .5, 1), ('arrive', 8, 9)])
    text = 'book tickets arrive'
    result = repair_long_pauses([part(text, 8, 9)], tokens, .6, align_all=True, warnings=[])
    assert len(result) == 2
    assert ''.join(p['text'] for p in result) == text
    assert [(p['start'], p['end']) for p in result] == [(0, 1), (8, 9)]
    assert_empty_gaps(result, tokens)


def test_another_candidate_in_current_gap_prevents_local_exception():
    tokens = words([('I', 0, 1), ('think', 8, 9)])
    candidates = [part('I', 3, 4), part('I think', 8, 9)]
    with pytest.raises(PauseAlignmentError, match='长停顿'):
        repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])


def test_remote_tail_extension_does_not_block_current_boundary_correction():
    tokens = words([('the', 0, 1), ('line', 8, 9), ('goodbye', 10, 11)])
    candidates = [part('The line', 8, 9), part('goodbye', 10, 11.4)]
    result = repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])
    assert result == [part('The line', 8, 9), part('goodbye', 10, 11)]
    assert_empty_gaps(result, tokens)


def test_remote_repeated_edge_word_is_not_confused_with_the_adjacent_word():
    tokens = words([('I', 0, 1), ('know', 1, 2), ('I', 10, 11), ('think', 18, 19)])
    candidates = [part('I', 0, 1), part('know', 1, 2), part('I think', 18, 19)]
    result = repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])
    assert result == candidates
    assert_empty_gaps(result, tokens)


def test_deleted_interior_zero_word_does_not_block_or_change_original_asr():
    tokens = words([('We', 0, .4), ('arrived', .4, 2.8), ('The', 3, 3), ('Yeah!', 8, 8.3)])
    candidates = [part('We arrived.', 0, 2.8), part('Yeah!', 8, 8.3)]
    original = deepcopy(tokens)
    result = repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])
    assert result == candidates
    assert tokens == original
    assert_empty_gaps(result, tokens)


@pytest.mark.parametrize('candidates', [
    [part('We arrived. The', 0, 2.8), part('Yeah!', 8, 8.3)],
    [part('We arrived.', 0, 2.8), part('The Yeah!', 8, 8.3)],
    [part('We arrived.', 0, 2.8), part('The', 3, 3.4), part('Yeah!', 8, 8.3)],
])
def test_retained_interior_zero_word_still_has_no_time_to_invent(candidates):
    tokens = words([('We', 0, .4), ('arrived', .4, 2.8), ('The', 3, 3), ('Yeah!', 8, 8.3)])
    with pytest.raises(PauseAlignmentError, match='零时长词'):
        repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])


def test_deleted_zero_word_is_not_proven_by_global_count_when_another_occurrence_is_deleted():
    tokens = words([('The', 0, .5), ('arrival', .5, 1), ('The', 3, 3), ('gate', 8, 9)])
    candidates = [part('arrival', .5, 1), part('The gate', 8, 9)]
    with pytest.raises(PauseAlignmentError, match='零时长词'):
        repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])


def test_deleted_zero_word_can_keep_the_separate_positive_duration_occurrence():
    tokens = words([('The', 0, .5), ('arrival', .5, 1), ('The', 3, 3), ('gate', 8, 9)])
    candidates = [part('The arrival', 0, 1), part('gate', 8, 9)]
    result = repair_long_pauses(candidates, tokens, .6, align_all=True, warnings=[])
    assert result == candidates
    assert_empty_gaps(result, tokens)


@pytest.mark.parametrize('spans,text,start,end', [
    ([('would', 0, 1), ('go', 8, 9)], 'would go', 0, 1),
    ([('我', 0, 1), ('去', 8, 9)], '我去', 0, 1),
    ([('would', 0, .5), ('think', .5, 1), ('go', 8, 9)], 'would go', 0, 1),
    ([('would', 0, 1), ('go', 8, 8.5), ('think', 8.5, 9)], 'would think', 8, 9),
])
def test_opposite_unique_word_cannot_use_suffix_addition_or_edge_replacement(spans, text, start, end):
    tokens = words(spans)
    candidate = part(text, start, end)
    try:
        result = repair_long_pauses([candidate], tokens, .6, align_all=True, warnings=[])
    except PauseAlignmentError:
        return
    assert result != [candidate]
    assert ''.join(p['text'] for p in result) == text
    assert_empty_gaps(result, tokens)


def test_mirror_single_prefix_has_visible_limited_policy_even_for_a_content_word():
    # 同样的一个前缀也可能是实词；时间轴无法裁定正确性，必须明确提示复核。
    tokens = words([('would', 0, 1), ('go', 8, 9)])
    candidate = part('would go', 8, 9)
    warnings = []
    result = repair_long_pauses([candidate], tokens, .6, align_all=True, warnings=warnings)
    assert result == [candidate]
    assert len(warnings) == 1 and '边缘文字的识别与声学归属仍需复核' in warnings[0]
    assert_empty_gaps(result, tokens)


def test_only_zero_words_cannot_supply_a_sound_region_for_boundary_correction():
    tokens = words([('first', 0, 1), ('the', 4, 4), ('line', 8, 9)])
    with pytest.raises(PauseAlignmentError, match='零时长词'):
        repair_long_pauses([part('the line', 8, 9)], tokens, .6,
                           align_all=True, warnings=[])


def test_cancellation_during_boundary_work_remains_visible_and_non_mutating():
    class Cancelled(Exception):
        pass
    tokens = words([('the', 0, 1), ('line', 8, 9)])
    candidate = part('The line', 8, 9)
    before = deepcopy(candidate)
    def cancel():
        raise Cancelled('取消')
    with pytest.raises(Cancelled):
        repair_long_pauses([candidate], tokens, .6, align_all=True,
                           warnings=[], cancel_check=cancel)
    assert candidate == before
