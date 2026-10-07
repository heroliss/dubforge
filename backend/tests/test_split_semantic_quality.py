"""语义整理质量门禁回归；模型响应全为本地桩，数据根目录由夹具隔离。"""
import json
import threading

import pytest

from dubforge.errors import StepFailedError
from dubforge.pipeline import step_split
from dubforge.runner import StepCancelled, StepContext
from dubforge.utils.srt import SubLine
from test_text_chain import _seed_transcribe, read_split


def _ctx(make_project, pdir, *, lines, words=None, max_len=50, max_duration=15):
    from dubforge import config
    from dubforge.domain import crud

    config.set_key("split.max_split_length", max_len)
    config.set_key("split.max_segment_seconds", max_duration)
    config.set_key("split.use_llm", True)
    config.set_key("asr.segmentation_mode", "word")
    uid = make_project()
    _seed_transcribe(uid, lines)
    if words is not None:
        path = pdir(uid) / "srt" / "words.json"
        path.write_text(json.dumps(words), encoding="utf-8")
        crud.add_artifact(crud.get_project(uid).id, "transcribe", "json",
                          "words.json", path, "srt/words.json", replace_existing=True)
    directory = pdir(uid)
    (directory / "logs").mkdir(exist_ok=True)
    context = StepContext(crud.get_project(uid), "split", directory,
                          directory / "logs" / "split.log", threading.Event())
    return uid, context


def _responses(monkeypatch, *responses):
    """保留真实 chat_validated 解析/重试语义，只替换网络调用。"""
    from dubforge.backends import llm

    calls = []

    def fake_chat(prompt=None, **kwargs):
        calls.append((prompt, kwargs))
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def test_semantic_limits_preserve_complete_unit_with_modest_length_overflow():
    text = "one really cool thing about taking the train in china"
    item = {"text": text, "start": 0.0, "end": 3.0}
    assert step_split._wc(text) == 44
    assert step_split._enforce_final_limits([item], 22, 8, semantic=True) == [item]
    assert len(step_split._enforce_final_limits([item], 22, 8)) > 1


def test_semantic_limits_preserve_modest_duration_overflow():
    item = {"text": "这是语义完整的一句话。", "start": 0.0, "end": 9.0}
    assert step_split._enforce_final_limits([item], 22, 8, semantic=True) == [item]
    assert len(step_split._enforce_final_limits([item], 22, 8)) > 1


def test_semantic_quality_warns_without_rejecting_connected_short_fragment():
    from dubforge.pipeline.semantic_quality import semantic_boundary_warnings

    parts = [{"text": "about taking the train in", "start": 0.0, "end": 1.0},
             {"text": "china", "start": 1.0, "end": 1.1}]
    assert step_split._semantic_quality_issues(parts, .6) == []
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 2
    assert "句尾'in'可能接续第2句" in warnings[0]
    assert "不足0.5秒" in warnings[1]
    assert all("不能确认错误" in warning for warning in warnings)


@pytest.mark.parametrize("text", ["yes", "no", "okay", "嗯", "好", "China!"])
def test_semantic_quality_preserves_short_answers_and_strong_sentence_end(text):
    parts = [{"text": "we can continue", "start": 0.0, "end": 1.0},
             {"text": text, "start": 1.0, "end": 1.2}]
    assert step_split._semantic_quality_issues(parts, .6) == []


def test_semantic_quality_does_not_merge_an_isolated_word_across_real_pause():
    parts = [{"text": "one", "start": 0.0, "end": .3},
             {"text": "really cool thing", "start": 4.0, "end": 5.0}]
    assert step_split._semantic_quality_issues(parts, .6) == []


def test_semantic_quality_only_warns_about_extreme_text_density():
    from dubforge.pipeline.semantic_quality import semantic_boundary_warnings

    parts = [{"text": "this entire sentence cannot be spoken in a tenth of a second",
              "start": 0.0, "end": .1}]
    assert step_split._semantic_quality_issues(parts, .6) == []
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 1 and "文字密度较高" in warnings[0]
    assert "不能确认错误" in warnings[0]


