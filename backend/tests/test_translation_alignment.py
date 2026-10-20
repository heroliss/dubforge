import pytest

from dubforge.pipeline.translation_alignment import audit, blocking_issues, estimate_speech_seconds, summary


def test_estimate_counts_cjk_and_latin_without_punctuation():
    assert estimate_speech_seconds("你好，world！") == pytest.approx(2 / 4.7 + 1 / 3)
    assert estimate_speech_seconds("") == 0.3


@pytest.mark.parametrize('plain,decorated', [
    ('被辽朝付钱雇佣', '被辽朝付钱雇佣。'),
    ('こんにちは', '「こんにちは」、。！？'),
    ('안녕하세요', '안녕하세요，。！？'),
    ("don't we're AI U.S.", "don't, we're: AI / U.S.!"),
    ('don’t we’re', '“don’t” — we’re!'),
])
def test_task_and_review_share_estimate_without_punctuation_inflation(plain, decorated):
    from dubforge.pipeline.step_dubtask import estimate_seconds
    expected = estimate_speech_seconds(plain)
    assert estimate_speech_seconds(decorated) == pytest.approx(expected)
    assert estimate_seconds(decorated) == pytest.approx(expected)


def test_latin_contractions_and_dotted_initialisms_count_as_single_words():
    assert estimate_speech_seconds("don't we’re U.S. AI") == pytest.approx(4 / 3)


def test_audit_flags_overrun_and_underuse():
    result = audit([
        {"id": 1, "start": 0, "end": 1, "text": "这是一句很长很长的中文台词"},
        {"id": 2, "start": 1, "end": 5, "text": "嗯"},
    ])
    kinds = {(item["kind"], item.get("id")) for item in result["issues"]}
    assert ("target_duration_overrun", 1) in kinds
    assert ("target_duration_underuse", 2) in kinds


def test_audit_marks_adjacent_long_short_as_possible_shift_but_not_across_pause():
    contiguous = audit([
        {"id": 1, "start": 0, "end": 1, "text": "这是一句很长很长的中文台词"},
        {"id": 2, "start": 1, "end": 5, "text": "嗯"},
    ])
    assert any(item["kind"] == "sequence_shift_suspect"
               for item in contiguous["issues"])

    separated = audit([
        {"id": 1, "start": 0, "end": 1, "text": "嗯"},
        {"id": 2, "start": 3, "end": 4, "text": "嗯"},
        {"id": 3, "start": 8, "end": 9, "text": "好"},
    ])
    assert not any(item["kind"] == "sequence_shift_suspect"
                   for item in separated["issues"])
    assert any(item["kind"] == "unused_source_gap"
               for item in separated["issues"])


def test_summary_is_actionable():
    result = audit([
        {"id": 1, "start": 0, "end": 1, "text": "这是一句很长很长的中文台词"},
        {"id": 2, "start": 1, "end": 5, "text": "嗯"},
    ])
    assert "逐句复核" in summary(result)


def test_summary_does_not_call_a_real_unused_gap_a_clean_pass():
    result = audit([
        {"id": 1, "start": 0, "end": 1, "text": "嗯"},
        {"id": 2, "start": 3, "end": 4, "text": "嗯"},
    ])
    assert "后方存在可复核空档" in summary(result)


def test_blocking_issues_only_keeps_severe_fit_and_shift_cases():
    result = audit([
        {"id": 1, "start": 0, "end": 1, "text": "这是一句非常非常长的中文台词"},
        {"id": 2, "start": 1, "end": 5, "text": "嗯"},
        {"id": 3, "start": 5, "end": 6, "text": "较长但不够硬门槛"},
    ])
    blocked = blocking_issues(result)
    assert any(item["kind"] == "sequence_shift_suspect" for item in blocked)
    # 普通的估算偏长只留在报告中，不因为预估误差阻止整批任务。
    assert all(item.get("id") != 3 for item in blocked)
