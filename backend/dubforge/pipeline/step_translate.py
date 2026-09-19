# -*- coding: utf-8 -*-
"""
步骤 5: translate —— 翻译
==========================
输入: split/split.json
全局上下文: 主题摘要 (滚动扩容) + 术语表 (custom_terms xlsx) + 风格提示词
三档:
  1. 全文 ≤ whole_doc_max_lines 行 → 整篇直译 (全局一致性最佳)
  2. 分块直译 (默认单步一次成稿; two_step=先忠实再润色) + 全局审校第三步
     (审校输入=全文译稿, 输出只有本块 —— 全局性与抗截断兼得)
  3. 主题摘要: 全文超 summary_length 时按行边界切段滚动汇总 (递归合并不截断)

并行: 分块与审校均按 llm.max_workers 并发执行 (线程池; 取消即停派发剩余块)
续传缓存: cache/translate/<块指纹>.json —— 指纹含目标语言/风格/模式/模型, 改配置自动失效
对齐防御: LLM 输出必须带行号 id, 按 id 对齐而非数组顺序 (根治 JSON 错位)
产物: translate.json {target_language, lines: [{id, src, dst}]}
"""
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from dubforge.backends import llm
from dubforge.utils.fs import atomic_replace
from dubforge.errors import StepFailedError

CHUNK_CHARS = 600
CHUNK_MAX_LINES = 10
CONTEXT_LINES = 3             # 每块携带的前后文行数 (跨块指代/语气衔接)


# ==================== 上下文构建 ====================
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
    """主题摘要; 超长时按行边界切段滚动汇总 (不把句子拦腰切, 也不静默截断)"""
    limit = int(ctx.cfg("translate.summary_length", 8000))
    if len(full_text) <= limit:
        return _summary_call(ctx, full_text)
    merged = _rolling_reduce(ctx, full_text, limit,
                             "用 3~5 句话概括以下视频片段的话题与要点 (用目标语言):")
    ctx.log("生成全文主题摘要 (LLM)...")
    t0 = __import__("time").time()
    out = _summary_call(ctx, merged)
    ctx.log(f"摘要完成 ({__import__('time').time()-t0:.1f}s)")
    return out


def _summary_call(ctx, text: str) -> str:
    return llm.chat(
        f"以下是一部视频的完整文字稿。请输出: 1) 一句话主题; 2) 3~5 句剧情/论述要点; "
        f"3) 出现的关键人名/专有名词列表。使用目标语言「{ctx.cfg('translate.target_language', '简体中文')}」。\n\n"
        f"{text}")


def _rolling_reduce(ctx, text: str, limit: int, instruction: str) -> str:
    """按行边界切成 ≤limit 的段, 各段概括; 拼接仍超限则递归逐层合并 (每层约缩小一个量级)"""
    segs, buf, n = [], [], 0
    for ln in text.split("\n"):
        if buf and n + len(ln) + 1 > limit:
            segs.append("\n".join(buf))
            buf, n = [], 0
        buf.append(ln)
        n += len(ln) + 1
    if buf:
        segs.append("\n".join(buf))
    ctx.log(f"内容 {len(text)} 字符超摘要上限, 分 {len(segs)} 段滚动汇总")
    sums = [llm.chat(f"{instruction}\n\n{seg}") for seg in segs]
    merged = "\n".join(sums)
    if len(merged) <= limit:
        return merged
    return _rolling_reduce(ctx, merged, limit,
                           "把以下几段视频要点合并成更精简的要点 (用目标语言):")


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


def _chunk_context(chunks: list[list[dict]], ci: int) -> tuple[str, str]:
    """每块携带的前后文 (CONTEXT_LINES 行), 供跨块指代/语气衔接"""
    prev = "\n".join(l["text"] for l in chunks[ci - 1][-CONTEXT_LINES:]) if ci > 0 else ""
    nxt = "\n".join(l["text"] for l in chunks[ci + 1][:CONTEXT_LINES:]) if ci < len(chunks) - 1 else ""
    return prev, nxt


