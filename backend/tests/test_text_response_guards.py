"""文本任务守卫按真实协议验收；只用隔离配置和离线增量，不请求模型。"""
import re
from types import SimpleNamespace

import pytest

from dubforge import config
from dubforge.backends import llm
from dubforge.backends.llm_guard import ResponseGuardError
from dubforge.errors import StepFailedError
from dubforge.pipeline import step_split as split
from dubforge.pipeline import step_translate as translate
from dubforge.utils.srt import SubLine


@pytest.fixture
def ctx(isolated_env, tmp_path):
    settings = {"llm": {"timeout": 50}, "translate": {"max_context_tokens": 200_000}}
    logs = []
    return SimpleNamespace(
        cfg=lambda key, default=None: config.get(key, default, settings=settings),
        settings=settings, pdir=tmp_path / "project", force=False,
        log=logs.append, logs=logs, llm_trace=lambda *_a, **_kw: None,
        check_cancel=lambda: None, check_pause=lambda: None,
        llm_profile_snapshot=lambda _selected: [dict(name="离线", model="fixture", params={})],
    )


def source_lines():
    return [SubLine(1, 0, 1, "Hello again."), SubLine(2, 1, 2, "Goodbye.")]


def translation_lines():
    return [dict(id=i, start=pos * 2, end=pos * 2 + 1, text=f"source {i}")
            for pos, i in enumerate((11, 14, 19))]


def feed_chunks(guard, raw, chunk_size):
    for offset in range(0, len(raw), chunk_size):
        guard.feed(raw[offset:offset + chunk_size])
    guard.finish()


@pytest.mark.parametrize("chunk_size", [1, 7, 100_000])
def test_split_factory_accepts_fragmented_bom_crlf_and_existing_format_compatibility(ctx, chunk_size):
    raw = ("\ufeff\r\n```text\r\n# 原文字幕\r\n"
           " 1 . 0.000 - 1.000 : Hello again.  \r\n\r\n"
           "2. 1.000-2.000: Goodbye.\r\n```\r\n\r\n"
           "整理说明：只调整标点。\r\n不改变正文。\r\n")
    guard = split._split_response_guard(ctx, source_lines(), "Hello again. Goodbye.")
    feed_chunks(guard, raw, chunk_size)
    warnings = []
    parts = split._parse_timed_protocol(raw, 0, 2, warnings)
    assert [(p["start"], p["end"], p["text"]) for p in parts] == [
        (0, 1, "Hello again."), (1, 2, "Goodbye.")]
    assert warnings and guard.counts["content"] == len(raw)
    assert guard.timeout_s == 50


def test_split_checks_only_complete_line_then_stops_before_final_parser(ctx):
    guard = split._split_response_guard(ctx, source_lines(), "Hello again. Goodbye.")
    guard.feed("1. 0-1: Hello again.\n2. 1-2: Goodbye.\n")
    guard.feed("I should review the whole output")
    with pytest.raises(ResponseGuardError) as error:
        guard.feed("\n")
    assert error.value.code == "LLM_OUTPUT_CONTRACT"
    assert guard.counts["content"] < 200


@pytest.mark.parametrize("bad", [
    "2. 0-1: Hello again.\n",                 # 首行重编号
    "1. 0-1: Hello again.\n1. 1-2: Goodbye.\n",  # 重复
    "1. 0-1: Hello again.\n3. 1-2: Goodbye.\n",  # 缺号
    "1. 0-1: Hello again.\n2. 1-2: Goodbye.\n1. 0-1: Second draft.\n",
    "1. 0-1: Hello again.\n\nNotes: complete\n1. 0-1: Second draft.\n",
])
def test_split_rejects_restarts_and_multiple_finished_drafts(ctx, bad):
    guard = split._split_response_guard(ctx, source_lines(), "Hello again. Goodbye.")
    with pytest.raises(ResponseGuardError) as error:
        feed_chunks(guard, bad, 1)
    assert error.value.code == "LLM_OUTPUT_CONTRACT"