def test_connected_china_fragment_does_not_force_retry_or_local_rewrite(make_project, pdir, monkeypatch):
    words = [{"word": "about", "start": 0.0, "end": .4},
             {"word": "taking", "start": .4, "end": .8},
             {"word": "the", "start": .8, "end": 1.0},
             {"word": "train", "start": 1.0, "end": 1.6},
             {"word": "in", "start": 1.6, "end": 1.9},
             {"word": "china", "start": 1.9, "end": 2.0}]
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 2, "about taking the train in china")], words=words)
    calls = _responses(monkeypatch,
                       "1. 0.00-1.90: about taking the train in\n2. 1.90-2.00: china",
                       "1. 0.00-2.00: about taking the train in china")
    step_split.run(ctx)
    assert len(calls) == 1
    assert read_split(uid)["lines"] == [
        {"id": 1, "start": 0.0, "end": 1.9, "text": "about taking the train in"},
        {"id": 2, "start": 1.9, "end": 2.0, "text": "china"}]
    log = (pdir(uid) / "logs" / "split.log").read_text(encoding="utf-8")
    assert "不足0.5秒" in log and "不能确认错误" in log


def test_short_time_warning_uses_real_word_alignment_without_retry(make_project, pdir, monkeypatch):
    words = [{"word": "we", "start": 0.0, "end": .5},
             {"word": "arrived", "start": .5, "end": 2.7},
             {"word": "in", "start": 2.7, "end": 2.8},
             {"word": "china", "start": 2.8, "end": 2.9}]
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 2.9, "we arrived in china")], words=words)
    # 模型给第二段 1.5 秒，只有声学对齐后才发现它实际仅 0.2 秒。
    calls = _responses(monkeypatch,
                       "1. 0.00-1.40: we arrived\n2. 1.40-2.90: in china",
                       "1. 0.00-2.90: we arrived in china")
    step_split.run(ctx)
    assert len(calls) == 1
    result = read_split(uid)["lines"]
    assert [(row["start"], row["end"]) for row in result] == [(0, 2.7), (2.7, 2.9)]
    assert "0.200秒" in (pdir(uid) / "logs" / "split.log").read_text(encoding="utf-8")


def test_short_tail_remains_a_warning_when_join_would_exceed_soft_limits(make_project, pdir, monkeypatch):
    text = "we arrived in beautiful china"
    words = [{"word": "we", "start": 0.0, "end": .5},
             {"word": "arrived", "start": .5, "end": 1.5},
             {"word": "in", "start": 1.5, "end": 1.6},
             {"word": "beautiful", "start": 1.6, "end": 2.9},
             {"word": "china", "start": 2.9, "end": 3.0}]
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 3, text)], words=words, max_len=10)
    calls = _responses(monkeypatch,
                       "1. 0.00-2.90: we arrived in beautiful\n2. 2.90-3.00: china",
                       "1. 0.00-1.50: we arrived\n2. 1.50-3.00: in beautiful china")
    step_split.run(ctx)
    # 短尾只是启发式证据：不能因此再请求整稿，或机械合句改变候选。
    assert len(calls) == 1
    result = read_split(uid)["lines"]
    assert " ".join(row["text"] for row in result) == text
    assert result[0]["start"] == 0 and result[-1]["end"] == 3
    assert step_split._semantic_quality_issues(result, .6) == []
    assert "不足0.5秒" in (pdir(uid) / "logs" / "split.log").read_text(encoding="utf-8")


def test_valid_complete_unit_needs_no_additional_model_call(make_project, pdir, monkeypatch):
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 3, "we arrived in beautiful china")], max_len=22)
    calls = _responses(monkeypatch, "1. 0.00-3.00: we arrived in beautiful china")
    step_split.run(ctx)
    assert len(calls) == 1
    assert read_split(uid)["count"] == 1


