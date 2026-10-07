"""长静音确定性修复：候选文字保真、声学边界和取消，全为离线数据。"""
import pytest

from dubforge.pipeline.semantic_pauses import PauseAlignmentError, repair_long_pauses
from dubforge.runner import StepCancelled


@pytest.fixture(autouse=True)
def isolated_acoustic_environment(isolated_env):
    """声学回归仅使用内存数据和隔离数据根目录，不读取真实项目。"""


def _words(spans):
    return [dict(start=start, end=end, word=text) for start, end, text in spans]


def _part(text, start=0, end=10):
    return dict(text=text, start=start, end=end)


def test_actual_i_before_six_second_pause_keeps_meaningful_half_sentence():
    words = _words([(37.76, 38.84, " i"), (44.92, 45.06, " think"),
                    (45.06, 45.2, " that"), (45.2, 45.34, " the"),
                    (45.34, 45.8, " biggest")])
    text = "I think that the biggest"
    result = repair_long_pauses([_part(text, 37.76, 45.8)], words, .6)
    assert [part["text"] for part in result] == ["I ", "think that the biggest"]
    assert [(part["start"], part["end"]) for part in result] == [(37.76, 38.84), (44.92, 45.8)]
    assert "".join(part["text"] for part in result) == text


def test_actual_theres_outside_model_time_is_not_lost_or_moved_to_about():
    words = _words([(77.74, 78.96, " there's"), (91.44, 91.58, " about"),
                    (91.58, 92.06, " 30"), (92.06, 92.46, " gates")])
    text = "There's about 30 gates."
    result = repair_long_pauses([_part(text, 91.44, 92.46)], words, .6)
    assert [part["text"] for part in result] == ["There's ", "about 30 gates."]
    assert [(part["start"], part["end"]) for part in result] == [(77.74, 78.96), (91.44, 92.46)]
    assert "".join(part["text"] for part in result) == text


def test_minor_correction_at_one_side_of_pause_uses_source_anchor_without_replacing_text():
    words = _words([(0, 1, "there's"), (8, 8.5, "about"), (8.5, 10, "thirty")])
    text = "There are  about thirty!"
    result = repair_long_pauses([_part(text)], words, .6)
    assert [part["text"] for part in result] == ["There are  ", "about thirty!"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (8, 10)]
    assert "".join(part["text"] for part in result) == text


def test_multiple_pauses_preserve_whitespace_punctuation_and_open_quotes():
    words = _words([(0, 1, "I"), (5, 6, "think"), (12, 13, "yes")])
    text = " I,\t “think”;  yes! \n"
    result = repair_long_pauses([_part(text, 0, 13)], words, .6)
    assert [part["text"] for part in result] == [" I,\t ", "“think”;  ", "yes! \n"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (5, 6), (12, 13)]
    assert "".join(part["text"] for part in result) == text


def test_cjk_keeps_all_characters_at_actual_acoustic_boundary():
    words = _words([(0, 1, "我们"), (6, 8, "出发")])
    text = "我们，出发！"
    result = repair_long_pauses([_part(text, 0, 8)], words, .6)
    assert [part["text"] for part in result] == ["我们，", "出发！"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (6, 8)]


def test_already_separated_text_with_wrong_silence_covering_end_is_trimmed():
    words = _words([(0, 1, "one"), (8, 10, "example")])
    result = repair_long_pauses([_part("one", 0, 8), _part("example", 8, 10)], words, .6)
    assert [(part["text"], part["start"], part["end"]) for part in result] == [
        ("one", 0, 1), ("example", 8, 10)]


def test_repeated_phrases_stay_in_their_own_order_without_searching_to_another_occurrence():
    words = _words([(0, .5, "we"), (.5, 1, "agree"),
                    (7, 7.5, "we"), (7.5, 8, "agree")])
    text = "we agree we agree"
    result = repair_long_pauses([_part(text, 0, 8)], words, .6)
    assert [part["text"] for part in result] == ["we agree ", "we agree"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (7, 8)]


def test_long_corrected_document_uses_bounded_local_mapping_and_keeps_source_gap():
    words = _words([(i / 2, (i + 1) / 2, word)
                    for i, word in enumerate(("This complete sentence describes travel. " * 140).split())])
    beginning = "This completed sentence describes travel. " + "This complete sentence describes travel. " * 139
    words.extend(_words([(400, 401, "one"), (415, 416, "example")]))
    text = beginning + "one example"
    assert len(text) > 4000
    result = repair_long_pauses([_part(text, 0, 416)], words, .6)
    assert "".join(part["text"] for part in result) == text
    assert [(part["start"], part["end"]) for part in result][-2:] == [(400, 401), (415, 416)]


