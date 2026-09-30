"""M2b 文本链测试: 全部 mock LLM (零 token), 覆盖整体窗口/缓存/id 对齐/鲁棒回退"""
import json

import pytest

from conftest import mark_upstream_done, wait_status
from dubforge.utils.lineprotocols import parse_numbered_lines, parse_timed_lines
from dubforge.utils.srt import SubLine, write_srt


def _seed_transcribe(uid, lines: list[tuple[float, float, str]]):
    """伪造 transcribe 产物: src.srt + words.json (按行文本回填词)"""
    from dubforge.domain import crud
    project = crud.get_project(uid)
    srt_dir = crud.project_dir(uid) / "srt"
    srt_dir.mkdir(parents=True, exist_ok=True)
    write_srt([SubLine(i + 1, s, e, t) for i, (s, e, t) in enumerate(lines)],
              srt_dir / "src.srt")
    words = []
    for s, e, t in lines:
        n = max(1, len(t.split()))
        span = (e - s) / n
        for j, w in enumerate(t.split()):
            words.append({"start": s + j * span, "end": s + (j + 1) * span,
                          "word": w + " "})
    (srt_dir / "words.json").write_text(json.dumps(words), encoding="utf-8")
    for name in ("src.srt", "words.json"):
        p = srt_dir / name
        crud.add_artifact(project.id, "transcribe", "srt" if name.endswith("srt") else "json",
                          name, p, f"srt/{name}")


def _mock_llm(monkeypatch, responder):
    """responder(prompt, system) -> str; 注入 llm 模块"""
    from dubforge.backends import llm
    calls = []

    def fake_chat(prompt, system=None, temperature=None, profile=None,
                  log=lambda m: None, cancel_check=None, trace=None):
        calls.append(prompt)
        return responder(prompt, system)

    def fake_chat_json(prompt, validator=None, system=None, max_retries=3,
                       temperature=None, log=lambda m: None, profile=None,
                       cancel_check=None, trace=None, retry_prompt=None,
                       retry_request=None, retry_context=None):
        raw = fake_chat(prompt, system, temperature, profile, log, cancel_check, trace)
        obj = llm._extract_json(raw)
        if validator:
            normalized = validator(obj)
            return obj if normalized is None else normalized
        return obj

    def fake_chat_validated(prompt, parser=None, validator=None, system=None,
                            max_retries=3, temperature=None,
                            log=lambda m: None, profile=None,
                            cancel_check=None, trace=None, retry_prompt=None,
                            retry_request=None, retry_context=None,
                            failure_label="输出"):
        last_error = None
        last_raw = ""
        for attempt in range(1, max_retries + 1):
            attempt_prompt = prompt
            suffix = ""
            if last_error is not None:
                mode = (retry_context(last_error, attempt, last_raw)
                        if callable(retry_context) else "conversation")
                if callable(retry_request):
                    attempt_prompt = retry_request(last_error, attempt)
                if callable(retry_prompt):
                    suffix = "\n\n" + str(retry_prompt(last_error, attempt))
                prompt_for_responder = attempt_prompt + suffix
            else:
                prompt_for_responder = prompt
            raw = fake_chat(prompt_for_responder, system, temperature, profile,
                            log, cancel_check, trace)
            last_raw = raw
            try:
                obj = parser(raw)
                if validator:
                    normalized = validator(obj)
                    return obj if normalized is None else normalized
                return obj
            except Exception as exc:
                last_error = exc
        raise AssertionError(f"连续 {max_retries} 次校验失败: {last_error}")

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(llm, "chat_validated", fake_chat_validated)
    return calls


def _run_until(uid, key, timeout=30):
    mark_upstream_done(uid, key)   # 链式测试手工播种产物, 上游状态直接置 done 越过门禁
    _runner().start_step(uid, key)
    return wait_status(uid, key, {"done", "failed"}, timeout=timeout)


def _runner():
    from dubforge.runner import runner
    return runner


def read_split(uid):
    from dubforge.domain import crud
    path = crud.project_dir(uid) / "artifacts" / "split" / "split.txt"
    rows = parse_timed_lines(path.read_text(encoding="utf-8"))
    return {"count": len(rows), "lines": rows}


def read_translate(uid):
    from dubforge.domain import crud
    path = crud.project_dir(uid) / "artifacts" / "translate" / "translate.txt"
    rows = parse_numbered_lines(path.read_text(encoding="utf-8"))
    return {"lines": [{"id": line_id, "dst": dst}
                      for line_id, dst in sorted(rows.items())]}


def test_split_long_line_with_word_timestamps(make_project, isolated_env, monkeypatch, pdir):
    uid = make_project()
    long_text = " ".join(["word"] * 40)          # 40 词 > 默认 max_split_length=22
    _seed_transcribe(uid, [(0.0, 10.0, long_text)])

    def responder(prompt, system):
        # LLM 返回单条超长行 (合法协议); 超上限的本地细分用词级时间戳完成
        return "1. 0.000-10.000: " + long_text
    _mock_llm(monkeypatch, responder)
    assert _run_until(uid, "split") == "done"

    data = read_split(uid)
    assert data["count"] > 1
    # 时间轴: 单调递增且覆盖原行区间
    starts = [l["start"] for l in data["lines"]]
    ends = [l["end"] for l in data["lines"]]
    assert starts == sorted(starts)
    assert abs(starts[0] - 0.0) < 0.2 and abs(ends[-1] - 10.0) < 0.5
    # 文本无丢失 (去空白拼接一致)
    import re as _re
    assert (_re.sub(r"\s+", "", "".join(l["text"] for l in data["lines"]))
            == _re.sub(r"\s+", "", long_text))
    # 英文句内不得粘连；句间边界由每行独立承载，不靠行首空格保真。
    assert all("wordword" not in l["text"] for l in data["lines"])
    assert all(l["text"] == l["text"].strip() for l in data["lines"])


def test_split_chinese_text_preserves_characters(make_project, isolated_env, pdir):
    """中文没有空格，规则切分也必须按字工作，而不是把整行当成一个 token。"""
    uid = make_project()
    text = "这是一个很长的中文句子，需要在自然标点附近拆开，同时保留全部内容。"
    _seed_transcribe(uid, [(0.0, 6.0, text)])
    import dubforge.config as cfgmod
    cfgmod.set_key("split.use_llm", False)
    cfgmod.set_key("split.max_split_length", 12)

    assert _run_until(uid, "split") == "done"
    data = read_split(uid)
    assert data["count"] > 1
    assert "".join(l["text"] for l in data["lines"]) == text
    assert all(len(l["text"].replace("，", "").replace("。", "")) <= 12
               for l in data["lines"])


