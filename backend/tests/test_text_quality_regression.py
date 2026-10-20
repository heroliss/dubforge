"""固定真实片段的离线观察：不调用模型，不裁决误词，不污染项目。"""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from dubforge import text_quality as quality
from dubforge.utils.lineprotocols import serialize_timed_lines


CORPUS = Path(__file__).parent / "fixtures/text_quality/corpus.json"


@pytest.fixture(autouse=True)
def isolate(isolated_env):
    """领域测试遵守独立 DATA_ROOT，真实项目只存在于固定截取样本。"""


@pytest.fixture()
def corpus():
    return quality.load_corpus(CORPUS)


def _case(corpus, name):
    return next(case for case in corpus["cases"] if case["id"] == name)


def _candidate(corpus, snapshot="recorded_model_rows"):
    # 多个关注窗口可能复用同一行，按原编号去重，再为片段集合重排评估编号。
    rows = {row["id"]: row for case in corpus["cases"] if case["source"] == "train"
            for row in case[snapshot]}
    return serialize_timed_lines([dict(row, id=index) for index, row in
                                  enumerate(sorted(rows.values(), key=lambda row: row["start"]), 1)])


def test_real_samples_have_traceable_sources_and_audio_uncertainty(corpus):
    assert len(corpus["cases"]) == 9
    assert corpus["sources"]["train"]["project_directory"].endswith("vPoYu-uHQPg")
    assert corpus["sources"]["train"]["recorded_response"]["timestamp"].startswith("2026-10-07")
    assert all(case["audio_review"] == "pending" for case in corpus["cases"])
    assert all(len(digest) == 64 for source in corpus["sources"].values() for digest in source["files"].values())
    assert any("10 yen" in row["text"] for row in _case(corpus, "currency")["source_rows"])
    assert any("kwa" in row["text"] for row in _case(corpus, "proper_name")["source_rows"])


def test_model_and_postprocessing_snapshots_expose_manufactured_singleton(corpus):
    case = _case(corpus, "long_gap_the_line")
    assert any(row["text"] == "The line" for row in case["recorded_model_rows"])
    assert any(row["text"] == "The" for row in case["recorded_artifact_rows"])
    assert not any(row["text"] == "The" for row in case["recorded_model_rows"])
    original = deepcopy(case)
    report = quality.evaluate(corpus, "train", _candidate(corpus))
    observed = _case(report, "long_gap_the_line")
    assert observed["observations"][0]["status"] == "observed"
    assert observed["observations"][1]["status"] == "not_observed"
    assert observed["reference_gaps"][0]["occupied_seconds"] == 0
    assert case == original


def test_real_boundary_candidate_survives_current_offline_postprocessing(corpus):
    from dubforge.pipeline.step_split import _prepare_semantic_parts
    from dubforge.utils.srt import SubLine
    case = _case(corpus, "long_gap_the_line")
    source = [SubLine(row["id"], row["start"], row["end"], row["text"]) for row in case["source_rows"]]
    candidate = [dict(row) for row in case["recorded_model_rows"]]
    words = deepcopy(case["words"])
    before = deepcopy(words)
    final, _ = _prepare_semantic_parts(candidate, source, " ".join(line.text for line in source),
                                      words, 20, 7, 2, warnings=[])
    assert [row["text"] for row in final] == [row["text"] for row in case["recorded_model_rows"]]
    assert not any(row["text"] == "The" for row in final)
    assert quality._gap_occupancy(final, case["reference_gaps"])[0]["occupied_seconds"] == 0
    assert words == before


def test_noise_observation_never_becomes_deletion_or_failure(corpus):
    text = "1. 130.220-131.400: we\n2. 135.750-139.050: made it with exactly 10 minutes to spare.\n"
    report = quality.evaluate(corpus, "train", text)
    case = _case(report, "long_gap_we")
    assert case["observations"][0]["status"] == "observed"
    assert "不能据此删词" in case["observations"][0]["interpretation"]
    assert report["structure"]["invalid_line_ids"] == []
    assert report["structure"]["overlaps"] == []
    assert case["rows"][0]["text"] == "we"
    assert report["translation"]["status"] == "not_evaluated"
    assert report["measurements"]["llm_tokens"] is None


def test_short_answer_and_zero_time_context_word_are_not_errors(corpus):
    report = quality.evaluate(corpus, "train",
        "1. 358.820-359.720: pretty good\n2. 359.720-360.220: Yeah\n"
        "3. 703.990-706.470: So now, as I make my own pancake.\n")
    assert _case(report, "meaningful_yeah")["observations"][0]["status"] == "observed"
    assert _case(report, "meaningful_yeah")["short_line_ids"] == [2]
    assert _case(report, "zero_duration_context_word")["observations"][0]["status"] == "observed"
    assert any(word["word"].strip() == "now" and word["start"] == word["end"]
               for word in _case(corpus, "zero_duration_context_word")["words"])
    assert report["structure"]["invalid_line_ids"] == []