def test_unprovable_replacement_across_gap_is_typed_error_not_silent_time_guess():
    words = _words([(0, 1, "first"), (8, 9, "second")])
    with pytest.raises(PauseAlignmentError, match="长停顿"):
        repair_long_pauses([_part("unrelated invented passage", 0, 9)], words, .6)


def test_added_word_at_gap_is_not_arbitrarily_assigned_to_either_side():
    words = _words([(0, 1, "I"), (8, 9, "think")])
    with pytest.raises(PauseAlignmentError, match="新增文字"):
        repair_long_pauses([_part("I surely think", 0, 9)], words, .6)


@pytest.mark.parametrize("gap,pause", [(1.9, .6), (1.999, 1)])
def test_short_pause_preserves_candidate(gap, pause):
    words = _words([(0, 1, "one"), (1 + gap, 2 + gap, "example")])
    candidate = _part("one example", 0, 2 + gap)
    assert repair_long_pauses([candidate], words, pause) == [candidate]


@pytest.mark.parametrize("pause", [.6, 1, 10])
def test_two_second_word_gap_is_split_independently_of_phrase_pause_setting(pause):
    words = _words([(0, 1, "one"), (3, 4, "example")])
    text = "One,  example!"
    result = repair_long_pauses([_part(text, 0, 4)], words, pause)
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (3, 4)]
    assert "".join(part["text"] for part in result) == text


def test_nested_source_words_keep_the_true_union_end_at_the_text_cut():
    words = _words([(0, 10, "long"), (1, 2, "inside"), (5, 6, "later"),
                    (12, 13, "after")])
    text = "long inside later after"
    result = repair_long_pauses([_part(text, 0, 13)], words, .6)
    assert [part["text"] for part in result] == ["long inside later ", "after"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 10), (12, 13)]
    assert "".join(part["text"] for part in result) == text


def test_nested_source_words_do_not_force_a_cut_within_existing_speech():
    words = _words([(0, 10, "long"), (1, 2, "inside"), (5, 6, "later")])
    candidate = _part("long inside later", 0, 10)
    assert repair_long_pauses([candidate], words, .6) == [candidate]


def test_zero_duration_text_at_next_speech_start_stays_with_that_speech():
    words = _words([(0, 1, "first"), (3, 3, "now"), (3, 4, "last")])
    text = "first now last"
    result = repair_long_pauses([_part(text, 0, 4)], words, .6)
    assert [part["text"] for part in result] == ["first ", "now last"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (3, 4)]
    assert "".join(part["text"] for part in result) == text


@pytest.mark.parametrize("words,candidate", [
    (_words([(0, 1, "first"), (4, 4, "now")]), _part("first now", 0, 1)),
    (_words([(0, 0, "now"), (4, 5, "first")]), _part("now first", 4, 5)),
])
def test_zero_duration_outer_word_does_not_extend_optional_reanchor(words, candidate):
    assert repair_long_pauses([candidate], words, .6, align_all=True) == [candidate]


def test_tail_zero_word_does_not_extend_a_segment_created_by_a_real_gap():
    words = _words([(0, 1, "first"), (3, 4, "last"), (6, 6, "now")])
    text = "first last now"
    result = repair_long_pauses([_part(text, 0, 4)], words, .6, align_all=True)
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (3, 4)]
    assert "".join(part["text"] for part in result) == text


def test_already_separated_correction_at_two_second_gap_uses_regions_not_word_shape():
    words = _words([(0, 1, "would"), (3, 4, "go")])
    candidates = [_part("won't", 0, 1), _part("leave", 3, 4)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True,
                              warnings=warnings) == candidates
    assert len(warnings) == 1 and "声学对应位置需复核" in warnings[0]


def test_nfkc_case_and_combining_accents_preserve_original_spelling():
    words = _words([(0, 1, "Café"), (7, 8, "YES")])
    text = "Ｃａｆｅ\u0301！ yes."
    result = repair_long_pauses([_part(text, 0, 8)], words, .6)
    assert "".join(part["text"] for part in result) == text
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (7, 8)]


def test_cancellation_preserves_exception_and_does_not_mutate_candidates():
    words = _words([(0, 1, "one"), (8, 9, "example")])
    candidate = _part("one example", 0, 9)

    def cancel():
        raise StepCancelled("用户取消")

    with pytest.raises(StepCancelled):
        repair_long_pauses([candidate], words, .6, cancel_check=cancel)
    assert candidate == _part("one example", 0, 9)