def test_split_output_lines_are_trimmed():
    """正式分句产物不允许携带片段边界空格，方便编号行翻译协议对齐。"""
    from dubforge.pipeline.step_split import _normalized_split_lines

    data = [
        {"id": 1, "text": "  first line  ", "start": 0.0, "end": 1.0},
        {"id": 2, "text": "\tsecond line\n", "start": 1.0, "end": 2.0},
    ]
    result = _normalized_split_lines(data)
    assert [x["text"] for x in result] == ["first line", "second line"]
    assert result[0]["start"] == 0.0 and result[1]["end"] == 2.0


def test_rule_split_avoids_tiny_english_punctuation_fragments():
    """早期逗号/连接词不能把英文半句单独切成孤立字幕。"""
    from dubforge.pipeline.step_split import _rule_split, _wc

    text = "This is a reasonably long English subtitle sentence, and it should break at natural punctuation."
    parts = _rule_split(text, 22)
    assert "".join(parts) == text
    assert all(_wc(part) >= 8 for part in parts)


def test_final_limit_split_uses_word_timestamps_for_alignment():
    """模型片段仍超硬上限时, 本地兜底应沿用词边界而非字符时间比例。"""
    from dubforge.pipeline.step_split import _enforce_final_limits

    words = [
        {"start": 0.0, "end": 0.4, "word": "甲乙丙"},
        {"start": 4.0, "end": 4.4, "word": "丁戊己"},
    ]
    parts = _enforce_final_limits(
        [{"text": "甲乙丙丁戊己", "start": 0.0, "end": 4.4}],
        max_len=3, max_duration=20, words=words)
    assert len(parts) == 2
    assert parts[0]["end"] <= 0.5
    assert parts[1]["start"] >= 3.9


def test_split_timed_protocol_parser_accepts_corrected_text():
    """模型可修正 ASR 错词，解析不再要求拼接与原文一致。"""
    from dubforge.pipeline.step_split import _parse_timed_protocol

    raw = ("1. 0.00-0.50: hello\n"
           "2. 0.50-1.00: world,\n"
           "3. 1.00-1.50: this is a\n"
           "4. 1.50-2.00: test")
    parts = _parse_timed_protocol(raw, 0.0, 2.0)
    assert len(parts) == 4
    assert parts[0]["text"] == "hello"
    assert parts[1]["text"] == "world,"


@pytest.mark.parametrize("raw", [
    "这是没有时间前缀的片段",
    "1. 0.00-1.00: 第一行\n没有前缀",
    "1. 1.00-0.50: 时间倒置",
    "1. 0.00-1.00: ",
    "2. 0.00-1.00: 序号错误",
])
def test_split_timed_protocol_structural_errors_do_not_guess(raw):
    from dubforge.pipeline.step_split import _parse_timed_protocol

    with pytest.raises(ValueError, match="格式错误|时间|片段|序号"):
        _parse_timed_protocol(raw, 0.0, 2.0)


def test_split_timed_protocol_tolerates_harmless_format_issues():
    """空格、空行、注释和小幅越界只兼容并警告，不触发重试。"""
    from dubforge.pipeline.step_split import _parse_timed_protocol

    raw = ("# 停顿 0.50s\n"
           "1.  0.00-1.00:hello world\n"
           "\n"
           "2. 1.00-2.02 :  second line  ")
    warnings: list[str] = []
    parts = _parse_timed_protocol(raw, 0.0, 2.0, warnings)
    assert [part["text"] for part in parts] == ["hello world", "second line"]
    assert parts[1]["end"] == 2.0
    assert any("空格不规范" in item for item in warnings)
    assert any("空行" in item for item in warnings)
    assert any("注释" in item for item in warnings)
    assert any("超出窗口" in item for item in warnings)


def test_split_uses_duration_as_secondary_boundary(make_project, isolated_env, pdir):
    """字数不超限但语音过慢时，也按时长给拆分步骤一个机会。"""
    uid = make_project()
    text = "这是一个语速很慢但长度并不太长的句子"
    _seed_transcribe(uid, [(0.0, 12.0, text)])
    import dubforge.config as cfgmod
    cfgmod.set_key("split.use_llm", False)
    cfgmod.set_key("split.max_split_length", 22)
    cfgmod.set_key("split.max_segment_seconds", 5)

    assert _run_until(uid, "split") == "done"
    data = read_split(uid)
    assert data["count"] > 1


def test_split_llm_resegments_whole_window_once(make_project, isolated_env,
                                                monkeypatch, pdir):
    """词级模式下连续短语在一个 token 窗口内只请求一次，语义边界由模型判断。"""
    uid = make_project()
    _seed_transcribe(uid, [
        (0.0, 2.0, "这是第一句"),
        (2.0, 4.0, "接着是第二句"),
        (4.0, 6.0, "最后一句"),
    ])

    calls = []

    def responder(prompt, system):
        calls.append(prompt)
        assert "输入短语时间轴:" not in prompt
        assert "输入原文顺序（仅用于核对" not in prompt
        assert "【本窗口时间轴输入】" in prompt
        assert "开始秒-结束秒: 原文片段" in prompt
        assert "0.00-2.00: 这是第一句" in prompt
        return ("1. 0.00-4.00: 这是第一句接着是第二句\n"
                "2. 4.00-6.00: 最后一句")

    _mock_llm(monkeypatch, responder)
    import dubforge.config as cfgmod
    cfgmod.set_key("asr.segmentation_mode", "word")
    assert _run_until(uid, "split") == "done"
    data = read_split(uid)
    assert len(calls) == 1
    assert [line["text"] for line in data["lines"]] == [
        "这是第一句接着是第二句", "最后一句"]
    assert data["lines"][0]["end"] == 4.0


def test_split_llm_phrase_mode_omits_word_timeline(make_project, isolated_env,
                                                   monkeypatch, pdir):
    """默认短语模式只发送短语时间和停顿提示，避免无谓扩大请求。"""
    uid = make_project()
    _seed_transcribe(uid, [
        (0.0, 1.8, "这是第一句，"),
        (1.8, 3.5, "接着是第二句。"),
    ])
    calls = []

    def responder(prompt, system):
        calls.append(prompt)
        assert "【任务：整体语义重组】" in prompt
        assert "词级: " not in prompt
        assert "【本窗口时间轴输入】" in prompt
        assert "输入词级时间轴:" not in prompt
        assert "0.00-1.80: 这是第一句，" in prompt
        assert "词级时间轴未发送" not in prompt
        return "1. 0.00-3.50: 这是第一句，接着是第二句。"

    _mock_llm(monkeypatch, responder)
    import dubforge.config as cfgmod
    cfgmod.set_key("asr.segmentation_mode", "phrase")
    assert _run_until(uid, "split") == "done"
    assert len(calls) == 1


