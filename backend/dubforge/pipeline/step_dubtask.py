# -*- coding: utf-8 -*-
"""
步骤 7: dubtask —— 配音任务分析
===============================
输入: split/split.json + transcribe/transcribe.json (总时长)
职责: 为每行估算朗读时长与语速状态, 计算到下一行的间隙 (容错空间)。
      不做行合并/拆分 (whisper 断句已合理; 过短行由配音端 duration_factor 微调)。

估算启发式 (无 ML 依赖):
  CJK ≈ 4.7 字/秒, 拉丁词 ≈ 3.0 词/秒 (自然中文配音语速)
speed_flag: 2=严重超速(无法容忍) 1=需加速 0=健康 -1=偏慢
产物: dubtask.json {lines:[{id,text,start,end,duration,est_dur,gap,speed_flag}]}
"""
import json
import re


def _wc(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9']+", text))
    return cjk, latin


def estimate_seconds(text: str) -> float:
    """自然语速朗读时长估算"""
    cjk, latin = _wc(text)
    return max(0.3, cjk / 4.7 + latin / 3.0)


def speed_flag(est: float, duration: float, tolerance: float) -> int:
    if est / 1.35 > duration + tolerance:      # 最大可接受加速 1.35x 仍放不下
        return 2
    if est > duration + tolerance:
        return 1
    if est < duration - tolerance:
        return -1
    return 0


def run(ctx) -> None:
    split_data = json.loads(ctx.upstream("split", "split.json").read_text(encoding="utf-8"))
    trans = json.loads(ctx.upstream("transcribe", "transcribe.json").read_text(encoding="utf-8"))
    total = float(trans.get("duration") or 0)
    tolerance = float(ctx.cfg("tts.tolerance", 0.4))   # 语速容忍秒数

    lines = split_data["lines"]
    out = []
    for i, l in enumerate(lines):
        ctx.check_cancel()
        duration = max(0.0, l["end"] - l["start"])
        est = estimate_seconds(l["text"])
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        gap = ((nxt["start"] if nxt else total) - l["end"]) if total else 0.0
        gap = max(0.0, gap)
        out.append({
            "id": l["id"], "text": l["text"],
            "start": l["start"], "end": l["end"],
            "duration": round(duration, 2),
            "est_dur": round(est, 2),
            "gap": round(gap, 2),
            "speed_flag": speed_flag(est, duration, tolerance),
        })

    ctx.write_json("dubtask.json", {
        "total_duration": round(total, 2),
        "tolerance": tolerance,
        "lines": out,
    })
    flags = [l["speed_flag"] for l in out]
    ctx.log(f"✅ 配音任务: {len(out)} 行 | 超速 {flags.count(1)} 严重 {flags.count(2)} "
            f"健康 {flags.count(0)} 偏慢 {flags.count(-1)}")