def test_zero_duration_whisper_word_keeps_text_without_inventing_speech_time():
    words = _words([(0, 1, "so"), (1, 1, "now"), (1, 2, "as"),
                    (8, 9, "expected")])
    text = "so now as expected"
    result = repair_long_pauses([_part(text, 0, 9)], words, .6)
    assert [part["text"] for part in result] == ["so now as ", "expected"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 2), (8, 9)]
    assert "".join(part["text"] for part in result) == text


def test_zero_duration_word_before_gap_stays_with_existing_positive_speech():
    words = _words([(0, 1, "so"), (1, 1, "now"), (8, 9, "yes")])
    result = repair_long_pauses([_part("so now yes", 0, 9)], words, .6)
    assert [part["text"] for part in result] == ["so now ", "yes"]
    assert [(part["start"], part["end"]) for part in result] == [(0, 1), (8, 9)]


def test_isolated_zero_duration_word_has_no_independent_time_to_invent():
    words = _words([(0, 1, "first"), (8, 8, "now"), (15, 16, "last")])
    candidate = _part("first now last", 0, 16)
    with pytest.raises(PauseAlignmentError, match="没有有效语音时间"):
        repair_long_pauses([candidate], words, .6)
    assert candidate == _part("first now last", 0, 16)


@pytest.mark.parametrize("left,right,words", [
    (_part("I", 37.76, 38.84), _part("I think that", 44.92, 45.2),
     _words([(37.76, 38.84, "I"), (44.92, 45.06, "think"), (45.06, 45.2, "that")])),
    (_part("There's", 77.74, 78.96), _part("There's about 30 gates.", 91.44, 92.46),
     _words([(77.74, 78.96, "there's"), (91.44, 91.58, "about"),
             (91.58, 92.06, "30"), (92.06, 92.46, "gates")])),
])
def test_actual_already_separated_short_prefix_keeps_text_and_true_silent_gap(left, right, words):
    warnings = []
    candidates = [left, right]
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1
    assert "新增短词声学归属不确定" in warnings[0]
    assert "已保留分句与真实空档，请复核" in warnings[0]