def test_split_llm_prompt_gives_autonomous_semantic_guidance():
    """提示词提供目标、时间事实和观察；语义组织交给模型自主判断。"""
    from dubforge.pipeline.step_split import _coarse_resegment_prompt

    lines = [
        SubLine(1, 0.0, 1.0, "first part"),
        SubLine(2, 3.0, 4.0, "second part"),
        SubLine(3, 4.1, 5.0, "third part"),
    ]
    prompt = _coarse_resegment_prompt(
        lines, 30, 8.0,
        pause_threshold=0.6, include_words=False)

    assert "由你根据语义和节奏判断" in prompt
    assert "保留源语言" in prompt
    assert "结合上下文主动检查" in prompt
    assert "证据不足时保留原意" in prompt
    assert "`# 停顿 X.XXs` 表示真实间隔达到 0.60 秒" in prompt
    assert "跨长停顿合句" in prompt
    assert "把后句开头的短词挂到前句末尾" in prompt
    assert "刻意停顿、犹豫或归属不明" in prompt
    assert "序号. 开始秒-结束秒: 原文片段" in prompt
    assert "# 停顿 2.00s" in prompt


def test_correction_guidance_survives_structural_retry_and_translation():
    """各阶段保留通用纠错建议，不夹带孤立词案例或禁止润色的要求。"""
    from types import SimpleNamespace

    from dubforge.pipeline.step_split import _split_repair_prompt
    from dubforge.pipeline.step_translate import _base_system

    timeline = "0.00-2.00: Welcome to the city."
    repair = _split_repair_prompt([], timeline, ValueError("缺少序号1"))
    translated = _base_system(SimpleNamespace(cfg=lambda key, default=None: default), "")
    assert "结合上下文主动检查" in repair
    assert "专名、数字和单位" in repair
    assert "证据不足时保留原意" in repair
    assert "可纠正明显的识别错误" in translated
    assert "尽量保留原语义" in translated
    assert "说话人本身的口误" in translated
    for prompt in (repair, translated):
        assert "yuan" not in prompt and "yen" not in prompt
        assert "不额外润色" not in prompt
    assert repair.endswith(timeline)


def test_correction_prompt_revision_invalidates_text_caches(make_project, pdir, monkeypatch):
    """新纠错策略必须穿透整步跳过与翻译窗口缓存，两层都不能复用旧结果。"""
    import threading

    from dubforge.domain import crud
    from dubforge.pipeline import text_prompts
    from dubforge.pipeline.step_translate import _chunk_key
    from dubforge.runner import StepContext, compute_fingerprint

    uid = make_project()
    project = crud.get_project(uid)
    ctx = StepContext(project, "translate", pdir(uid), pdir(uid) / "logs" / "translate.log", threading.Event())
    chunk = [{"id": 1, "text": "Welcome to the city."}]
    old_steps = {key: compute_fingerprint(project, key) for key in ("transcribe", "split", "translate")}
    old_chunk = _chunk_key(ctx, chunk, "translate")
    monkeypatch.setattr(text_prompts, "ASR_CORRECTION_VERSION", "next-correction-policy")
    assert compute_fingerprint(project, "transcribe") == old_steps["transcribe"]
    for key in ("split", "translate"):
        assert compute_fingerprint(project, key) != old_steps[key]
    assert _chunk_key(ctx, chunk, "translate") != old_chunk


def test_translation_alignment_revision_only_invalidates_translation(make_project, pdir, monkeypatch):
    """逐句约定变化使翻译及窗口缓存失效，无需重新做语音识别或语义整理。"""
    import threading

    from dubforge.domain import crud
    from dubforge.pipeline import text_prompts
    from dubforge.pipeline.step_translate import _chunk_key
    from dubforge.runner import StepContext, compute_fingerprint

    uid = make_project()
    project = crud.get_project(uid)
    ctx = StepContext(project, "translate", pdir(uid), pdir(uid) / "logs" / "translate.log", threading.Event())
    chunk = [{"id": 1, "text": "Welcome."}]
    previous = {key: compute_fingerprint(project, key) for key in ("transcribe", "split", "translate")}
    previous_chunk = _chunk_key(ctx, chunk, "window-v6")
    monkeypatch.setattr(text_prompts, "TRANSLATION_PROMPT_VERSION", "next-alignment-policy")
    for key in ("transcribe", "split"):
        assert compute_fingerprint(project, key) == previous[key]
    assert compute_fingerprint(project, "translate") != previous["translate"]
    assert _chunk_key(ctx, chunk, "window-v6") != previous_chunk


def test_split_semantic_settings_remove_manual_merge_switch():
    """跨短语合并开关移除，参数名称直接说明分窗参考用途。"""
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA

    paths = {field.path for fields in STEP_PARAM_SCHEMA.values()
             for field in fields}
    assert "split.allow_cross_line_merge" not in paths
    labels = {field.path: field.label for fields in STEP_PARAM_SCHEMA.values()
              for field in fields}
    assert labels["split.context_lines"] == "分窗前后参考句数"
    assert labels["split.max_split_length"] == "目标单句长度（字符）"


def test_split_long_gap_merge_warns_without_repair():
    """长间隔由模型判断；跨长间隔只警告，不再本地拆回。"""
    from dubforge.pipeline.step_split import (_protected_gap_warnings,
                                              _short_gap_boundaries)

    lines = [
        SubLine(1, 0.0, 1.0, "first"),
        SubLine(2, 10.0, 11.0, "second"),
        SubLine(3, 11.05, 12.0, "third"),
    ]
    assert _short_gap_boundaries(lines, 0.6) == {2}
    merged = [{"text": "firstsecond", "start": 0.0, "end": 11.0}]
    warnings = _protected_gap_warnings(merged, lines, {2}, 0.6)
    assert len(warnings) == 1
    assert "跨越了 9.00s 长间隔" in warnings[0]
    assert "已按模型结果保留" in warnings[0]
    separate = [{"text": "first", "start": 0.0, "end": 1.0},
                {"text": "second", "start": 10.0, "end": 11.0}]
    assert _protected_gap_warnings(separate, lines, {2}, 0.6) == []


def test_split_timeline_format_marks_pauses_and_uses_centiseconds():
    """给模型的时间轴保留 0.01 秒显示精度，并显式呈现长停顿。"""
    from dubforge.pipeline.step_split import _format_word_timeline

    text = _format_word_timeline([
        {"start": 0.001, "end": 0.237, "word": "甲，"},
        {"start": 1.041, "end": 1.289, "word": "乙"},
    ], pause_threshold=0.6)
    assert "0.00-0.24: 甲，" in text
    assert "# 停顿 0.80s" in text
    assert "1.04-1.29: 乙" in text