def test_split_final_time_validation_still_runs_after_format_guard(ctx):
    raw = "1. 1.000-0.000: Hello again."
    guard = split._split_response_guard(ctx, source_lines(), "Hello again.")
    feed_chunks(guard, raw, 3)
    with pytest.raises(ValueError):
        split._parse_timed_protocol(raw, 0, 2)


@pytest.mark.parametrize("chunk_size", [1, 11, 100_000])
def test_translate_factory_keeps_original_ids_under_arbitrary_chunks(ctx, chunk_size):
    raw = "\ufeff\r\n 11 、 你好 \r\n\r\n14． 欢迎\r\n19. 再见\r\n"
    chunk = translation_lines()
    guard = translate._translation_response_guard(ctx, chunk, [11, 14, 19], False)
    feed_chunks(guard, raw, chunk_size)
    warnings = []
    got, empty, summary = translate._parse_numbered_output(raw, chunk, True, warnings=warnings)
    assert got == {11: "你好", 14: "欢迎", 19: "再见"}
    assert empty == [] and summary == "" and warnings
    assert guard.counts["content"] == len(raw)


@pytest.mark.parametrize("final_window", [False, True])
def test_translate_handoff_and_final_surplus_marker_match_final_parser(ctx, final_window):
    raw = "11. 你好\n14. 欢迎\n19. 再见\n\n" + translate.HANDOFF_MARKER + "\r\n\r\n称谓保持一致\r\n"
    chunk = translation_lines()
    guard = translate._translation_response_guard(ctx, chunk, [11, 14, 19], not final_window)
    feed_chunks(guard, raw, 1)
    got, empty, summary = translate._parse_numbered_output(raw, chunk, final_window)
    assert len(got) == 3 and not empty and summary == "称谓保持一致"


@pytest.mark.parametrize("bad", [
    "1. 你好\n",  # 原编号不能从1重排
    "11. 你好\n11. 另一个版本\n",
    "19. 再见\n14. 欢迎\n",
    "11. 你好\nI should check the translation.\n",
    "11. 你好\n14. 欢迎\n19. 再见\n11. New draft\n",
    "11. 你好\n" + translate.HANDOFF_MARKER + "\n术语保持一致\n14. 欢迎\n",
])
def test_translate_factory_stops_non_protocol_lines_and_numbered_rewrites(ctx, bad):
    guard = translate._translation_response_guard(ctx, translation_lines(), [11, 14, 19], False)
    with pytest.raises(ResponseGuardError) as error:
        feed_chunks(guard, bad, 2)
    assert error.value.code == "LLM_OUTPUT_CONTRACT"


def test_translate_missing_and_empty_lines_reach_dynamic_local_repair(ctx):
    chunk = translation_lines()
    validator = translate._valid_window(chunk, final_window=False)
    initial = "11. 你好\n14. \n" + translate.HANDOFF_MARKER + "\n称谓保持一致"
    initial_guard = translate._translation_response_guard(ctx, chunk, validator.required_ids, True)
    feed_chunks(initial_guard, initial, 1)
    with pytest.raises(translate._MissingTranslationLines) as missing:
        validator(initial)
    assert missing.value.missing == [14, 19]
    validator.required_ids = missing.value.missing
    validator.require_handoff = False
    repaired = "14. 欢迎\n19. 再见"
    repair_guard = translate._translation_response_guard(ctx, chunk, validator.required_ids, False)
    feed_chunks(repair_guard, repaired, 1)
    result = validator(repaired)
    assert result == {"lines": {11: "你好", 14: "欢迎", 19: "再见"},
                      "handoff_summary": "称谓保持一致"}
    wrong_guard = translate._translation_response_guard(ctx, chunk, [14, 19], False)
    with pytest.raises(ResponseGuardError):
        wrong_guard.feed("11. 不能偷偷重译已确认句\n")


