# -*- coding: utf-8 -*-
"""
步骤 5: translate —— 翻译 (三档策略, ADR 设计自 VideoLingo-Workspace ADR-0002)
==============================================================================
输入: split/split.json
全局上下文: 主题摘要 (滚动扩容) + 术语表 (custom_terms xlsx) + 风格提示词
三档:
  1. 全文 ≤ whole_doc_max_lines 行 → 整篇直译 (全局一致性最佳)
  2. 分块两步直译 (faithfulness→expressiveness) + 全局审校第三步
     (上下文是全文直译稿, 输出只有本块 —— 全局性与抗截断兼得)
  3. 摘要滚动扩容: 全文超 summary_length 时分段汇总

续传缓存: cache/translate/<块指纹>.json —— 每块结果落盘, 重跑免费跳过已译块
对齐防御: LLM 输出必须带行号 id, 按 id 对齐而非数组顺序 (根治 JSON 错位)
产物: translate.json {target_language, lines: [{id, src, dst}]}
"""
import hashlib
import json
import re

import pandas as pd

from dubforge.backends import llm
from dubforge.errors import StepFailedError

CHUNK_CHARS = 600
CHUNK_MAX_LINES = 10


# ==================== 上下文构建 ====================

def _load_terms(ctx) -> str:
    """术语表 xlsx: 列 src/dst/备注(可选); 空配置返回空串"""
    path = ctx.cfg("translate.custom_terms_path", "")
    if not path:
        return ""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        ctx.log(f"术语表不存在, 忽略: {path}")
        return ""
    df = pd.read_excel(p)
    rows = []
    for _, r in df.iterrows():
        s, d = str(r.get("src", "")).strip(), str(r.get("dst", "")).strip()
        if s and d and s != "nan" and d != "nan":
            note = str(r.get("note", "")).strip()
            rows.append(f"- {s} → {d}" + (f" (备注: {note})" if note and note != "nan" else ""))
    return "\n".join(rows)


def _summarize(ctx, full_text: str) -> str:
    """主题摘要; 超长时滚动分段汇总 (档位三)"""
    limit = int(ctx.cfg("translate.summary_length", 8000))
    if len(full_text) <= limit:
        text = full_text
    else:
        ctx.log(f"全文 {len(full_text)} 字符超摘要上限, 滚动分段汇总")
        part_sums = []
        for i in range(0, len(full_text), limit):
            part = full_text[i:i + limit]
            part_sums.append(llm.chat(
                f"用 3~5 句话概括以下视频片段的话题与要点 (用目标语言):\n\n{part}"))
        text = "\n".join(part_sums)
    ctx.log("生成全文主题摘要 (LLM)...")
    t0 = __import__("time").time()
    out = llm.chat(
        f"以下是一部视频的完整文字稿。请输出: 1) 一句话主题; 2) 3~5 句剧情/论述要点; "
        f"3) 出现的关键人名/专有名词列表。使用目标语言「{ctx.cfg('translate.target_language', '简体中文')}」。\n\n"
        f"{text[:limit]}")
    ctx.log(f"摘要完成 ({__import__('time').time()-t0:.1f}s)")
    return out


def _terms_block(terms: str, sentence: str = "") -> str:
    block = ""
    if terms:
        block += f"\n【术语表】(必须按此翻译)\n{terms}\n"
    if sentence:
        hits = [ln for ln in terms.splitlines() if ln.startswith("- ") and
                ln[2:].split("→")[0].strip().lower() in sentence.lower()]
        if hits:
            block += f"\n【本块命中术语】\n" + "\n".join(hits) + "\n"
    return block


# ==================== 分块 ====================

def _chunk_lines(lines: list[dict]) -> list[list[dict]]:
    chunks, buf, chars = [], [], 0
    for ln in lines:
        if buf and (chars + len(ln["text"]) > CHUNK_CHARS or len(buf) >= CHUNK_MAX_LINES):
            chunks.append(buf)
            buf, chars = [], 0
        buf.append(ln)
        chars += len(ln["text"])
    if buf:
        chunks.append(buf)
    return chunks