def test_complete_model_unit_over_forty_characters_is_not_mechanically_split(make_project, pdir, monkeypatch):
    text = "they like to add different sauces and nuts to snack on during their train journey"
    assert len(text) > 40
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 5, text)], max_len=10, max_duration=7)
    calls = _responses(monkeypatch, "1. 0.00-5.00: " + text)
    step_split.run(ctx)
    assert len(calls) == 1
    assert read_split(uid)["lines"] == [dict(id=1, start=0.0, end=5.0, text=text)]


def test_extreme_density_warning_does_not_retry_or_discard_candidate(make_project, pdir, monkeypatch):
    text = "this entire sentence cannot be spoken in a tenth of a second"
    uid, ctx = _ctx(make_project, pdir, lines=[(0, .1, text)])
    calls = _responses(monkeypatch, "1. 0.00-0.10: " + text)
    step_split.run(ctx)
    assert len(calls) == 1
    assert read_split(uid)["lines"] == [dict(id=1, start=0.0, end=.1, text=text)]
    log = (pdir(uid) / "logs" / "split.log").read_text(encoding="utf-8")
    assert "文字密度较高" in log and "不能确认错误" in log


def test_rejected_structure_preserves_old_file_and_artifact(make_project, pdir, monkeypatch):
    from dubforge.domain import crud

    text = "this phrase cannot be spoken this quickly"
    uid, ctx = _ctx(make_project, pdir, lines=[(0, .1, text)])
    old_path = pdir(uid) / "artifacts" / "split" / "split.txt"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_bytes = b"1. 0.000-2.000: old approved source\n"
    old_path.write_bytes(old_bytes)
    old_artifact = crud.add_artifact(ctx.project.id, "split", "text", "split.txt",
                                     old_path, "artifacts/split/split.txt")
    calls = _responses(monkeypatch, f"1. 0.00-0.05: {text}\n1. 0.05-0.10: {text}")
    with pytest.raises(StepFailedError):
        step_split.run(ctx)
    assert len(calls) == 2
    assert old_path.read_bytes() == old_bytes
    artifacts = crud.latest_artifacts(ctx.project.id, ["split"])
    assert len(artifacts) == 1
    assert artifacts["split/split.txt"].id == old_artifact.id
    assert artifacts["split/split.txt"].sha256 == old_artifact.sha256


def test_quality_retry_keeps_full_original_context_without_bad_candidate(make_project, pdir, monkeypatch):
    lines = [SubLine(1, 0, 1, "previous context"),
             SubLine(2, 1, 3, "about taking the train in china"),
             SubLine(3, 3, 4, "following context")]
    uid, ctx = _ctx(make_project, pdir, lines=[(x.start, x.end, x.text) for x in lines])
    bad = "1. 0.00-1.00: previous context\n3. 1.00-3.00: about taking the train in china\n4. 3.00-4.00: following context"
    good = "1. 0.00-1.00: previous context\n2. 1.00-3.00: about taking the train in china\n3. 3.00-4.00: following context"
    calls = _responses(monkeypatch, bad, good)
    result = step_split._llm_resegment(ctx, lines, 50, 15, 2, [])
    assert result and len(calls) == 2
    initial, retry = calls[0][0], calls[1][0]
    assert initial in retry
    assert "previous context" in retry and "following context" in retry
    assert bad not in retry
    assert calls[1][1].get("messages") is None


@pytest.mark.parametrize("error", [StepCancelled("步骤被用户取消"),
                                     StepFailedError("连接中断", code="LLM_TRANSPORT")])
def test_cancel_and_transport_error_do_not_trigger_quality_retry(make_project, pdir, monkeypatch, error):
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 2, "we arrived in china")])
    calls = _responses(monkeypatch, error)
    with pytest.raises(type(error)) as exc:
        step_split.run(ctx)
    assert exc.value.code == error.code
    assert len(calls) == 1
    assert not (pdir(uid) / "artifacts" / "split" / "split.txt").exists()


