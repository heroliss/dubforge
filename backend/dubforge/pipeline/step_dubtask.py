"""
配音任务准备（dubtask 运行入口仅为旧工程保留）
===============================
输入: 语义整理后的时间轴 + translate/translate.txt (逐句译文)
职责: 为每行估算朗读时长并提示时长偏差，计算到下一行的间隙。
      保持语义整理的句子和时间轴；此处只做预估，不自动调整实际配音速度。

估算启发式 (无 ML 依赖):
  CJK ≈ 4.7 字/秒, 拉丁词 ≈ 3.0 词/秒 (自然中文配音语速)
speed_flag: 2=明显偏长 1=偏长 0=容差内 -1=偏短（均为合成前预估）
产物: dubtask.json {lines:[{id,text,start,end,duration,est_dur,gap,speed_flag}]}
"""
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
    if est / 1.35 > duration + tolerance:      # 假设加速 35% 仍偏长，作为较强提示
        return 2
    if est > duration + tolerance:
        return 1
    if est < duration - tolerance:
        return -1
    return 0


def build_task(ctx, *, editing=False) -> dict:
    """从当前语义时间轴和译文派生任务；试配、批量配音共用，无需先运行独立步骤。"""
    from dubforge.pipeline.subtitle_document import read
    lines = read(ctx, translation=not editing, strict=not editing)
    total = max(l["end"] for l in lines)
    tolerance = float(ctx.cfg("tts.tolerance", 0.4))   # 预估时长偏差容差

    out = []
    for i, l in enumerate(lines):
        duration = max(0.0, l["end"] - l["start"])
        est = estimate_seconds(l["text"])
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        gap = ((nxt["start"] if nxt else total) - l["end"]) if total else 0.0
        gap = max(0.0, gap)
        out.append({
            **l,
            "id": l["id"], "text": l["text"],
            "start": l["start"], "end": l["end"],
            "duration": round(duration, 2),
            "est_dur": round(est, 2),
            "gap": round(gap, 2),
            "available_duration": max(0.0, (nxt['start'] if nxt else l['end']) - l['start']),
            "speed_flag": speed_flag(est, duration, tolerance),
        })

    return {
        "total_duration": round(total, 2),
        "tolerance": tolerance,
        "lines": out,
    }


def run(ctx) -> None:
    """兼容旧工程的显式任务表导出入口。"""
    task = build_task(ctx)
    ctx.write_json("dubtask.json", task)
    out = task["lines"]
    flags = [l["speed_flag"] for l in out]
    ctx.log(f"✅ 配音任务: {len(out)} 行 | 预估偏长 {flags.count(1)} 明显偏长 {flags.count(2)} "
            f"容差内 {flags.count(0)} 偏短 {flags.count(-1)}；仅提示，不自动变速")