def _chunk_key(ctx, chunk: list[dict], stage: str, extra: str = "") -> str:
    """块缓存指纹: 内容 + 相关配置 + 阶段 (内容或配置变 → 缓存自然失效)"""
    payload = json.dumps({
        "stage": stage, "extra": extra,
        "lines": [[l["id"], l["text"]] for l in chunk],
        "target": ctx.cfg("translate.target_language"),
        "style": ctx.cfg("translate.style_prompt", ""),
        "model": ctx.cfg("llm.model"),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _cache_dir(ctx):
    from pathlib import Path
    d = ctx.pdir / "cache" / "translate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cached(ctx, key: str):
    p = _cache_dir(ctx) / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _store(ctx, key: str, data) -> None:
    p = _cache_dir(ctx) / f"{key}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    import os
    os.replace(tmp, p)


# ==================== 提示词 ====================

def _base_system(ctx, summary: str, terms: str) -> str:
    style = ctx.cfg("translate.style_prompt", "")
    sys = (f"你是专业的视频字幕翻译, 把输入翻译成「{ctx.cfg('translate.target_language', '简体中文')}」。\n"
           f"要求: 口语化、符合中文表达习惯、不逐词直译; 保留语气; 一行对一行。")
    if style:
        sys += f"\n【风格要求】{style}"
    if summary:
        sys += f"\n【全文主题背景】\n{summary}"
    if terms:
        sys += f"\n【术语表】(必须按此翻译)\n{terms}"
    return sys


def _faith_prompt(chunk: list[dict], prev_tail: str, next_head: str) -> str:
    ctx_block = ""
    if prev_tail:
        ctx_block += f"\n【上一块结尾, 仅作衔接参考, 不要翻译】\n{prev_tail}"
    if next_head:
        ctx_block += f"\n【下一块开头, 仅作衔接参考, 不要翻译】\n{next_head}"
    body = "\n".join(f"{l['id']}. {l['text']}" for l in chunk)
    return (f"请逐行直译以下字幕 (忠实原意, 先不管通顺)。{ctx_block}\n"
            f"输出 JSON 数组, 每个元素 {{\"id\": 行号, \"dst\": 译文}}, id 必须与输入行号一一对应。\n\n"
            f"{body}")


def _express_prompt(chunk: list[dict], faith_map: dict[int, str]) -> str:
    body = "\n".join(f"{l['id']}. 原文: {l['text']}\n   直译: {faith_map[l['id']]}"
                     for l in chunk)
    return (f"以下是字幕的直译稿, 请逐行润色成通顺自然的中文口语, 保持与原文行一一对应, 不增删行。\n"
            f"输出 JSON 数组, 每个元素 {{\"id\": 行号, \"dst\": 润色后译文}}。\n\n{body}")


def _polish_prompt(chunk: list[dict], whole_draft: str) -> str:
    body = "\n".join(f"{l['id']}. {l['text']}" for l in chunk)
    return (f"【全文直译稿】(供参考上下文, 不要整体重译)\n{whole_draft}\n\n"
            f"【当前块】请结合全文语境, 把下列行润色为通顺自然的中文口语 (代词/语气/衔接词按全文语境调整)。\n"
            f"输出 JSON 数组, 每个元素 {{\"id\": 行号, \"dst\": 译文}}, id 一一对应。\n\n{body}")


# ==================== 单块执行 (两步) ====================

def _valid_ids(chunk: list[dict]):
    want = [l["id"] for l in chunk]

    def valid(obj):
        if not isinstance(obj, list):
            raise ValueError("不是数组")
        got = {}
        for item in obj:
            if not isinstance(item, dict) or "id" not in item or "dst" not in item:
                raise ValueError("元素缺少 id/dst 字段")
            got[int(item["id"])] = str(item["dst"]).strip()
        missing = [i for i in want if i not in got or not got[i]]
        if missing:
            raise ValueError(f"缺少行: {missing[:5]}")
        return {i: got[i] for i in want}
    return valid


def _translate_chunk_two_step(ctx, chunk, system, prev_tail, next_head) -> dict[int, str]:
    key = _chunk_key(ctx, chunk, "two_step", prev_tail + "|" + next_head)
    hit = _cached(ctx, key)
    if hit:
        ctx.log(f"块 {chunk[0]['id']}~{chunk[-1]['id']} 命中缓存, 跳过")
        return {int(k): v for k, v in hit.items()}

    # 第一步: 直译
    faith = llm.chat_json(_faith_prompt(chunk, prev_tail, next_head),
                          validator=_valid_ids(chunk), system=system, log=ctx.log)
    # 第二步: 润色
    express = llm.chat_json(_express_prompt(chunk, faith),
                            validator=_valid_ids(chunk), system=system, log=ctx.log)
    _store(ctx, key, express)
    return express


def _translate_chunk_polish(ctx, chunk, system, whole_draft) -> dict[int, str]:
    key = _chunk_key(ctx, chunk, "polish", whole_draft[:200])
    hit = _cached(ctx, key)
    if hit:
        return {int(k): v for k, v in hit.items()}
    result = llm.chat_json(_polish_prompt(chunk, whole_draft),
                           validator=_valid_ids(chunk), system=system, log=ctx.log)
    _store(ctx, key, result)
    return result


# ==================== 主流程 ====================

def run(ctx) -> None:
    split_path = ctx.upstream("split", "split.json")
    lines: list[dict] = json.loads(
        split_path.read_text(encoding="utf-8"))["lines"]
    if not lines:
        raise StepFailedError("分句结果为空, 无法翻译")

    target = ctx.cfg("translate.target_language", "简体中文")
    full_text = "\n".join(l["text"] for l in lines)
    ctx.log(f"待翻译 {len(lines)} 行 / {len(full_text)} 字符, 目标语言: {target}")

    summary = _summarize(ctx, full_text)
    terms = _load_terms(ctx)
    system = _base_system(ctx, summary, terms)
    ctx.check_cancel()

    whole_max = int(ctx.cfg("translate.whole_doc_max_lines", 60))
    results: dict[int, str] = {}

    if len(lines) <= whole_max:
        # ---- 档位一: 整篇直译 ----
        ctx.log(f"行数 ≤ {whole_max}, 走整篇直译")
        key = _chunk_key(ctx, lines, "whole_doc")
        hit = _cached(ctx, key)
        if hit:
            results = {int(k): v for k, v in hit.items()}
            ctx.log("整篇翻译命中缓存")
        else:
            ctx.log("整篇直译 (LLM)...")
            t1 = __import__("time").time()
            results = llm.chat_json(
                _faith_prompt(lines, "", ""),
                validator=_valid_ids(lines), system=system, log=ctx.log)
            ctx.report_progress(90)
            ctx.log(f"整篇直译完成 ({__import__('time').time()-t1:.1f}s)")
            _store(ctx, key, results)
    else:
        # ---- 档位二: 分块两步 + 全局审校 ----
        chunks = _chunk_lines(lines)
        ctx.log(f"分块翻译: {len(chunks)} 块 (global_polish={ctx.cfg('translate.global_polish', True)})")
        for ci, chunk in enumerate(chunks):
            ctx.check_cancel()
            prev_tail = chunks[ci - 1][-1]["text"] if ci > 0 else ""
            next_head = chunks[ci + 1][0]["text"] if ci < len(chunks) - 1 else ""
            results.update(_translate_chunk_two_step(
                ctx, chunk, system, prev_tail, next_head))
            ctx.report_progress((ci + 1) / len(chunks) * 60)
            ctx.log(f"块 {ci + 1}/{len(chunks)} 完成")

        if ctx.cfg("translate.global_polish", True):
            whole_draft = "\n".join(f"{l['id']}. {results[l['id']]}" for l in lines)
            for ci, chunk in enumerate(chunks):
                ctx.check_cancel()
                results.update(_translate_chunk_polish(ctx, chunk, system, whole_draft))
                ctx.report_progress(60 + (ci + 1) / len(chunks) * 35)
                ctx.log(f"全局审校 {ci + 1}/{len(chunks)} 完成")

    out = [{"id": l["id"], "src": l["text"], "dst": results[l["id"]]}
           for l in lines]
    ctx.write_json("translate.json", {"target_language": target, "lines": out})
    ctx.log(f"✅ 翻译完成: {len(out)} 行 → {target}")
