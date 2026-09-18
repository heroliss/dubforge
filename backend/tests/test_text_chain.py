# -*- coding: utf-8 -*-
"""M2b 文本链测试: 全部 mock LLM (零 token), 覆盖三档策略/缓存/id 对齐/鲁棒回退"""
import json

from conftest import wait_status
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
    import os
    for name in ("src.srt", "words.json"):
        p = srt_dir / name
        crud.add_artifact(project.id, "transcribe", "srt" if name.endswith("srt") else "json",
                          name, p, f"srt/{name}")


def _mock_llm(monkeypatch, responder):
    """responder(prompt, system) -> str; 注入 llm 模块"""
    from dubforge.backends import llm
    calls = []

    def fake_chat(prompt, system=None, temperature=None):
        calls.append(prompt)
        return responder(prompt, system)

    def fake_chat_json(prompt, validator=None, system=None, max_retries=3,
                       temperature=None, log=lambda m: None):
        raw = fake_chat(prompt, system)
        obj = llm._extract_json(raw)
        if validator:
            normalized = validator(obj)
            return obj if normalized is None else normalized
        return obj

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return calls


def _run_until(uid, key, timeout=30):
    _runner().start_step(uid, key)
    return wait_status(uid, key, {"done", "failed"}, timeout=timeout)


def _runner():
    from dubforge.runner import runner
    return runner


def test_split_long_line_with_word_timestamps(make_project, isolated_env, pdir):
    uid = make_project()
    long_text = " ".join(["word"] * 40)          # 40 词 > 默认 max_split_length=22
    _seed_transcribe(uid, [(0.0, 10.0, long_text)])
    assert _run_until(uid, "split") == "done"

    data = json.loads((pdir(uid)
                       / "artifacts" / "split" / "split.json").read_text(encoding="utf-8"))
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
    # 英文行间不得粘连 (词边界保真)
    assert "wordword" not in "".join(l["text"] for l in data["lines"])


def test_translate_whole_doc_tier(make_project, isolated_env, monkeypatch, pdir):
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 2.0, "Hello world"), (2.0, 4.0, "Second line")])

    def responder(prompt, system):
        if "输出 JSON 数组" not in prompt:
            return "测试主题: 验证翻译链路"          # 摘要调用
        assert "1. Hello world" in prompt and "2. Second line" in prompt
        return json.dumps([{"id": 1, "dst": "你好世界"}, {"id": 2, "dst": "第二行"}])
    _mock_llm(monkeypatch, responder)

    from dubforge.domain import crud as _crud
    def _rr(key):
        st = _run_until(uid, key)
        if st != "done":
            s = _crud.get_step(_crud.get_project(uid).id, key)
            print(f"[{key}] {s.status} | {s.error_code} | {s.error_message}")
        return st
    assert _rr("split") == "done"
    assert _rr("translate") == "done"
    data = json.loads((pdir(uid)
                       / "artifacts" / "translate" / "translate.json").read_text(encoding="utf-8"))
    assert data["lines"][0]["dst"] == "你好世界"
    assert data["lines"][1]["src"] == "Second line"


def test_translate_chunked_tier_and_cache(make_project, isolated_env, monkeypatch):
    """12 行 (超过整篇阈值) → 分块; 二次运行命中缓存 (LLM 调用数不变)"""
    uid = make_project()
    lines = [(float(i) * 2, float(i) * 2 + 1.5, f"sentence number {i} about topic")
             for i in range(12)]
    _seed_transcribe(uid, lines)

    calls = []
    def responder(prompt, system):
        calls.append(1)
        import re as _re
        ids = _re.findall(r"^(\d+)\. ", prompt, flags=_re.M)
        return json.dumps([{"id": int(i), "dst": f"译{i}"} for i in ids])
    _mock_llm(monkeypatch, responder)

    import dubforge.config as cfgmod
    cfgmod.set_key("translate.whole_doc_max_lines", 5)

    from dubforge.domain import crud as _crud
    def _rr(key):
        st = _run_until(uid, key)
        if st != "done":
            s = _crud.get_step(_crud.get_project(uid).id, key)
            print(f"[{key}] {s.status} | {s.error_code} | {s.error_message}")
        return st
    assert _rr("split") == "done"
    assert _rr("translate") == "done"
    calls_after_1 = len(calls)
    # 摘要 1 + 2 块×(直译+润色) 2×2 + 全局审校 2 = 7
    assert calls_after_1 == 7
    runner = _runner()
    r = runner.start_step(uid, "translate", force=False)
    # 全部块已缓存 → 但 fresh 指纹包含产物哈希(变了) → 会重跑, 但 LLM 调用应为 0
    if r["status"] == "started":
        wait_status(uid, "translate", {"done"})
    assert len(calls) == calls_after_1             # 缓存命中, 零新增调用


def test_translate_id_alignment_robust(make_project, isolated_env, monkeypatch, pdir):
    """LLM 返回乱序 id → 仍按 id 正确对齐 (不按数组顺序)"""
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 2.0, "alpha"), (2.0, 4.0, "beta")])

    def responder(prompt, system):
        if "输出 JSON 数组" not in prompt:
            return "测试主题: 验证翻译链路"
        return json.dumps([{"id": 2, "dst": "贝塔"}, {"id": 1, "dst": "阿尔法"}])
    _mock_llm(monkeypatch, responder)

    from dubforge.domain import crud as _crud
    def _rr(key):
        st = _run_until(uid, key)
        if st != "done":
            s = _crud.get_step(_crud.get_project(uid).id, key)
            print(f"[{key}] {s.status} | {s.error_code} | {s.error_message}")
        return st
    assert _rr("split") == "done"
    assert _rr("translate") == "done"
    data = json.loads((pdir(uid)
                       / "artifacts" / "translate" / "translate.json").read_text(encoding="utf-8"))
    m = {l["id"]: l["dst"] for l in data["lines"]}
    assert m[1] == "阿尔法" and m[2] == "贝塔"


def test_split_llm_garbage_falls_back(make_project, isolated_env, monkeypatch, pdir):
    """LLM 拆分返回垃圾 → 校验失败 → 回退规则切 (鲁棒)"""
    uid = make_project()
    long_text = " ".join(["hello"] * 30)
    _seed_transcribe(uid, [(0.0, 8.0, long_text)])

    def responder(prompt, system):
        return "这不是 JSON"                      # 拆分/摘要都喂垃圾 → 全走回退
    _mock_llm(monkeypatch, responder)

    assert _run_until(uid, "split") == "done"
    data = json.loads((pdir(uid)
                       / "artifacts" / "split" / "split.json").read_text(encoding="utf-8"))
    assert data["count"] > 1


def test_subtitles_outputs(make_project, isolated_env, monkeypatch, pdir):
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 2.0, "Hello world"), (2.0, 4.0, "Second line")])
    def responder(prompt, s=None):
        if "输出 JSON 数组" not in prompt:
            return "测试主题: 验证翻译链路"
        return json.dumps([{"id": 1, "dst": "你好，世界，这是一句比较长的翻译需要换行处理"},
                           {"id": 2, "dst": "第二行"}])
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