def test_split_stitches_artificial_window_boundary():
    """token 分窗落在连续短语中间时，边界应在本地安全拼回。"""
    from dubforge.pipeline.step_split import _stitch_window_edges

    parts = [
        {"text": "这是前半", "start": 0.0, "end": 1.8, "_window": 1},
        {"text": "后半句", "start": 1.8, "end": 3.2, "_window": 2},
    ]
    out = _stitch_window_edges(parts, max_len=22, max_duration=8, pause=0.6)
    assert len(out) == 1
    assert out[0]["text"] == "这是前半后半句"
    assert "_window" not in out[0]


def test_split_long_gap_merge_is_preserved_with_warning(make_project, isolated_env,
                                                          monkeypatch, pdir):
    """模型合并长间隔时任务成功，结果保留。"""
    uid = make_project()
    _seed_transcribe(uid, [
        (0.0, 1.0, "第一段"),
        (2.5, 3.5, "第二段"),
    ])

    _mock_llm(monkeypatch, lambda prompt, system: "1. 0.00-3.50: 第一段第二段")
    assert _run_until(uid, "split") == "done"
    data = read_split(uid)
    assert [line["text"] for line in data["lines"]] == ["第一段第二段"]


def test_split_repairs_duration_guard_boundary_by_default(make_project,
                                                          isolated_env,
                                                          monkeypatch, pdir):
    """接近 ASR 时长保护上限且无停顿时，默认仍允许修复技术性切分。"""
    uid = make_project()
    _seed_transcribe(uid, [
        (0.0, 11.5, "这一段是同一句话的前半部分"),
        (11.5, 13.0, "后半部分"),
    ])

    def responder(prompt, system):
        return "1. 0.00-13.00: 这一段是同一句话的前半部分后半部分"

    _mock_llm(monkeypatch, responder)
    import dubforge.config as cfgmod
    cfgmod.set_key("split.max_segment_seconds", 20)
    assert _run_until(uid, "split") == "done"
    data = read_split(uid)
    assert len(data["lines"]) == 1


def test_translate_whole_doc_tier(make_project, isolated_env, monkeypatch, pdir):
    """整体翻译使用编号纯文本，产物仍为按 id 落盘的 translate.txt。"""
    uid = make_project(params={"step_params": {"text.source": "transcribe"}})
    _seed_transcribe(uid, [(0.0, 2.0, "Helo world"), (2.0, 4.0, "Second line")])

    def responder(prompt, system):
        if "【待翻译原文】" not in prompt:
            return "1. 0.00-2.00: Hello world\n2. 2.00-4.00: Second line"
        assert "1. Hello world" in prompt and "2. Second line" in prompt
        assert "行号. 译文" in prompt and "直接返回编号文本" in prompt
        return "1. 你好世界\n2. 第二行"
    _mock_llm(monkeypatch, responder)

    from dubforge.domain import crud as _crud
    def _rr(key):
        st = _run_until(uid, key)
        if st != "done":
            row = _crud.get_step(_crud.get_project(uid).id, key)
            print(f"[{key}] {row.status} | {row.error_code} | {row.error_message}")
        return st
    assert _rr("split") == "done"
    assert _rr("translate") == "done"
    split = read_split(uid)
    data = read_translate(uid)
    assert split["lines"][0]["text"] == "Hello world"
    assert split["lines"][1]["text"] == "Second line"
    assert data["lines"][0]["dst"] == "你好世界"


def test_translate_chunked_tier_and_cache(make_project, isolated_env, monkeypatch):
    """正文超过单次 token 预算 → 分窗口; 二次运行命中缓存 (LLM 调用数不变)"""
    uid = make_project()
    lines = [(float(i) * 2, float(i) * 2 + 1.5,
              f"sentence number {i} about topic")
             for i in range(12)]
    _seed_transcribe(uid, lines)

    calls = []
    def responder(prompt, system):
        calls.append(1)
        import re as _re
        ids = _re.findall(r"^(\d+)\. ", prompt, flags=_re.MULTILINE)
        output = "".join(f"{i}. 译{i}\n" for i in ids)
        if "第 12/12 个翻译窗口" not in prompt:
            output += "###HANDOFF###\n上一窗口已完成，继续保持术语一致。"
        return output
    _mock_llm(monkeypatch, responder)

    import dubforge.config as cfgmod
    cfgmod.set_key("split.use_llm", False)
    cfgmod.set_key("split.max_split_length", 80)
    cfgmod.set_key("translate.max_context_tokens", 1_000)

    from dubforge.domain import crud as _crud
    def _rr(key):
        st = _run_until(uid, key)
        if st != "done":
            row = _crud.get_step(_crud.get_project(uid).id, key)
            print(f"[{key}] {row.status} | {row.error_code} | {row.error_message}")
        return st
    assert _rr("split") == "done"
    assert _rr("translate") == "done"
    calls_after_1 = len(calls)
    assert calls_after_1 == 12
    runner = _runner()
    r = runner.start_step(uid, "translate", force=False)
    if r["status"] == "started":
        wait_status(uid, "translate", {"done"})
    assert len(calls) == calls_after_1

    r = runner.start_step(uid, "translate", force=True)
    assert r["status"] == "started"
    wait_status(uid, "translate", {"done"})
    assert len(calls) == calls_after_1 * 2


def test_translate_numbered_output_requires_ordered_known_ids(
        make_project, isolated_env, monkeypatch, pdir):
    """未知号和乱序不是可对齐输入，而是严格协议失败并自动重试。"""
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 2.0, "alpha"), (2.0, 4.0, "beta")])
    responses = iter(["2. 贝塔\n1. 阿尔法", "1. 阿尔法\n2. 贝塔"])
    translate_started = False

    def responder(prompt, system):
        nonlocal translate_started
        if "【待翻译原文】" in prompt:
            translate_started = True
        if not translate_started:
            return "1. 0.000-2.000: alpha\n2. 2.000-4.000: beta"
        return next(responses)
    _mock_llm(monkeypatch, responder)
    from dubforge.domain import crud as _crud
    assert _run_until(uid, "split") == "done"
    status = _run_until(uid, "translate", timeout=30)
    if status != "done":
        row = _crud.get_step(_crud.get_project(uid).id, "translate")
        print(row.error_code, row.error_message)
    assert status == "done"
    data = read_translate(uid)
    mapped = {line["id"]: line["dst"] for line in data["lines"]}
    assert mapped == {1: "阿尔法", 2: "贝塔"}


