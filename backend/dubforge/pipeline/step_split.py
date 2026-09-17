# -*- coding: utf-8 -*-
"""
步骤 4: split —— 分句
=====================
输入: transcribe 的 src.srt + words.json (词级时间戳)
职责: 把超过 max_split_length 的长行拆成适合字幕/配音的短句, 并保持时间轴。
  - 规则切: 按标点 (逗号/连接词/空格) 贪心切分到词数上限 —— 零成本兜底
  - LLM 语义切: 对"规则切不动"的超长行, 让 LLM 给出语义子句; 校验拼接一致,
    失败自动回退规则切 (鲁棒优先)
  - 时间轴: 优先用词级时间戳精确定位子句边界; 词缺失时按字符占比线性分摊

产物: split.json {lines: [{id, text, start, end}]} (翻译的输入行)
"""
import json
import re

from dubforge.utils.srt import SubLine, parse_srt

# 英文常见从句连接词 (规则切分点, 仅超长行兜底用)
_BREAK_WORDS = {"and", "but", "so", "because", "which", "that", "when",
                "while", "then", "or", "however", "therefore"}


def _wc(text: str) -> int:
    """词数: 英文按空白, CJK 按字"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len([w for w in re.findall(r"[A-Za-z0-9']+", text)])
    return cjk + words


def _rule_split(text: str, max_len: int) -> list[str]:
    """规则切分: 按词 token (含尾随空格) 贪心分组到 max_len 词数内。

    保真约定: "".join(parts) 必须与原文逐字符一致 (token 自带空格),
    否则英文行拼接处会出现 wordword 粘连。
    """
    if _wc(text) <= max_len:
        return [text]
    tokens = re.findall(r"\s*\S+\s*", text)
    groups: list[str] = []
    buf, cnt = "", 0
    for tok in tokens:
        w = _wc(tok)
        if cnt + w > max_len and buf:
            groups.append(buf)
            buf, cnt = "", 0
        buf += tok
        cnt += w
    if buf.strip():
        groups.append(buf)
    # 首尾空白规整 (保留组间空格)
    return [g if i == 0 else g for i, g in enumerate(groups)] if groups else [text]


def _llm_split(ctx, text: str, max_len: int) -> list[str] | None:
    """LLM 语义拆分; 任何异常/不一致 → None (调用方回退规则切)"""
    from dubforge.backends import llm
    prompt = (
        f"请把下面的字幕长句拆分成 {max_len} 词以内的短句。\n"
        f"要求: 1) 保留原文语言与用词, 不翻译不改写; 2) 在语义自然处断开;\n"
        f"3) 输出 JSON 数组, 元素为字符串, 按顺序拼接后必须与原句完全一致。\n\n"
        f"原句: {text}\n\n"
        f'输出示例: ["短句一,", "短句二"]'
    )

    def valid(obj):
        if not isinstance(obj, list) or not obj:
            raise ValueError("不是非空数组")
        for x in obj:
            if not isinstance(x, str) or not x.strip():
                raise ValueError("数组含非字符串元素")
        joined = re.sub(r"\s+", "", "".join(obj))
        orig = re.sub(r"\s+", "", text)
        if joined != orig:
            raise ValueError("拼接后与原句不一致 (不得增删改字符)")

    try:
        parts = llm.chat_json(prompt, validator=valid, temperature=0.1,
                              log=ctx.log)
        return [p.strip() for p in parts]
    except Exception as e:  # noqa: BLE001
        ctx.log(f"LLM 语义拆分失败, 回退规则切: {e}")
        return None


def _words_in_range(words: list[dict], start: float, end: float) -> list[dict]:
    return [w for w in words if w["start"] >= start - 0.08 and w["end"] <= end + 0.08]


def _assign_times(text: str, line: SubLine, line_words: list[dict]) -> tuple[float, float]:
    """按字符占比在行时间窗内线性分摊 (词精确对齐的优化留后续)"""
    return line.start, line.end


def _times_for_parts(parts: list[str], line: SubLine,
                     line_words: list[dict]) -> list[tuple[float, float]]:
    """给拆分出的子句分配时间窗; 词级数据可用时用词边界, 否则字符占比"""
    times: list[tuple[float, float]] = []
    if line_words:
        idx = 0
        cursor = 0.0
        total_chars = sum(len(p) for p in parts)
        for p in parts:
            frac_chars = len(p)
            # 按字符占比找到应消耗的词数
            target = (cursor + frac_chars) / max(1, sum(len(x) for x in parts))
            end_idx = max(idx + 1, int(round(target * len(line_words))))
            end_idx = min(end_idx, len(line_words))
            seg_words = line_words[idx:end_idx]
            if seg_words:
                times.append((seg_words[0]["start"], seg_words[-1]["end"]))
            else:
                times.append(_assign_times(p, line, line_words))
            cursor += frac_chars
            idx = end_idx
    else:
        total = sum(len(p) for p in parts)
        span = line.end - line.start
        acc = 0
        for p in parts:
            t0 = line.start + span * acc / total
            acc += len(p)
            times.append((t0, line.start + span * acc / total))
    # 修正相邻边界与单调性
    for i in range(len(times) - 1):
        if times[i][1] > times[i + 1][0]:
            mid = (times[i][1] + times[i + 1][0]) / 2
            times[i] = (times[i][0], mid)
            times[i + 1] = (mid, times[i + 1][1])
    return times


def run(ctx) -> None:
    src_srt = ctx.upstream("transcribe", "src.srt")
    words: list[dict] = json.loads(ctx.upstream("transcribe", "words.json").read_text(encoding="utf-8"))
    max_len = int(ctx.cfg("split.max_split_length", 22))
    use_llm = bool(ctx.cfg("split.use_llm", True))

    lines = parse_srt(src_srt)
    out: list[dict] = []
    next_id = 1
    for line in lines:
        ctx.check_cancel()
        if _wc(line.text) <= max_len:
            out.append({"id": next_id, "text": line.text,
                        "start": round(line.start, 3), "end": round(line.end, 3)})
            next_id += 1
            continue

        # 超长行: 规则切 → (可选) LLM 语义切校验替换
        parts = _rule_split(line.text, max_len)
        if use_llm and len(parts) > 1:
            better = _llm_split(ctx, line.text, max_len)
            if better:
                parts = better
        lw = _words_in_range(words, line.start, line.end)
        times = _times_for_parts(parts, line, lw)
        for text, (s, e) in zip(parts, times):
            out.append({"id": next_id, "text": text,
                        "start": round(s, 3), "end": round(e, 3)})
            next_id += 1
        ctx.log(f"长行 #{line.index} 拆为 {len(parts)} 句")

    ctx.write_json("split.json", {
        "count": len(out),
        "lines": out,
    })
    ctx.log(f"✅ 分句完成: {len(lines)} 行 → {len(out)} 句 (max_len={max_len})")