def test_already_separated_short_suffix_keeps_model_time_without_assigning_inserted_word():
    words = _words([(0, 1, "I"), (8, 9, "think")])
    candidates = [_part("I surely ", 0, 1), _part("think", 8, 9)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1


@pytest.mark.parametrize("text", ["I I think", "I surely think"])
def test_same_short_addition_in_single_candidate_crossing_real_gap_still_fails(text):
    words = _words([(0, 1, "I"), (8, 9, "think")])
    warnings = []
    with pytest.raises(PauseAlignmentError, match="新增文字"):
        repair_long_pauses([_part(text, 0, 9)], words, .6, align_all=True, warnings=warnings)
    assert warnings == []


@pytest.mark.parametrize("end,start", [(1.02, 8), (1, 7.98), (.8, 8), (1, 8.2)])
def test_separated_addition_requires_actual_gap_boundaries_not_only_model_separation(end, start):
    words = _words([(0, 1, "I"), (8, 9, "think")])
    with pytest.raises(PauseAlignmentError, match="新增文字"):
        repair_long_pauses([_part("I", 0, end), _part("I think", start, 9)], words,
                           .6, align_all=True, warnings=[])


def test_another_candidate_inside_real_silence_cannot_hide_behind_separated_neighbours():
    words = _words([(0, 1, "I"), (8, 9, "think")])
    candidates = [_part("I", 0, 1), _part("I", 3, 4), _part("think", 8, 9)]
    with pytest.raises(PauseAlignmentError, match="新增文字"):
        repair_long_pauses(candidates, words, .6, align_all=True, warnings=[])


def test_candidate_boundary_inside_added_text_keeps_already_proven_separate_acoustic_regions():
    words = _words([(0, 1, "I"), (8, 9, "think")])
    candidates = [_part("I surely", 0, 1), _part("really think", 8, 9)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1 and "声学对应位置需复核" in warnings[0]


def test_added_words_in_proven_acoustic_region_are_preserved_for_text_quality_review():
    words = _words([(0, 1, "I"), (8, 9, "think")])
    candidates = [_part("I", 0, 1), _part("some other wholly invented story think", 8, 9)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1 and "声学对应位置需复核" in warnings[0]


def test_replacement_in_proven_regions_is_not_rejected_by_text_similarity_alone():
    words = _words([(0, 1, "first"), (8, 9, "second")])
    candidates = [_part("unrelated", 0, 1), _part("invention", 8, 9)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1 and "声学对应位置需复核" in warnings[0]


def test_short_insert_pause_warnings_are_bounded_without_deleting_candidates():
    words = _words([(index * 5, index * 5 + 1, f"word{index}") for index in range(11)])
    candidates = [_part("word0", 0, 1)]
    candidates.extend(_part(f"word{index - 1} word{index}", index * 5, index * 5 + 1)
                      for index in range(1, 11))
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 8
    assert all("新增短词声学归属不确定" in warning for warning in warnings)


@pytest.mark.parametrize("source,candidate", [("would", "won't"), ("Kwa Jude", "Quanjude"),
                                              ("10 yen", "10 yuan")])
def test_already_separated_asr_correction_keeps_model_wording_and_source_acoustic_region(source, candidate):
    words = _words([(0, 1, source), (8, 9, "go")])
    candidates = [_part(candidate, 0, 1), _part("go", 8, 9)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1 and "声学对应位置需复核" in warnings[0]


def test_correction_on_both_sides_needs_no_lexical_similarity_but_records_review_position():
    words = _words([(0, 1, "would"), (8, 9, "go")])
    candidates = [_part("won't", 0, 1), _part("leave", 8, 9)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1 and "第 1、2 句" in warnings[0]


@pytest.mark.parametrize("left,right", [("go", "would"), ("won't go", "go")])
def test_exact_words_moved_to_wrong_acoustic_region_cannot_use_correction_exception(left, right):
    words = _words([(0, 1, "would"), (8, 9, "go")])
    warnings = []
    with pytest.raises(PauseAlignmentError, match="长停顿"):
        repair_long_pauses([_part(left, 0, 1), _part(right, 8, 9)], words,
                           .6, align_all=True, warnings=warnings)
    assert warnings == []


@pytest.mark.parametrize("start,end", [(-1, 1), (.2, 1), (0, .8), (2, 3)])
def test_corrected_text_with_unproven_or_shifted_region_is_not_accepted(start, end):
    words = _words([(0, 1, "would"), (8, 9, "go")])
    with pytest.raises(PauseAlignmentError, match="长停顿"):
        repair_long_pauses([_part("won't", start, end), _part("go", 8, 9)], words,
                           .6, align_all=True, warnings=[])


def test_preexisting_short_insert_exception_cannot_preserve_outside_source_region_time():
    words = _words([(10, 11, "I"), (18, 19, "think")])
    with pytest.raises(PauseAlignmentError, match="长停顿"):
        repair_long_pauses([_part("I", 0, 11), _part("I think", 18, 19)], words,
                           .6, align_all=True, warnings=[])


def test_unknown_correction_still_cannot_cross_real_silence_without_reliable_cut():
    words = _words([(0, 1, "would"), (8, 9, "go")])
    with pytest.raises(PauseAlignmentError, match="长停顿"):
        repair_long_pauses([_part("won't leave", 0, 9)], words,
                           .6, align_all=True, warnings=[])


def test_raw_candidate_neighbouring_yeah_is_reanchored_on_same_pause_side_without_false_rejection():
    words = _words([(358.82, 359.48, "pretty"), (359.48, 359.72, "good"),
                    (359.72, 359.96, "yeah"), (359.96, 360.22, "I"),
                    (360.22, 360.46, "hope"), (360.46, 366, "orange"),
                    (371.66, 373.84, "there's")])
    candidates = [_part("Pretty good.", 358.82, 359.72),
                  _part("Yeah, I hope orange.", 359.96, 366),
                  _part("There's", 371.66, 373.84)]
    out = repair_long_pauses(candidates, words, .6, align_all=True, warnings=[])
    assert [part["text"] for part in out] == [part["text"] for part in candidates]
    assert out[1]["start"] == 359.72
    assert out[1]["end"] == 366 and out[2]["start"] == 371.66


def test_repeated_i_anchor_in_longer_context_cannot_prove_cross_pause_word_movement():
    words = _words([(0, .2, "I"), (.2, .4, "know"), (.4, 1, "I"),
                    (8, 8.2, "I"), (8.2, 8.5, "think"), (8.5, 9, "that")])
    candidates = [_part("I know I I", 0, 1), _part("think that", 8, 9)]
    warnings = []
    assert repair_long_pauses(candidates, words, .6, align_all=True, warnings=warnings) == candidates
    assert len(warnings) == 1 and "声学对应位置需复核" in warnings[0]