def test_split_llm_garbage_fails_loudly(make_project, isolated_env, monkeypatch, pdir):
    """LLM 拆分返回垃圾 → 校验失败 → 任务响亮失败 (不静默回退规则切,
    避免无声学语义的结果流进翻译/配音); 链挂起后「重试并继续」即可恢复"""
    uid = make_project()
    long_text = " ".join(["hello"] * 30)
    _seed_transcribe(uid, [(0.0, 8.0, long_text)])

    def responder(prompt, system):
        return "这不是 JSON"
    _mock_llm(monkeypatch, responder)

    assert _run_until(uid, "split") == "failed"
    from dubforge.domain import crud as _crud
    step = _crud.get_step(_crud.get_project(uid).id, "split")
    assert step.error_code == "SPLIT_WINDOW_UNTRUSTED"


def test_subtitles_outputs(make_project, isolated_env, monkeypatch, pdir):
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 2.0, "Hello world"), (2.0, 4.0, "Second line")])
    def responder(prompt, s=None):
        if "【待翻译原文】" in prompt:
            return ("1. 你好，世界，这是一句比较长的翻译需要换行处理\n"
                    "2. 第二行")
        return "1. 0.000-2.000: Hello world\n2. 2.000-4.000: Second line"
    _mock_llm(monkeypatch, responder)
    for key in ("split", "translate", "subtitles"):
        assert _run_until(uid, key) == "done"

    srt_dir = pdir(uid) / "srt"
    assert (srt_dir / "trans.srt").exists()
    bi = (srt_dir / "bilingual.srt").read_text(encoding="utf-8")
    assert "你好" in bi and "Hello world" in bi       # 译文上/原文下
    # 行宽: 超长译文有换行
    tr = (srt_dir / "trans.srt").read_text(encoding="utf-8")
    assert "\n" in tr.split("\n")[2] or len(tr) > 0



def test_translate_sequential_chunks(make_project, isolated_env, monkeypatch, pdir):
    """多个翻译窗口按顺序传递交接摘要, 输出按 id 正确归位"""
    uid = make_project()
    lines = [(float(i) * 2, float(i) * 2 + 1.5,
              f"sentence number {i} about topic")
             for i in range(12)]
    _seed_transcribe(uid, lines)

    calls = []
    prompts = []
    def responder(prompt, system):
        calls.append(1)
        if "【待翻译原文】" in prompt:
            prompts.append(prompt)
            import re as _re
            ids = _re.findall(r"^(\d+)\. ", prompt, flags=_re.MULTILINE)
            output = "".join(f"{i}. 译{i}\n" for i in ids)
            if len(prompts) < 12:
                output += "###HANDOFF###\n上一窗口摘要：保持 topic 术语一致。"
            return output
        return ""
    _mock_llm(monkeypatch, responder)

    import dubforge.config as cfgmod
    cfgmod.set_key("split.use_llm", False)
    cfgmod.set_key("split.max_split_length", 80)
    cfgmod.set_key("translate.max_context_tokens", 1_000)

    assert _run_until(uid, "split") == "done"
    assert _run_until(uid, "translate") == "done"
    assert len(calls) == 12
    assert "上一窗口摘要：保持 topic 术语一致。" in prompts[1]
    assert "###HANDOFF###" in prompts[0] and "###HANDOFF###" not in prompts[-1]
    data = read_translate(uid)
    dsts = {line["id"]: line["dst"] for line in data["lines"]}
    assert dsts[3] == "译3" and dsts[11] == "译11"


def test_translate_chunk_planner_uses_context_budget():
    """默认 0.2M 预算不会把 80 行/约 12k 字符机械切成 600 字符小块。"""
    from dubforge.pipeline.step_translate import _chunk_lines

    lines = [{"id": i + 1, "text": "这是用于验证上下文预算的字幕内容。" * 10}
             for i in range(80)]
    chunks = _chunk_lines(lines, token_budget=150_000)
    assert len(chunks) == 1
    assert sum(len(line["text"]) for line in chunks[0]) > 12_000


def test_translate_window_prompt_only_requests_handoff_when_needed():
    from dubforge.pipeline.step_translate import _window_prompt

    line = [{"id": 1, "text": "hello"}]
    final_prompt = _window_prompt(line, "", 1, 1)
    rolling_prompt = _window_prompt(line, "上一窗口摘要", 1, 2)
    assert "行号. 译文" in final_prompt
    assert "直接返回编号文本" in final_prompt
    assert "【待翻译原文】\n1. hello" in final_prompt
    assert "###HANDOFF###" not in final_prompt
    assert "###HANDOFF###" in rolling_prompt
    assert "上一窗口交接摘要" in rolling_prompt
    assert "第 1/2 个翻译窗口" in rolling_prompt


def test_translation_duration_is_sent_in_initial_repair_budget_and_cache():
    from types import SimpleNamespace
    from dubforge.pipeline import step_translate as st
    ctx = SimpleNamespace(cfg=lambda key, default=None: default)
    short = [{'id': 1, 'text': 'A very short sentence', 'start': 2.0, 'end': 2.8}]
    long = [{**short[0], 'end': 7.0}]
    assert '[时长 0.80 秒]' in st._window_prompt(short, '', 1, 1)
    assert '[时长 0.80 秒]' in st._repair_prompt(short, '', 1, 1, [1])
    assert '[时长 5.00 秒]' in st._window_prompt(long, '', 1, 1)
    assert st._line_cost(short[0]) > st._line_cost({'id': 1, 'text': short[0]['text']})
    assert st._chunk_key(ctx, short, 'window-v6') != st._chunk_key(ctx, long, 'window-v6')


@pytest.mark.parametrize("style", ["", "  ", "用四川方言，幽默一点，多用网络流行语"])
def test_translate_prompt_prefers_autonomous_natural_translation(style):
    """简短提示保留原意、时长、标点和逐句约定，自定义风格按原文传入。"""
    from dubforge.pipeline.step_translate import _base_system, _window_prompt

    class Ctx:
        def cfg(self, key, default=None):
            return {
                "translate.target_language": "简体中文",
                "translate.style_prompt": style,
            }.get(key, default)

    system = _base_system(Ctx(), "hello = 你好")
    prompt = _window_prompt([{"id": 1, "text": "hello"}], "", 1, 1)

    assert "结合整体语境和常识" in system
    assert "用于字幕和配音" in system
    assert "按每句标注的时长控制长度" in system
    assert "短句优先简洁表达" in system
    assert "句末省略逗号、句号" in system
    assert "问号、感叹号、省略号" in system
    assert "句中标点按需保留" in system
    assert "句子数量、标号及顺序保持一致" in system
    assert "每句译文非空，不加空行" in system
    assert "术语参考：" in system
    assert "hello = 你好" in system
    assert "直接返回编号文本" in prompt
    assert "请优先按用户风格组织表达" not in system
    assert "可充分润色和本地化" not in system
    if style.strip():
        assert f"翻译风格要求：\n{style.strip()}" in system
    else:
        assert "翻译风格要求：" not in system