def test_translation_guard_is_not_a_completion_certificate(ctx):
    chunk = translation_lines()
    raw = "11. 你好\n14. 欢迎\n19. 再见"
    guard = translate._translation_response_guard(ctx, chunk, [11, 14, 19], True)
    feed_chunks(guard, raw, 1)
    with pytest.raises(translate._TranslationProtocolError):
        translate._parse_numbered_output(raw, chunk, False)


def test_guarded_attempt_is_replaced_with_one_fresh_complete_candidate(ctx, monkeypatch):
    calls, parsed = [], []
    candidates = ["1. 0-1: Hello again.\nWait, I should review it.\n",
                  "1. 0-1: Hello again.\n2. 1-2: Goodbye."]

    def chat(prompt="", **kwargs):
        calls.append((prompt, kwargs))
        guard = kwargs["response_guard_factory"]()
        raw = candidates[len(calls) - 1]
        feed_chunks(guard, raw, 1)
        return raw

    def parser(raw):
        parsed.append(raw)
        return split._parse_timed_protocol(raw, 0, 2)

    monkeypatch.setattr(llm, "chat", chat)
    result = llm.chat_validated(
        "全文素材", parser, max_retries=2,
        response_guard_factory=lambda: split._split_response_guard(ctx, source_lines(), "Hello again. Goodbye."))
    assert len(calls) == 2 and len(parsed) == 1 and len(result) == 2
    assert all(not kwargs.get("messages") for _, kwargs in calls)
    assert "Wait" not in calls[1][0]


def test_protocol_failure_is_finite_and_never_reaches_final_parser(ctx, monkeypatch):
    calls, parsed = [], []

    def chat(prompt="", **kwargs):
        calls.append(prompt)
        guard = kwargs["response_guard_factory"]()
        guard.feed("I should think before returning a draft.\n")
        pytest.fail("异常响应不应继续")

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(ResponseGuardError) as error:
        llm.chat_validated("全文素材", lambda raw: parsed.append(raw), max_retries=3,
            response_guard_factory=lambda: split._split_response_guard(ctx, source_lines(), "Hello again. Goodbye."))
    assert error.value.code == "LLM_OUTPUT_CONTRACT"
    assert len(calls) == 3 and parsed == []


@pytest.mark.parametrize("ending", ["", "\n", "\r\n\r\n"])
def test_legal_long_repetition_keeps_every_delta_in_split_and_cross_language_translation(ctx, ending):
    source = "thank you " * 800
    translated = "谢谢 " * 1600
    lines = [SubLine(1, 0, 600, source)]
    raw_split = "1. 0-600: " + source.rstrip() + ending
    split_guard = split._split_response_guard(ctx, lines, source)
    feed_chunks(split_guard, raw_split, 37)
    assert split._parse_timed_protocol(raw_split, 0, 600)[0]["text"] == source.rstrip()
    chunk = [dict(id=81, start=0, end=600, text=source)]
    raw_translated = "81. " + translated.rstrip() + ending
    translate_guard = translate._translation_response_guard(ctx, chunk, [81], False)
    feed_chunks(translate_guard, raw_translated, 1)
    got, empty, _ = translate._parse_numbered_output(raw_translated, chunk, True)
    assert got[81] == translated.rstrip() and not empty
    assert translate_guard.counts["content"] == len(raw_translated)


