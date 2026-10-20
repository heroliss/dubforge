"""重复声学锚点的离线回归；不依据词表删除或移动候选文字。"""
import pytest

from dubforge.pipeline.semantic_pauses import PauseAlignmentError, repair_long_pauses
from dubforge.pipeline.speech_gaps import speech_gaps


@pytest.fixture(autouse=True)
def isolated_acoustic_environment(isolated_env):
    """词轴全由内存构造，数据根与配置均使用临时目录。"""


def word(text, start, end):
    return dict(word=text, start=start, end=end)


def part(text, start, end):
    return dict(text=text, start=start, end=end)


def repeated_words():
    return [word('I', 0, 1), word('I', 8, 8.2), word('think', 8.2, 9)]


def assert_gaps_are_empty(result, words):
    for gap in speech_gaps(words):
        assert not any(p['start'] < gap.end - .011 and p['end'] > gap.start + .011
                       for p in result)


@pytest.mark.parametrize('text', ['I think', 'Ｉ, think!', 'I\tthink'])
def test_full_phrase_in_unique_model_region_keeps_text_and_gap(text):
    words = repeated_words()
    candidate = part(text, 8, 9)
    warnings = []
    result = repair_long_pauses([candidate], words, .6, align_all=True, warnings=warnings)
    assert result == [candidate]
    assert_gaps_are_empty(result, words)
    assert len(warnings) == 1 and '完整短语与本侧词轴精确对应' in warnings[0]
    assert '未据此删改文字' in warnings[0]


def test_complete_phrase_match_is_not_a_pronoun_whitelist():
    words = [word('station', 0, 1), word('station', 8, 8.5), word('entrance', 8.5, 9)]
    candidate = part('Station entrance.', 8, 9)
    result = repair_long_pauses([candidate], words, .6, align_all=True, warnings=[])
    assert result == [candidate]
    assert_gaps_are_empty(result, words)


def test_another_later_gap_cannot_pull_the_proven_phrase_back_again():
    words = repeated_words() + [word('then', 15, 16), word('leave', 16, 17)]
    candidates = [part('I think', 8, 9), part('then leave', 15, 17)]
    result = repair_long_pauses(candidates, words, .6, align_all=True, warnings=[])
    assert result == candidates
    assert_gaps_are_empty(result, words)


def test_full_prepare_keeps_the_precise_duplicate_phrase_region():
    from dubforge.pipeline import step_split
    from dubforge.utils.srt import SubLine
    words = repeated_words()
    candidates = [part('I think', 8, 9)]
    lines = [SubLine(1, 0, 1, 'I'), SubLine(2, 8, 9, 'I think')]
    result, _ = step_split._prepare_semantic_parts(
        candidates, lines, 'I I think', words, 50, 7, .6, warnings=[])
    assert result == candidates
    assert_gaps_are_empty(result, words)


def test_both_proven_occurrences_are_preserved_when_both_are_in_the_candidate():
    words = repeated_words()
    candidates = [part('I', 0, 1), part('I think', 8, 9)]
    result = repair_long_pauses(candidates, words, .6, align_all=True, warnings=[])
    assert result == candidates
    assert_gaps_are_empty(result, words)


def test_single_edge_correction_keeps_complete_later_phrase_and_reports_uncertainty():
    words = [word('I', 0, 1), word('think', 8, 9)]
    candidate = part('I think', 8, 9)
    warnings = []
    result = repair_long_pauses([candidate], words, .6, align_all=True, warnings=warnings)
    assert result == [candidate]
    assert len(warnings) == 1 and '边缘文字的识别与声学归属仍需复核' in warnings[0]
    assert_gaps_are_empty(result, words)


@pytest.mark.parametrize('start,end', [(8.05, 9), (8, 8.95), (4, 9), (-1, 9)])
def test_unknown_or_silence_covering_model_time_cannot_claim_the_exact_region(start, end):
    words = repeated_words()
    candidate = part('I think', start, end)
    warnings = []
    result = repair_long_pauses([candidate], words, .6, align_all=True, warnings=warnings)
    assert result != [candidate]
    assert ''.join(p['text'] for p in result) == candidate['text']
    assert_gaps_are_empty(result, words)
    assert not any('完整短语与本侧词轴精确对应' in warning for warning in warnings)


def test_whole_candidate_really_crossing_gap_still_is_split():
    words = repeated_words()
    candidate = part('I I think', 0, 9)
    result = repair_long_pauses([candidate], words, .6, align_all=True, warnings=[])
    assert len(result) == 2
    assert ''.join(p['text'] for p in result) == candidate['text']
    assert_gaps_are_empty(result, words)


def test_unknown_addition_crossing_gap_still_fails_instead_of_guessing():
    words = repeated_words()
    with pytest.raises(PauseAlignmentError):
        repair_long_pauses([part('I surely I think', 0, 9)], words, .6,
                           align_all=True, warnings=[])


def test_candidate_in_silence_cannot_hide_behind_a_valid_phrase():
    words = repeated_words()
    candidates = [part('I', 3, 4), part('I think', 8, 9)]
    result = repair_long_pauses(candidates, words, .6, align_all=True, warnings=[])
    assert result != candidates
    assert_gaps_are_empty(result, words)


def test_repeated_tied_acoustic_regions_do_not_supply_unique_proof():
    words = [word('I', 0, 1), word('I', 8, 9), word('think', 8, 9),
             word('I', 8, 9), word('think', 8, 9)]
    candidate = part('I think', 8, 9)
    warnings = []
    result = repair_long_pauses([candidate], words, .6, align_all=True, warnings=warnings)
    assert result != [candidate]
    assert not any('完整短语与本侧词轴精确对应' in warning for warning in warnings)
    assert_gaps_are_empty(result, words)


@pytest.mark.parametrize('align_all,expected_start', [(False, 1.5), (True, 0)])
def test_no_long_gap_keeps_the_original_optional_alignment_behaviour(align_all, expected_start):
    words = [word('I', 0, 1), word('I', 1.5, 1.7), word('think', 1.7, 2)]
    candidate = part('I think', 1.5, 2)
    warnings = []
    result = repair_long_pauses([candidate], words, .6, align_all=align_all, warnings=warnings)
    assert result == [dict(candidate, start=expected_start)]
    assert warnings == []


def test_exact_region_protection_keeps_cancellation_visible():
    class Cancelled(Exception):
        pass
    def cancel():
        raise Cancelled('取消')
    with pytest.raises(Cancelled):
        repair_long_pauses([part('I think', 8, 9)], repeated_words(), .6,
                           cancel_check=cancel, align_all=True, warnings=[])