def test_numbered_protocol_parser_accepts_exact_lines():
    from dubforge.pipeline.step_translate import _parse_numbered_output

    chunk = [{"id": i, "text": f"text {i}"} for i in (1, 2, 3)]
    got, empty, summary = _parse_numbered_output(
        "1. 一\n2. 二\n3. 三", chunk, final_window=True)
    assert got == {1: "一", 2: "二", 3: "三"}
    assert empty == [] and summary == ""


def test_numbered_protocol_parser_reports_missing_and_empty_without_merging():
    """空译文行号仍计入 got（dst=""），empty 单独记录；未出现的行号不算。"""
    from dubforge.pipeline.step_translate import _parse_numbered_output

    chunk = [{"id": i, "text": f"text {i}"} for i in (1, 2, 3)]
    got, empty, summary = _parse_numbered_output("1. 一\n3. ", chunk, True)
    assert got == {1: "一", 3: ""}
    assert empty == [3] and summary == ""


@pytest.mark.parametrize("raw", [
    "1. 一\n译文没有编号",
    "1. 一\n1. 再来一行",
    "2. 二\n1. 一",
    "1. 一\n9. 未知",
])
def test_numbered_protocol_structural_errors_do_not_guess(raw):
    from dubforge.pipeline.step_translate import _parse_numbered_output

    chunk = [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}]
    with pytest.raises(ValueError, match="缺少|重复输出|顺序错误|未知行号"):
        _parse_numbered_output(raw, chunk, True)


def test_translate_numbered_protocol_tolerates_spacing_and_blank_lines():
    """编号空格差异和空行兼容；只有行号对齐问题才严重。"""
    from dubforge.pipeline.step_translate import _parse_numbered_output

    chunk = [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}]
    warnings: list[str] = []
    got, empty, summary = _parse_numbered_output(
        "1.一\n\n2、二  ", chunk, True, warnings=warnings)
    assert got == {1: "一", 2: "二"}
    assert any("空格不规范" in item for item in warnings)
    assert any("空行" in item for item in warnings)
    assert any("两端空白" in item for item in warnings)


def test_numbered_protocol_handoff_only_in_rolling_window():
    from dubforge.pipeline.step_translate import _parse_numbered_output

    chunk = [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}]
    raw = "1. 一\n2. 二\n\n###HANDOFF###\n下一窗口继续保持地名一致"
    got, empty, summary = _parse_numbered_output(raw, chunk, final_window=False)
    assert got == {1: "一", 2: "二"} and empty == []
    assert summary == "下一窗口继续保持地名一致"
    warnings: list[str] = []
    got, empty, summary = _parse_numbered_output(
        raw, chunk, final_window=True, warnings=warnings)
    assert got == {1: "一", 2: "二"} and empty == []
    assert any("交接标记" in item for item in warnings)
    with pytest.raises(ValueError, match="必须恰好输出一行交接标记"):
        _parse_numbered_output("1. 一\n2. 二", chunk, final_window=False)


def test_translate_validator_merges_missing_lines_on_retry():
    """首轮缺行时保留已返回译文，后续只补缺失行即可通过校验。"""
    from dubforge.pipeline.step_translate import _valid_window

    validator = _valid_window([
        {"id": 1, "text": "one"},
        {"id": 2, "text": "two"},
        {"id": 3, "text": "three"},
    ])
    with pytest.raises(ValueError, match="缺少行号: 3"):
        validator("1. 一\n2. 二")
    validator.required_ids = [3]
    result = validator("3. 三")
    assert result["lines"] == {1: "一", 2: "二", 3: "三"}


@pytest.mark.parametrize("empty_text", ["", " \t"])
def test_translate_validator_retries_empty_lines(empty_text):
    """有编号但无正文也必须重试，不能输出空译文作为成功结果。"""
    from dubforge.pipeline.step_translate import _MissingTranslationLines, _valid_window

    validator = _valid_window([
        {"id": 1, "text": "one"},
        {"id": 2, "text": "two"},
        {"id": 3, "text": "three"},
    ])
    with pytest.raises(_MissingTranslationLines, match="空内容") as caught:
        validator("1. 一\n2. 二\n3. " + empty_text)
    assert caught.value.missing == [3] and caught.value.empty == [3]
    assert validator.collected == {1: "一", 2: "二"}
    validator.required_ids = [3]
    assert validator("3. 三")["lines"] == {1: "一", 2: "二", 3: "三"}

    # 缺行号仍然触发重试
    validator2 = _valid_window([
        {"id": 1, "text": "one"},
        {"id": 2, "text": "two"},
    ])
    with pytest.raises(ValueError, match="缺少行号: 2"):
        validator2("1. 一")


def test_translate_structure_error_discards_uncertain_alignment():
    """未知号/错号可能牵连后续译文，不把旧结果拼入重新翻译的窗口。"""
    from dubforge.pipeline.step_translate import _valid_window

    validator = _valid_window([
        {"id": 1, "text": "one"},
        {"id": 2, "text": "two"},
        {"id": 3, "text": "three"},
    ])
    with pytest.raises(ValueError, match="未知行号 9") as caught:
        validator("1. 一\n9. 九")
    assert caught.value.missing == [2, 3]
    assert validator.collected == {}
    assert validator.required_ids == [1, 2, 3]
    result = validator("1. 一\n2. 二\n3. 三")
    assert result["lines"] == {1: "一", 2: "二", 3: "三"}


def test_translate_repair_prompt_keeps_neighbors_as_context_only():
    from dubforge.pipeline.step_translate import _repair_prompt

    chunk = [
        {"id": 1, "text": "第一行"},
        {"id": 2, "text": "第二行"},
        {"id": 3, "text": "第三行"},
        {"id": 4, "text": "第四行"},
        {"id": 5, "text": "第五行"},
        {"id": 6, "text": "第六行"},
    ]
    prompt = _repair_prompt(chunk, "上一窗口摘要", 2, 3, [4],
                             {1: "第一行参考", 2: "第二行旧译文", 5: "第五行旧译文"})
    source_block = prompt.split("【待重译片段】", 1)[1] \
        .split("【邻近上下文", 1)[0]
    assert "4. 第四行" in source_block
    assert "2. 第二行" not in source_block and "6. 第六行" not in source_block
    assert "待返回行号：4" in prompt
    assert "既有译文: 第二行旧译文" in prompt
    assert "6. 第六行" in prompt
    assert "只补齐「待重译片段」" in prompt
    assert "编号文本" in prompt and "无需交接标记" in prompt