def _real_pause_words():
    # 第一个真实项目的 #41 模型片段：ASR 的 one 与 really 相隔 13.89 秒。
    spans = [(145.61, 146.97, "one"), (160.86, 161.40, "really"),
             (161.40, 161.62, "cool"), (161.62, 161.82, "thing"),
             (161.82, 162.04, "about"), (162.04, 162.26, "taking"),
             (162.26, 162.38, "the"), (162.38, 162.60, "train"),
             (162.60, 162.82, "in"), (162.82, 162.92, "china")]
    return [{"start": start, "end": end, "word": word} for start, end, word in spans]


def test_real_41st_model_segment_detects_internal_long_acoustic_pause():
    parts = [{"text": "one really cool thing about taking the train in china",
              "start": 145.61, "end": 162.92}]
    assert step_split._semantic_pause_issues(parts, _real_pause_words(), .6)


def test_program_separates_real_long_pause_without_another_model_request(make_project, pdir, monkeypatch):
    text = "one really cool thing about taking the train in china"
    uid, ctx = _ctx(make_project, pdir,
                    lines=[(145.61, 146.97, "one"),
                           (160.86, 162.92, "really cool thing about taking the train in china")],
                    words=_real_pause_words())
    calls = _responses(monkeypatch,
                       "1. 145.61-162.92: " + text,
                       "1. 145.61-146.97: one\n2. 160.86-162.92: really cool thing about taking the train in china")
    step_split.run(ctx)
    assert len(calls) == 1
    actual = read_split(uid)["lines"]
    assert [(row["start"], row["end"]) for row in actual] == [
        (145.61, 146.97), (160.86, 162.92)]
    assert " ".join(row["text"] for row in actual) == text
    assert all(row["text"] != "china" for row in actual)


def test_split_and_translation_keep_same_ids_count_and_acoustic_times(make_project, pdir, monkeypatch):
    from dubforge.domain import crud
    from dubforge.pipeline import step_translate, text_source

    uid, split_ctx = _ctx(make_project, pdir,
                          lines=[(0.0, 2.0, "Welcome to china."),
                                 (3.0, 5.0, "Let us ride a train.")])
    calls = _responses(monkeypatch,
                       "1. 0.00-2.00: Welcome to china.\n2. 3.00-5.00: Let us ride a train.",
                       "1. 欢迎来到中国。\n2. 我们坐火车吧。")
    step_split.run(split_ctx)
    translate_ctx = StepContext(crud.get_project(uid), "translate", pdir(uid),
                                pdir(uid) / "logs" / "translate.log", threading.Event())
    step_translate.run(translate_ctx)
    source = text_source.source_lines(translate_ctx)
    target = text_source.translated_lines(translate_ctx, source)
    assert len(calls) == 2
    assert len(source) == len(target) == 2
    assert [(row["id"], row["start"], row["end"]) for row in target] == [
        (row["id"], row["start"], row["end"]) for row in source]
    assert [row["text"] for row in target] == ["欢迎来到中国。", "我们坐火车吧。"]


@pytest.mark.parametrize("complete", [
    "so we just scanned our passports and that's what let us in",
    "there's drinks you can mix the hard alcohol with",
    "they also have a dessert bar which i will definitely have to dig into",
])
def test_complete_phrasal_or_relative_clause_before_next_sentence_is_allowed(complete):
    parts = [{"text": complete, "start": 0.0, "end": 4.0},
             {"text": "we got no tickets when we were trying to get on the train",
              "start": 4.0, "end": 7.0}]
    assert step_split._semantic_quality_issues(parts, .6) == []


@pytest.mark.parametrize("ending", ["the", "our", "and", "because", "I"])
def test_dependent_word_shape_without_short_tail_is_warning(ending):
    from dubforge.pipeline.semantic_quality import semantic_boundary_warnings

    parts = [{"text": "this is " + ending, "start": 0.0, "end": 1.0},
             {"text": "next complete clause with several words", "start": 1.0, "end": 4.0}]
    assert step_split._semantic_quality_issues(parts, .6) == []
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 1
    assert "仅凭词形、短时长或文字密度不能确认错误" in warnings[0]