def test_local_repair_guard_failure_resets_next_request_to_full_window(ctx, monkeypatch):
    chunk = [dict(id=i, start=i * 2, end=i * 2 + 1, text=f"source {i}") for i in range(1, 51)]
    calls = []

    def chat(prompt="", **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raw = "\n".join(f"{i}. 第一轮{i}" for i in range(1, 51) if i != 25)
        elif len(calls) == 2:
            assert "【待重译片段】" in prompt
            raw = "I should check the local repair.\n"
        else:
            assert "【待翻译原文】" in prompt and "【待重译片段】" not in prompt
            raw = "\n".join(f"{i}. 完整重译{i}" for i in range(1, 51))
        feed_chunks(kwargs["response_guard_factory"](), raw, 7)
        return raw

    monkeypatch.setattr(llm, "chat", chat)
    result, summary = translate._translate_window(ctx, chunk, "system", "", 1, 1)
    assert len(calls) == 3 and len(result) == 50 and summary == ""
    assert result[1] == "完整重译1" and result[25] == "完整重译25"


def test_guard_aborts_and_successful_parser_share_the_same_recovery_budget(ctx, monkeypatch):
    chunk = translation_lines()
    budget = {"remaining": 5}
    calls = []

    def chat(prompt="", **kwargs):
        calls.append(prompt)
        raw = ("Wait, I should check it.\n" if len(calls) < 3 else
               "11. 你好\n14. 欢迎\n19. 再见")
        feed_chunks(kwargs["response_guard_factory"](), raw, 1)
        return raw

    monkeypatch.setattr(llm, "chat", chat)
    result, _ = translate._translate_window(ctx, chunk, "system", "", 1, 1,
                                            recovery_budget=budget)
    assert len(calls) == 3 and len(result) == 3
    assert budget["remaining"] == 2  # 两次流式中止和一次正式parser，各扣一次。


def test_exhausted_guard_recovery_budget_never_sends_a_child_request(ctx, monkeypatch):
    budget = {"remaining": 2}
    calls = []

    def chat(prompt="", **kwargs):
        calls.append(prompt)
        feed_chunks(kwargs["response_guard_factory"](), "11. 第一稿\n11. 第二稿\n", 1)
        pytest.fail("重复编号响应不应返回")

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(StepFailedError) as error:
        translate._translate_window(ctx, translation_lines(), "system", "", 1, 1,
                                    recovery_budget=budget)
    assert error.value.code == "LLM_OUTPUT_INVALID"
    assert len(calls) == 2 and budget["remaining"] == 0


def test_structural_guard_failure_retains_bounded_adaptive_and_fresh_reviews(ctx, monkeypatch):
    chunk = [dict(id=i, start=i, end=i + .8, text=f"source {i}") for i in range(1, 5)]
    budget = {"remaining": 10}
    calls = []

    def chat(prompt="", **kwargs):
        assert not kwargs.get("messages")
        calls.append(prompt)
        if len(calls) <= 3:
            raw = "1. 第一稿\n1. 第二稿\n"
        else:
            marker = ("【待翻译原文与候选译文】" if "【待翻译原文与候选译文】" in prompt else
                      "【待翻译原文】")
            body = prompt.split(marker, 1)[1].split("【", 1)[0]
            ids = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", body, re.MULTILINE)]
            raw = "\n".join(f"{i}. 有效译文{i}" for i in ids)
            if "译文后另起一行输出交接标记" in prompt:
                raw += "\n" + translate.HANDOFF_MARKER + "\n称谓保持一致"
        feed_chunks(kwargs["response_guard_factory"](), raw, 3)
        return raw

    monkeypatch.setattr(llm, "chat", chat)
    result, _ = translate._translate_window(ctx, chunk, "system", "", 1, 1,
                                            recovery_budget=budget)
    assert result == {i: f"有效译文{i}" for i in range(1, 5)}
    assert len(calls) == 7  # 原窗3次、两个子窗各翻译+校对；父合并不再次审核。
    assert sum("【待翻译原文与候选译文】" in prompt for prompt in calls) == 2
    assert budget["remaining"] == 3


def test_request_deadline_stops_without_retry_or_adaptive(ctx, monkeypatch):
    budget = {"remaining": 5}
    calls = []

    def chat(prompt="", **kwargs):
        calls.append(prompt)
        guard = kwargs["response_guard_factory"]()
        guard.started -= guard.timeout_s + 1
        guard.check()
        pytest.fail("超时响应不应继续")

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(ResponseGuardError) as error:
        translate._translate_window(ctx, translation_lines(), "system", "", 1, 1,
                                    recovery_budget=budget)
    assert error.value.code == "LLM_REQUEST_DEADLINE"
    assert len(calls) == 1 and budget["remaining"] == 4