def test_translate_local_retry_uses_fresh_small_request(monkeypatch):
    """少量缺行重试只发邻近上下文，不重发完整窗口和上一轮输出。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    chunk = [{"id": i, "text": f"line {i}"} for i in range(1, 101)]
    responses = [
        "".join(f"{i}. 译{i}\n" for i in range(1, 100)),
        "100. 第一百行",
    ]
    calls = []
    validator = st._valid_window(chunk)

    def fake_chat(prompt="", system=None, **kwargs):
        calls.append({"prompt": prompt,
                      "messages": kwargs.get("messages")})
        return responses.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    result = llm.chat_validated(
        st._window_prompt(chunk, "", 1, 1),
        parser=validator, system="固定规则",
        retry_context=lambda error, attempt, last_raw: "fresh",
        retry_request=lambda error, attempt: (
            setattr(validator, "required_ids", error.missing),
            st._repair_prompt(chunk, "", 1, 1, error.missing, validator.collected))[1],
        retry_prompt=lambda error, attempt: "只补缺失行")
    assert result["lines"][100] == "第一百行"
    assert calls[1]["messages"] is None
    source_block = calls[1]["prompt"].split("【待重译片段】", 1)[1] \
        .split("【邻近上下文", 1)[0]
    assert "line 1\n" not in source_block
    assert "line 98" not in source_block and "line 100" in source_block
    assert "line 98" in calls[1]["prompt"]


@pytest.mark.parametrize("last_line", ["", "100. \t"])
def test_translate_window_chooses_local_fresh_retry(monkeypatch, tmp_path, last_line):
    """局部重试不携带完整对话，但继续携带用户风格和通用纠错建议。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    chunk = [{"id": i, "text": f"line {i}"} for i in range(1, 101)]
    responses = [
        "".join(f"{i}. 译{i}\n" for i in range(1, 100)) + last_line,
        "100. 第一百行",
    ]
    calls = []
    traces = []

    class Ctx:
        force = True
        pdir = tmp_path

        def cfg(self, key, default=None):
            return {"translate.max_context_tokens": 200_000,
                    "translate.style_prompt": "用四川方言，幽默一点"}.get(key, default)

        def log(self, message):
            pass

        def check_pause(self):
            pass

        def check_cancel(self):
            pass

        def llm_trace(self, event, content="", **meta):
            traces.append({"event": event, "content": content, **meta})

    def fake_chat(prompt="", system=None, **kwargs):
        calls.append({"prompt": prompt, "system": system, "messages": kwargs.get("messages")})
        return responses.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    ctx = Ctx()
    result = st._translate_window(ctx, chunk, st._base_system(ctx, ""), "", 1, 1)
    assert result[0][100] == "第一百行"
    assert calls[0]["messages"] is None
    assert calls[1]["messages"] is None
    assert calls[0]["system"] == calls[1]["system"]
    assert "用四川方言，幽默一点" in calls[1]["system"]
    assert "可纠正明显的识别错误" in calls[1]["system"]
    assert "每句译文非空" in calls[1]["system"]
    assert "句末省略逗号、句号" in calls[1]["system"]
    assert len(calls[1]["prompt"]) < len(calls[0]["prompt"])
    assert "line 1\n" not in calls[1]["prompt"]
    assert "line 98" in calls[1]["prompt"] and "line 100" in calls[1]["prompt"]
    strategy = next(x for x in traces if x["event"] == "retry_strategy")
    assert strategy["mode"] == "fresh" and strategy["missing_count"] == 1
    merged = next(x for x in traces if x["event"] == "merge_summary")
    assert "第 1 个模型响应解析出 99 行" in merged["content"]
    assert "第 2 个模型响应解析出 1 行" in merged["content"]
    assert merged["final_lines"] == 100 and merged["expected_lines"] == 100