@pytest.mark.parametrize('text', ['Who am I?', 'I!', 'I think'])
def test_complete_pronoun_expression_does_not_create_a_boundary_warning(text):
    from dubforge.pipeline.semantic_quality import semantic_boundary_warnings

    parts = [dict(text=text, start=0, end=1),
             dict(text='The next complete statement has several words', start=1, end=4)]
    assert semantic_boundary_warnings(parts, .6) == []


def test_prepare_exports_boundary_warning_without_changing_text_or_times():
    parts = [dict(text="this is the", start=0, end=1),
             dict(text="next complete clause with several words", start=1, end=4)]
    lines = [SubLine(1, 0, 4, "this is the next complete clause with several words")]
    warnings = []
    final, aligned = step_split._prepare_semantic_parts(parts, lines, lines[0].text,
                                                       [], 50, 15, .6, warnings=warnings)
    assert final == parts and aligned is False
    assert len(warnings) == 1 and "不据此修改文字或时间" in warnings[0]


def test_prepare_preserves_short_neighbor_without_word_shape_rewrite():
    parts = [dict(text="so", start=0, end=.2),
             dict(text="we arrived.", start=.2, end=2)]
    lines = [SubLine(1, 0, 2, "so we arrived.")]
    warnings = []
    final, _ = step_split._prepare_semantic_parts(parts, lines, lines[0].text,
                                                 [], 50, 15, .6, warnings=warnings)
    assert final == parts
    assert len(warnings) == 1 and "不足0.5秒" in warnings[0]


def test_prepare_keeps_short_fragment_as_review_warning():
    text = "Antidisestablishmentarianism in China"
    parts = [dict(text="Antidisestablishmentarianism in", start=0, end=1.9),
             dict(text="China", start=1.9, end=2)]
    lines = [SubLine(1, 0, 2, text)]
    warnings = []
    final, _ = step_split._prepare_semantic_parts(parts, lines, text, [], 50, 15, .6,
                                                 warnings=warnings)
    assert final == parts
    assert any("不足0.5秒" in warning for warning in warnings)
    assert step_split._semantic_quality_issues(final, .6) == []


def test_long_document_without_full_fuzzy_alignment_repairs_pause_once(make_project, pdir, monkeypatch):
    from dubforge import config

    prefix = "This is a complete sentence describing travel details. " * 100
    changed = prefix.replace("complete", "completed", 1).strip()
    unit = "one really cool thing about taking the train in china"
    config.set_key("split.window_max_chars", 0)
    # 全文微改仍走有界映射；不得扩大字符 diff 或因可修复静音多请求一次。
    prefix_tokens = prefix.split()
    prefix_words = [{"word": word, "start": i * 100 / len(prefix_tokens),
                     "end": (i + 1) * 100 / len(prefix_tokens)}
                    for i, word in enumerate(prefix_tokens)]
    words = prefix_words + _real_pause_words()
    uid, ctx = _ctx(make_project, pdir,
                    lines=[(0.0, 100.0, prefix.strip()),
                           (145.61, 146.97, "one"),
                           (160.86, 162.92, "really cool thing about taking the train in china")],
                    words=words, max_len=10000, max_duration=300)
    assert len(step_split._alignment_text(prefix)) > 4000
    assert step_split._alignment_text(prefix) != step_split._alignment_text(changed)
    seen = []
    align = step_split._aligned_word_times

    def record_alignment(parts, line, selected_words):
        result = align(parts, line, selected_words)
        seen.append((line.start, line.end, result is not None))
        return result

    monkeypatch.setattr(step_split, "_aligned_word_times", record_alignment)
    calls = _responses(monkeypatch,
                       f"1. 0.00-100.00: {changed}\n2. 145.61-162.92: {unit}",
                       f"1. 0.00-100.00: {changed}\n2. 145.61-146.97: one\n"
                       "3. 160.86-162.92: really cool thing about taking the train in china")
    step_split.run(ctx)
    assert len(calls) == 1
    assert (0.0, 162.92, False) in seen
    assert [(row["start"], row["end"]) for row in read_split(uid)["lines"]][-2:] == [
        (145.61, 146.97), (160.86, 162.92)]


