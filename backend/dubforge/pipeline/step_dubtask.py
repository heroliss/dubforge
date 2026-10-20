"""
配音任务准备（dubtask 运行入口仅为旧工程保留）
===============================
输入: 语义整理后的时间轴 + translate/translate.txt (逐句译文)
职责: 为每行估算朗读时长并提示时长偏差，计算连续语音岛的候选可用范围。
      保持语义整理的句子和源时间轴；实际配音先使用语音岛范围，成片时再做最终铺轨。

估算启发式 (无 ML 依赖):
  CJK ≈ 4.7 字/秒, 拉丁词 ≈ 3.0 词/秒 (自然中文配音语速)
speed_flag: 2=明显偏长 1=偏长 0=容差内 -1=偏短（均为合成前预估）
产物: dubtask.json {lines:[{id,text,start,end,duration,est_dur,gap,speed_flag}]}
"""
from dubforge.pipeline.translation_alignment import estimate_speech_seconds


def estimate_seconds(text: str) -> float:
    """任务准备与翻译审查共用同一自然语速估算。"""
    return estimate_speech_seconds(text)


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
    # TTS 的第一次尝试不能只看本句到下一句的短槽位；连续台词之间的
    # 空余应先作为同一语音岛的候选范围，实际 WAV 生成后再由铺轨器做
    # 最终排布。长停顿仍是硬边界，不会被借用。
    try:
        island_gap = max(.6, float(ctx.cfg("asr.pause_threshold_seconds", .6) or .6))
    except (TypeError, ValueError, OverflowError):
        island_gap = .6
    island_ends = {}
    island_start = 0
    for index in range(len(lines) - 1):
        gap = float(lines[index + 1]['start']) - float(lines[index]['end'])
        if gap > island_gap:
            island_end = float(lines[index]['end'])
            for member in range(island_start, index + 1):
                island_ends[lines[member]['id']] = island_end
            island_start = index + 1
    if lines:
        island_end = float(lines[-1]['end'])
        for member in range(island_start, len(lines)):
            island_ends[lines[member]['id']] = island_end
    for i, l in enumerate(lines):
        duration = max(0.0, l["end"] - l["start"])
        est = estimate_seconds(l["text"])
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        gap = ((nxt["start"] if nxt else total) - l["end"]) if total else 0.0
        gap = max(0.0, gap)
        adaptive_available = max(
            max(0.0, (nxt['start'] if nxt else l['end']) - l['start']),
            max(0.0, island_ends.get(l['id'], l['end']) - l['start']),
        )
        out.append({
            **l,
            "id": l["id"], "text": l["text"],
            "start": l["start"], "end": l["end"],
            "duration": round(duration, 2),
            "est_dur": round(est, 2),
            "gap": round(gap, 2),
            "available_duration": max(0.0, (nxt['start'] if nxt else l['end']) - l['start']),
            "adaptive_available_duration": round(adaptive_available, 3),
            "speed_flag": speed_flag(est, duration, tolerance),
        })

    return {
        "total_duration": round(total, 2),
        "tolerance": tolerance,
        "lines": out,
    }


def run(ctx) -> None:
    """兼容旧工程的显式任务表导出入口。"""
    from dubforge.execution_progress import report_stage
    report_stage(ctx, 'preparing_dubbing_task', '核对译文时间轴与预估朗读时长')
    task = build_task(ctx)
    report_stage(ctx, 'saving', '保存配音任务表', completed=len(task['lines']), total=len(task['lines']), unit='句')
    ctx.write_json("dubtask.json", task)
    out = task["lines"]
    flags = [l["speed_flag"] for l in out]
    ctx.log(f"✅ 配音任务: {len(out)} 行 | 预估偏长 {flags.count(1)} 明显偏长 {flags.count(2)} "
            f"容差内 {flags.count(0)} 偏短 {flags.count(-1)}；仅提示，不自动变速")
