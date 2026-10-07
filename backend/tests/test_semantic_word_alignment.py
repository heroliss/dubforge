"""全文可靠词序重锚：仅修改可证明的时间，不修改候选文字或猜测纠错边界。"""
import pytest

from dubforge.pipeline.semantic_pauses import PauseAlignmentError, repair_long_pauses
from dubforge.runner import StepCancelled


@pytest.fixture(autouse=True)
def isolated_alignment_environment(isolated_env):
    """校时回归在隔离数据根运行，避免使用任何真实项目或服务。"""


def _word(text, start, end):
    return dict(word=text, start=start, end=end)


def _part(text, start, end):
    return dict(text=text, start=start, end=end)


def test_opt_in_corrects_prefix_assigned_to_previous_candidate_without_long_pause():
    words = [_word("travel", 610.5, 611.2), _word("so", 611.52, 611.7),
             _word("many", 611.7, 612), _word("ways", 612, 613.4),
             _word("work", 613.4, 614)]
    parts = [_part("Travel. ", 610.5, 613.24), _part("So many ways work!", 613.24, 614)]
    assert repair_long_pauses(parts, words, .6) == parts
    aligned = repair_long_pauses(parts, words, .6, align_all=True)
    assert [(row["start"], row["end"]) for row in aligned] == [(610.5, 611.2), (611.52, 614)]
    assert [row["text"] for row in aligned] == [row["text"] for row in parts]
    assert parts[1]["start"] == 613.24


def test_corrected_document_over_character_diff_limit_reanchors_all_reliable_endpoints():
    words, parts = [], []
    tokens = ["This", "complete", "sentence", "describes", "travel"]
    for index in range(140):
        start = index * 5
        words.extend(_word(token, start + offset, start + offset + 1)
                     for offset, token in enumerate(tokens))
        text = "This complete sentence describes travel. "
        if index == 1:
            text = "This completed sentence describes travel. "
        parts.append(_part(text, start + 1, start + 5))
    assert sum(len(part["text"]) for part in parts) > 4000
    aligned = repair_long_pauses(parts, words, .6, align_all=True)
    assert [(row["start"], row["end"]) for row in aligned] == [
        (index * 5, index * 5 + 5) for index in range(140)]
    assert [row["text"] for row in aligned] == [row["text"] for row in parts]


def test_internal_correction_does_not_replace_raw_candidate_characters():
    words = [_word("This", 0, .2), _word("complete", .2, .7), _word("sentence", .7, 1)]
    text = " This  completed\t sentence!\n"
    aligned = repair_long_pauses([_part(text, .2, .9)], words, .6, align_all=True)
    assert aligned == [_part(text, 0, 1)]


@pytest.mark.parametrize("text", ["Finely done", "Fine donely"])
def test_replaced_endpoint_keeps_model_time_even_with_other_exact_endpoint(text):
    words = [_word("Fine", 0, 1), _word("done", 1, 2)]
    candidate = _part(text, .3, 1.8)
    assert repair_long_pauses([candidate], words, .6, align_all=True) == [candidate]


def test_inserted_endpoint_does_not_borrow_the_neighbouring_word_time():
    words = [_word("We", 0, 1), _word("agree", 1, 2)]
    parts = [_part("We truly ", .2, 1.5), _part("agree.", 1.5, 2)]
    aligned = repair_long_pauses(parts, words, .6, align_all=True)
    assert aligned[0] == parts[0]
    assert aligned[1] == _part("agree.", 1, 2)


def test_cut_inside_same_source_word_cannot_invent_a_subword_acoustic_time():
    words = [_word("cannot", 0, 1), _word("go", 1, 2)]
    parts = [_part("can ", .2, .5), _part("not go", .5, 1.8)]
    assert repair_long_pauses(parts, words, .6, align_all=True) == parts


@pytest.mark.parametrize("source,text", [(["cannot", "go"], "Can not go!"),
                                        (["ice", "cream"], "Icecream.")])
def test_proven_joined_or_split_words_reanchor_only_actual_source_word_boundaries(source, text):
    words = [_word(token, index, index + 1) for index, token in enumerate(source)]
    assert repair_long_pauses([_part(text, .2, 1.8)], words, .6, align_all=True) == [
        _part(text, 0, 2)]


def test_zero_duration_word_preserves_text_but_does_not_create_its_own_interval():
    words = [_word("so", 0, 1), _word("now", 1, 1), _word("as", 1, 2)]
    parts = [_part("so", .2, .8), _part("now", .8, 1.2), _part("as", 1.2, 1.8)]
    aligned = repair_long_pauses(parts, words, .6, align_all=True)
    assert aligned == [_part("so", 0, 1), parts[1], _part("as", 1, 2)]
    assert [row["text"] for row in aligned] == [row["text"] for row in parts]


def test_zero_duration_word_with_positive_neighbours_has_valid_enclosing_boundaries():
    words = [_word("so", 0, 1), _word("now", 1, 1), _word("as", 1, 2)]
    assert repair_long_pauses([_part("so now as", .3, 1.7)], words, .6, align_all=True) == [
        _part("so now as", 0, 2)]


def test_existing_source_word_overlap_is_left_for_downstream_time_checks():
    words = [_word("Fine", 0, 1), _word("done", .9, 2)]
    aligned = repair_long_pauses([_part("Fine", .2, .7), _part("done", 1.3, 1.8)],
                                 words, .6, align_all=True)
    assert aligned == [_part("Fine", 0, 1), _part("done", .9, 2)]


def test_optional_alignment_never_introduces_a_reversed_start_after_unknown_boundary():
    words = [_word("Finely", 0, 1), _word("done", 1, 2)]
    parts = [_part("Fine", 1.1, 1.3), _part("done", 1.4, 1.8)]
    assert repair_long_pauses(parts, words, .6, align_all=True) == parts


def test_optional_alignment_never_introduces_a_reversed_start_before_unknown_boundary():
    words = [_word("We", 10, 11), _word("agree", 11, 12)]
    parts = [_part("We", 0, .5), _part("agreement", .5, 1)]
    assert repair_long_pauses(parts, words, .6, align_all=True) == parts


def test_cancellation_during_full_document_mapping_propagates_without_mutation():
    words = [_word("word", index, index + 1) for index in range(200)]
    parts = [_part("word " * 200, 1, 200)]
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise StepCancelled("用户取消")

    with pytest.raises(StepCancelled):
        repair_long_pauses(parts, words, .6, align_all=True, cancel_check=cancel)
    assert parts == [_part("word " * 200, 1, 200)]


@pytest.mark.parametrize("text", ["unrelated invented passage", "I surely think"])
def test_unreliable_actual_long_pause_remains_a_typed_error_when_align_all_enabled(text):
    words = [_word("I", 0, 1), _word("think", 8, 9)]
    with pytest.raises(PauseAlignmentError, match="长停顿"):
        repair_long_pauses([_part(text, 0, 9)], words, .6, align_all=True)


def test_isolated_zero_word_across_real_long_pauses_still_cannot_get_invented_time():
    words = [_word("first", 0, 1), _word("now", 8, 8), _word("last", 15, 16)]
    with pytest.raises(PauseAlignmentError, match="没有有效语音时间"):
        repair_long_pauses([_part("first now last", 0, 16)], words, .6, align_all=True)