def test_initial_prompt_over_context_budget_never_sends_to_model(make_project, pdir, monkeypatch):
    from dubforge import config

    config.set_key("split.llm_context_tokens", 8000)
    uid, ctx = _ctx(make_project, pdir, lines=[(0.0, 2.0, "we arrived in china")])
    monkeypatch.setattr(step_split, "_coarse_resegment_prompt",
                        lambda *args, **kwargs: "原始时间轴内容" * 8000)
    calls = _responses(monkeypatch, "1. 0.00-2.00: we arrived in china")
    with pytest.raises(StepFailedError) as exc:
        step_split.run(ctx)
    assert exc.value.code == "SPLIT_CONTEXT_BUDGET"
    assert calls == []
    assert not (pdir(uid) / "artifacts" / "split" / "split.txt").exists()


def test_large_quality_diagnostic_over_budget_stops_before_second_request(make_project, pdir, monkeypatch):
    from dubforge import config

    config.set_key("split.llm_context_tokens", 8000)
    uid, ctx = _ctx(make_project, pdir, lines=[(0.0, 2.0, "about taking the train in china")])

    def oversized_repair(*args, **kwargs):
        return args[3] + "\n【质量诊断】" + "片段需要重新整理" * 8000

    monkeypatch.setattr(step_split, "_split_repair_prompt", oversized_repair)
    calls = _responses(monkeypatch,
                       "1. 0.00-1.00: about taking the train\n1. 1.00-2.00: in china")
    with pytest.raises(StepFailedError) as exc:
        step_split.run(ctx)
    assert exc.value.code == "SPLIT_CONTEXT_BUDGET"
    assert len(calls) == 1
    assert not (pdir(uid) / "artifacts" / "split" / "split.txt").exists()


def test_preposition_and_punctuated_short_tail_only_prompt_review():
    from dubforge.pipeline.semantic_quality import semantic_boundary_warnings

    parts = [{"text": "about taking the train in.", "start": 0.0, "end": 1.0},
             {"text": "China.", "start": 1.0, "end": 1.1}]
    assert step_split._semantic_quality_issues(parts, .6) == []
    warnings = semantic_boundary_warnings(parts, .6)
    assert len(warnings) == 1 and "句尾'in'可能接续第2句" in warnings[0]
    assert "不能确认错误" in warnings[0]


def test_cancellation_during_quality_parser_keeps_old_split_and_artifact(make_project, pdir, monkeypatch):
    from dubforge.domain import crud

    uid, ctx = _ctx(make_project, pdir, lines=[(0.0, 2.0, "we arrived in china")])
    old_path = pdir(uid) / "artifacts" / "split" / "split.txt"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old = b"1. 0.000-2.000: old approved source\n"
    old_path.write_bytes(old)
    original_artifact = crud.add_artifact(ctx.project.id, "split", "text", "split.txt",
                                          old_path, "artifacts/split/split.txt")
    prepare = step_split._prepare_semantic_parts

    def finish_then_cancel(*args, **kwargs):
        result = prepare(*args, **kwargs)
        ctx._cancel.set()
        return result

    monkeypatch.setattr(step_split, "_prepare_semantic_parts", finish_then_cancel)
    calls = _responses(monkeypatch, "1. 0.00-2.00: we arrived in china")
    with pytest.raises(StepCancelled):
        step_split.run(ctx)
    assert len(calls) == 1
    assert old_path.read_bytes() == old
    latest = crud.latest_artifacts(ctx.project.id, ["split"])["split/split.txt"]
    assert (latest.id, latest.sha256) == (original_artifact.id, original_artifact.sha256)