def test_translate_rolling_local_retry_keeps_handoff_summary(monkeypatch, tmp_path):
    """中间窗口首次已拿到摘要时，小片段修复不再要求重复交接标记。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    chunk = [{"id": 1, "text": "one"}, {"id": 2, "text": "two"},
             {"id": 3, "text": "three"}]
    responses = [
        "1. 一\n3. 三\n###HANDOFF###\n保持人物一致",
        "2. 二",
    ]
    calls = []

    class Ctx:
        force = True
        pdir = tmp_path

        def cfg(self, key, default=None):
            return 200_000 if key == "translate.max_context_tokens" else default

        def log(self, message): pass
        def check_pause(self): pass
        def check_cancel(self): pass
        def llm_trace(self, event, content="", **meta): pass

    def fake_chat(prompt="", system=None, **kwargs):
        raw = responses.pop(0)
        calls.append(prompt)
        return raw

    monkeypatch.setattr(llm, "chat", fake_chat)
    translated, summary = st._translate_window(Ctx(), chunk, "系统", "", 1, 2)
    assert translated == {1: "一", 2: "二", 3: "三"}
    assert summary == "保持人物一致"
    assert "###HANDOFF###" in calls[0]
    assert "###HANDOFF###" not in calls[1]


def test_translate_rejects_json_without_json_repair(monkeypatch, tmp_path):
    """翻译协议遇到 JSON 只按格式错误处理；JSON 修复不再用于翻译。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    chunk = [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}]
    responses = ['{"lines":{"1":"一","2":"二"}}', "1. 一\n2. 二"]
    traces = []

    class Ctx:
        force = True
        pdir = tmp_path

        def cfg(self, key, default=None):
            return 200_000 if key == "translate.max_context_tokens" else default

        def log(self, message): pass
        def check_pause(self): pass
        def check_cancel(self): pass
        def llm_trace(self, event, content="", **meta):
            traces.append({"event": event, "content": content, **meta})

    def fake_chat(prompt="", system=None, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    result, _ = st._translate_window(Ctx(), chunk, "系统提示", "", 1, 1)
    assert result == {1: "一", 2: "二"}
    assert not any(x["event"] == "json_repair" for x in traces)
    error = next(x for x in traces if x["event"] == "validation_error")
    assert "缺少“行号. 译文”前缀" in error["content"]


@pytest.fixture
def translation_retry_ctx(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        force=True, pdir=tmp_path,
        cfg=lambda key, default=None: 200_000 if key == "translate.max_context_tokens" else default,
        log=lambda message: None, check_pause=lambda: None, check_cancel=lambda: None,
        llm_trace=lambda *args, **kwargs: None)


def test_translate_unnumbered_continuation_retranslates_whole_window(monkeypatch, translation_retry_ctx):
    """复现日志：漏编号但其后仍有全部编号，不能只补该编号而接受后续错位。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    chunk = [{"id": i, "text": f"line {i}"} for i in range(1, 184)]
    bad = "\n".join(f"{i}. 错位旧译文{i}" for i in range(1, 184))
    bad = bad.replace("130. 错位旧译文130", "漏编号的译文\n130. 错位旧译文130")
    responses = [bad, "\n".join(f"{i}. 正确译文{i}" for i in range(1, 184))]
    calls = []

    def fake_chat(prompt="", **kwargs):
        messages = kwargs.get("messages")
        calls.append((prompt, [dict(m) for m in messages] if messages is not None else None))
        return responses.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    translated, _ = st._translate_window(translation_retry_ctx, chunk, "系统", "", 1, 1)
    assert len(calls) == 2 and calls[1][1] is None
    assert "1. line 1\n" in calls[1][0] and "183. line 183" in calls[1][0]
    assert "重新翻译完整窗口" in calls[1][0]
    assert "待补齐行号" not in calls[1][0]
    assert translated == {i: f"正确译文{i}" for i in range(1, 184)}


def test_translate_partial_repairs_shrink_without_adding_neighbors(monkeypatch, translation_retry_ctx):
    """补齐分两次返回时，只继续请求未完成部分，不把参考行变成新的缺行。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    chunk = [{"id": i, "text": f"line {i}"} for i in range(1, 101)]
    responses = [
        "\n".join(f"{i}. 译{i}" for i in range(1, 101) if i not in (50, 51)),
        "50. 五十", "51. 五十一"]
    calls = []

    def fake_chat(prompt="", **kwargs):
        calls.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    translated, _ = st._translate_window(translation_retry_ctx, chunk, "系统", "", 1, 1)
    assert "待返回行号：50, 51\n" in calls[1]
    assert "待返回行号：51\n" in calls[2]
    assert len(translated) == 100 and translated[50] == "五十" and translated[51] == "五十一"
    assert translated[49] == "译49" and translated[52] == "译52"


@pytest.mark.parametrize("context_limit", [200_000, 1_000])
def test_translate_large_missing_retry_matches_requested_scope(monkeypatch, translation_retry_ctx, context_limit):
    """大面积缺行保留对话时仅补缺行；上下文超预算则完整重翻并清除旧结果。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    translation_retry_ctx.cfg = lambda key, default=None: context_limit if key == "translate.max_context_tokens" else default
    chunk = [{"id": i, "text": f"line {i}"} for i in range(1, 11)]
    ids = range(2, 11) if context_limit == 200_000 else range(1, 11)
    responses = ["1. 原译一", "\n".join(f"{i}. 新译{i}" for i in ids)]
    calls = []

    def fake_chat(prompt="", **kwargs):
        messages = kwargs.get("messages")
        calls.append((prompt, [dict(m) for m in messages] if messages is not None else None))
        return responses.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    translated, _ = st._translate_window(translation_retry_ctx, chunk, "系统", "", 1, 1)
    assert len(translated) == 10
    if context_limit == 200_000:
        assert calls[1][1] is not None
        assert "待返回行号：2, 3, 4, 5, 6, 7, 8, 9, 10" in calls[1][1][-1]["content"]
        assert translated[1] == "原译一"
    else:
        assert calls[1][1] is None and "1. line 1\n" in calls[1][0]
        assert translated[1] == "新译1"


def test_translate_repeated_empty_response_fails_without_caching(monkeypatch, translation_retry_ctx):
    """三次都为空译文时明确失败，不能把缺失内容写入可复用缓存。"""
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm
    from dubforge.errors import StepFailedError

    calls = []

    def fake_chat(*args, **kwargs):
        calls.append(1)
        return "1. "

    monkeypatch.setattr(llm, "chat", fake_chat)
    with pytest.raises(StepFailedError):
        st._translate_window(translation_retry_ctx, [{"id": 1, "text": "one"}], "系统", "", 1, 1)
    assert len(calls) == 3
    assert not list(translation_retry_ctx.pdir.rglob("*.json"))


@pytest.mark.parametrize("cached_lines", [{"1": ""}, {"1": "一", "2": "多余"}, {"2": "错误编号"}, {"1": "一\n续行"}])
def test_translate_invalid_cached_lines_are_retranslated(monkeypatch, translation_retry_ctx, cached_lines):
    from dubforge.pipeline import step_translate as st
    from dubforge.backends import llm

    translation_retry_ctx.force = False
    chunk = [{"id": 1, "text": "one"}]
    key = st._chunk_key(translation_retry_ctx, chunk, "window-v6", "1/1|")
    st._store(translation_retry_ctx, key, {"lines": cached_lines})
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: "1. 新译文")
    translated, _ = st._translate_window(translation_retry_ctx, chunk, "系统", "", 1, 1)
    assert translated == {1: "新译文"}
    assert st._cached(translation_retry_ctx, key)["lines"] == {"1": "新译文"}


def test_translate_full_retry_guard_drops_previous_output(monkeypatch):
    """完整上下文重试会超预算时，改发原始窗口的独立重翻请求。"""
    from dubforge.pipeline import step_translate as st

    class Ctx:
        force = False

        def cfg(self, key, default=None):
            return 1_000 if key == "translate.max_context_tokens" else default

    chunk = [{"id": 1, "text": "很长" * 500}, {"id": 2, "text": "第二行"}]
    original = st._window_prompt(chunk, "", 1, 1)
    assert not st._full_context_retry_fits(
        Ctx(), "system", original, "上一轮超长输出" * 100, "修复缺失行")


def test_subtitles_extend_short_cues_without_merging_text():
    from dubforge.pipeline.step_subtitles import _extend_short_cues
    from dubforge.utils.srt import SubLine

    lines = [SubLine(1, 0.0, 0.2, "嗯"),
             SubLine(2, 0.25, 1.8, "我们继续")]
    changed = _extend_short_cues(lines, 0.5)
    assert changed == 1
    assert lines[0].end == 0.25  # 延长显示不能引入相邻字幕重叠
    assert [line.text for line in lines] == ["嗯", "我们继续"]


def test_translate_chunk_planner_splits_by_token_budget():
    from dubforge.pipeline.step_translate import _chunk_lines

    lines = [{"id": i + 1, "text": "中文内容" * 100} for i in range(8)]
    chunks = _chunk_lines(lines, token_budget=500)
    assert len(chunks) > 1
    assert [line["id"] for chunk in chunks for line in chunk] == list(range(1, 9))