def test_other_video_meaningful_single_word_survives_observation(corpus):
    report = quality.evaluate(corpus, "short_answer", "1. 750.320-751.540: Okay!\n", "1. 好。\n")
    assert report["structure"]["invalid_line_ids"] == []
    assert report["translation"]["status"] == "ok"
    case = _case(report, "meaningful_okay")
    assert case["one_word_line_ids"] == [1]
    assert case["observations"][0]["status"] == "observed"


def test_expected_phrases_are_observations_not_synonym_or_fact_judgements(corpus):
    report = quality.evaluate(corpus, "train", "1. 32.870-35.070: Those tickets cost ten Chinese yuan.\n")
    note = _case(report, "currency")["observations"][0]
    assert note["status"] == "not_observed"
    assert "合理改写" in note["interpretation"]
    assert report["structure"]["invalid_line_ids"] == []


def test_reference_gap_occupancy_counts_union_not_duplicate_overlap(corpus):
    local = deepcopy(corpus)
    case = _case(local, "long_gap_we")
    case["reference_gaps"] = [[131.4, 135.75]]
    report = quality.evaluate(local, "train", "1. 131.400-133.400: first\n2. 131.900-132.400: nested\n"
                              "3. 132.900-134.400: overlapping\n")
    assert _case(report, "long_gap_we")["reference_gaps"][0]["occupied_seconds"] == 3
    assert len(report["structure"]["overlaps"]) == 2


@pytest.mark.parametrize(("translation", "field", "expected"), [
    ("1. 你好\n1. 再见\n", "duplicate_ids", [1]),
    ("1. 你好\n", "missing_ids", [2]),
    ("1. 你好\n2. 再见\n3. 多出来\n", "extra_ids", [3]),
    ("2. 再见\n1. 你好\n", "order_matches", False),
    ("1. 你好\n这一行没有编号\n2. 再见\n", "malformed_file_lines", [2]),
])
def test_translation_alignment_detects_structure_without_semantic_claim(corpus, translation, field, expected):
    report = quality.evaluate(corpus, "train", "1. 0-1: hello\n2. 2-3: goodbye\n", translation)
    assert report["translation"]["status"] == "problem"
    assert report["translation"][field] == expected
    assert "不能证明" in report["translation"]["content_correspondence"]


def test_empty_translation_is_recorded_but_not_rejected(corpus):
    report = quality.evaluate(corpus, "train", "1. 0-1: Hmm\n2. 2-3: goodbye\n", "1. \n2. 再见\n")
    assert report["translation"]["status"] == "ok"
    assert report["translation"]["empty_ids"] == [1]


def test_baseline_comparison_explains_changes_without_quality_score(corpus):
    before = quality.evaluate(corpus, "train", _candidate(corpus, "recorded_artifact_rows"))
    after = quality.evaluate(corpus, "train", _candidate(corpus))
    result = quality.compare(after, before)
    boundary = next(case for case in result["cases"] if case["id"] == "long_gap_the_line")
    assert boundary["observations"][1] == dict(label="是否重新制造独立 The", before="observed", after="not_observed")
    assert "不自动代表改善" in result["interpretation"]
    assert "score" not in result
    assert "整体质量分" in quality.render_markdown(dict(current=after, comparison=result))


def test_cli_writes_atomic_report_and_keeps_inputs_unchanged(corpus, tmp_path):
    source, translated, report = tmp_path / "split.txt", tmp_path / "translate.txt", tmp_path / "report.json"
    source.write_text(_candidate(corpus), encoding="utf-8")
    translated.write_text("1. 测试译文\n", encoding="utf-8")
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, translated)]
    assert quality.main(["--corpus", str(CORPUS), "--source", "train", "--split", str(source),
                         "--translate", str(translated), "--report", str(report), "--format", "json"]) == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["current"]["translation"]["status"] == "problem"
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, translated)] == before
    assert len(result["inputs"]) == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_report_cannot_overwrite_any_input(corpus, tmp_path, capsys):
    source = tmp_path / "source.txt"
    source.write_text("1. 0-1: hello\n", encoding="utf-8")
    before = source.read_bytes()
    assert quality.main(["--corpus", str(CORPUS), "--source", "train", "--split", str(source),
                         "--report", str(source)]) == 2
    assert source.read_bytes() == before
    assert "不能覆盖" in capsys.readouterr().err


def test_list_and_invalid_input_are_clear_chinese_errors(corpus, tmp_path, capsys):
    assert quality.main(["--corpus", str(CORPUS), "--list"]) == 0
    assert "short_answer" in json.loads(capsys.readouterr().out)
    broken = tmp_path / "bad.json"
    broken.write_text('{"version": 2}', encoding="utf-8")
    assert quality.main(["--corpus", str(broken), "--list"]) == 2
    assert "样本版本" in capsys.readouterr().err
    with pytest.raises(quality.TextQualityError, match="有限秒数"):
        quality.evaluate(corpus, "train", f"1. 0-{'9' * 400}: too long\n")