# ==================== 缓存 ====================
def _chunk_key(ctx, chunk: list[dict], stage: str, extra: str = "") -> str:
    """块缓存指纹: 内容 + 相关配置 + 阶段 (内容或配置变 → 缓存自然失效)"""
    payload = json.dumps({
        "stage": stage, "extra": extra,
        "lines": [[l["id"], l["text"]] for l in chunk],
        "target": ctx.cfg("translate.target_language"),
        "style": ctx.cfg("translate.style_prompt", ""),
        "two_step": bool(ctx.cfg("translate.two_step", False)),
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
    atomic_replace(tmp, p)


# ==================== 提示词 ====================
def _single_prompt(chunk: list[dict], prev_tail: str, next_head: str) -> str:
    ctx_block = ""
    if prev_tail:
        ctx_block += f"\n【上一块结尾, 仅作衔接参考, 不要翻译】\n{prev_tail}"
    if next_head:
        ctx_block += f"\n【下一块开头, 仅作衔接参考, 不要翻译】\n{next_head}"
    body = "\n".join(f"{l['id']}. {l['text']}" for l in chunk)
    return (f"请逐行翻译以下字幕 (口语化、自然通顺、忠实原意, 符合目标语言表达习惯)。{ctx_block}\n"
            f"输出 JSON 数组, 每个元素 {{\"id\": 行号, \"dst\": 译文}}, id 必须与输入行号一一对应。\n\n"
            f"{body}")


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


# ==================== 单块执行 ====================
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


def _translate_chunk(ctx, chunk: list[dict], system: str,
                     prev_tail: str, next_head: str) -> dict[int, str]:
    """单块翻译: 默认单步一次成稿; two_step=先忠实直译再润色 (两次请求)"""
    ctx.check_pause()          # 并行 worker 的暂停点: 暂停时不发起新的 LLM 请求
    two_step = bool(ctx.cfg("translate.two_step", False))
    stage = "two_step" if two_step else "single"
    key = _chunk_key(ctx, chunk, stage, prev_tail + "|" + next_head)
    hit = _cached(ctx, key)
    if hit:
        ctx.log(f"块 {chunk[0]['id']}~{chunk[-1]['id']} 命中缓存, 跳过")
        return {int(k): v for k, v in hit.items()}
    if two_step:
        faith = llm.chat_json(_faith_prompt(chunk, prev_tail, next_head),
                              validator=_valid_ids(chunk), system=system, log=ctx.log)
        result = llm.chat_json(_express_prompt(chunk, faith),
                               validator=_valid_ids(chunk), system=system, log=ctx.log)
    else:
        result = llm.chat_json(_single_prompt(chunk, prev_tail, next_head),
                               validator=_valid_ids(chunk), system=system, log=ctx.log)
    _store(ctx, key, result)
    return result


def _translate_chunk_polish(ctx, chunk: list[dict], system: str, whole_draft: str) -> dict[int, str]:
    ctx.check_pause()
    key = _chunk_key(ctx, chunk, "polish", whole_draft[:200])
    hit = _cached(ctx, key)
    if hit:
        return {int(k): v for k, v in hit.items()}
    result = llm.chat_json(_polish_prompt(chunk, whole_draft),
                           validator=_valid_ids(chunk), system=system, log=ctx.log)
    _store(ctx, key, result)
    return result


def _run_chunks_parallel(ctx, chunks: list, work_fn, p_from: float, p_to: float,
                         log_prefix: str) -> dict[int, str]:
    """并行执行各块 (llm.max_workers 控制并发); 取消/出错即停派发排队中的块。
    work_fn(ci) -> dict[int, str]"""
    workers = max(1, int(ctx.cfg("llm.max_workers", 1) or 1))
    results: dict[int, str] = {}
    done = 0
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm-chunk")
    try:
        futs = {ex.submit(work_fn, ci): ci for ci in range(len(chunks))}
        for fut in as_completed(futs):
            results.update(fut.result())
            done += 1
            ctx.report_progress(p_from + done / len(chunks) * (p_to - p_from))
            ctx.log(f"{log_prefix} {done}/{len(chunks)} 完成")
            ctx.check_pause()
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)   # 不再派发排队中的块
        raise
    ex.shutdown(wait=True)
    return results


# ==================== 主流程 ====================
def run(ctx) -> None:
    split_path = ctx.upstream("split", "split.json")
    lines: list[dict] = json.loads(
        split_path.read_text(encoding="utf-8"))["lines"]
    if not lines:
        raise StepFailedError("分句结果为空, 无法翻译")

    target = ctx.cfg("translate.target_language", "简体中文")
    full_text = "\n".join(l["text"] for l in lines)
    two_step = bool(ctx.cfg("translate.two_step", False))
    ctx.log(f"待翻译 {len(lines)} 行 / {len(full_text)} 字符, 目标语言: {target}, "
            f"模式: {'两步直译' if two_step else '单步'}")

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
            prompt = (_faith_prompt(lines, "", "") if two_step
                      else _single_prompt(lines, "", ""))
            results = llm.chat_json(prompt, validator=_valid_ids(lines),
                                    system=system, log=ctx.log)
            ctx.report_progress(90)
            ctx.log(f"整篇直译完成 ({__import__('time').time()-t1:.1f}s)")
            _store(ctx, key, results)
    else:
        # ---- 档位二: 分块直译 (并行) + 全局审校 (并行) ----
        chunks = _chunk_lines(lines)
        ctx.log(f"分块翻译: {len(chunks)} 块 (global_polish={ctx.cfg('translate.global_polish', True)})")

        def translate_work(ci: int):
            prev_tail, next_head = _chunk_context(chunks, ci)
            return _translate_chunk(ctx, chunks[ci], system, prev_tail, next_head)

        results.update(_run_chunks_parallel(ctx, chunks, translate_work, 0, 60, "分块直译"))

        if ctx.cfg("translate.global_polish", True):
            whole_draft = "\n".join(f"{l['id']}. {results[l['id']]}" for l in lines)

            def polish_work(ci: int):
                return _translate_chunk_polish(ctx, chunks[ci], system, whole_draft)

            results.update(_run_chunks_parallel(ctx, chunks, polish_work, 60, 95, "全局审校"))

    out = [{"id": l["id"], "src": l["text"], "dst": results[l["id"]]}
           for l in lines]
    ctx.write_json("translate.json", {"target_language": target, "lines": out})
    ctx.log(f"✅ 翻译完成: {len(out)} 行 → {target}")


# 供 tests / API 侧构造假 srt 用的公共出口 (M2b 文本链使用)
__all__ = ["run", "SubLine", "write_srt", "DubForgeError", "re"]
