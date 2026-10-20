"""翻译结果的轻量时长/邻句错位审查。

翻译协议只能证明编号完整，不能证明模型把内容放在了正确的编号中。
这个模块不尝试判断两种语言的语义等价性，而是利用目标语言的朗读时长
估算和原字幕的相邻时间轴，找出最值得复核的窗口。它属于提示性审查，
不会自动改写译文或移动原始时间轴。
"""

from __future__ import annotations

import re
from typing import Iterable


# 仅计文字，不能把 CJK 标点区的句号/逗号算成额外朗读字符。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                  r"\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fd-\u30ff"
                  r"\u1100-\u11ff\u3131-\u318e\uac00-\ud7a3\uff66-\uff9d]")
_LATIN = re.compile(r"(?:[A-Za-z]\.){2,}|[A-Za-z0-9]+(?:['’][A-Za-z]+)?")

# 这些阈值刻意保守：审查应该少报，但不能漏掉“长句挤进短时段，下一句
# 只剩几个字”的明显编号错位。真实 TTS 时长仍以生成的 wav 为准。
# 预估值只作筛查，不能把自然语速差异全部当成错误。相对余量和绝对余量
# 同时达到时才提示；以实际 IndexTTS 结果校准后，默认约覆盖最明显的长句。
DEFAULT_OVERRUN_RATIO = 1.6
DEFAULT_OVERRUN_SECONDS = 0.6
DEFAULT_UNDER_RATIO = 0.35
DEFAULT_UNDER_SECONDS = 0.75
DEFAULT_CONTIGUOUS_GAP = 0.35
# 这一档筛出最值得复核的估时差异，不能据此认定编号错位或阻止 TTS。
# 真实声音可能较短，也能利用有界滑动和相邻窗口；最终以 WAV 排布为准。
DEFAULT_BLOCKING_OVERRUN_RATIO = 2.0
DEFAULT_BLOCKING_OVERRUN_SECONDS = 0.6


def estimate_speech_seconds(text: str) -> float:
    """估算目标语言自然朗读时长（秒）。

    这是 TTS 前的筛查尺度，不绑定某个引擎：中日韩按字、拉丁文字按词
    估计，并保留 0.3 秒的短句下限。标点和空格不计时。
    """

    value = str(text or "")
    cjk = len(_CJK.findall(value))
    latin = len(_LATIN.findall(value))
    return max(0.3, cjk / 4.7 + latin / 3.0)


def _row(line: dict, next_line: dict | None) -> dict:
    start = float(line["start"])
    end = float(line["end"])
    next_start = float(next_line["start"]) if next_line is not None else end
    # 相邻原始起点间隔仅作估时筛查，不等同于真实排布可借用的窗口。
    available = max(0.0, next_start - start)
    source_gap = max(0.0, next_start - end)
    estimated = estimate_speech_seconds(line.get("text", ""))
    return {
        "id": int(line["id"]),
        "start": start,
        "end": end,
        "available": available,
        "source_gap": source_gap,
        "estimated": estimated,
        "text_length": len(str(line.get("text", ""))),
    }


def audit(lines: Iterable[dict], *,
          overrun_ratio: float = DEFAULT_OVERRUN_RATIO,
          overrun_seconds: float = DEFAULT_OVERRUN_SECONDS,
          under_ratio: float = DEFAULT_UNDER_RATIO,
          under_seconds: float = DEFAULT_UNDER_SECONDS,
          contiguous_gap: float = DEFAULT_CONTIGUOUS_GAP,
          max_windows: int = 32) -> dict:
    """审查译文是否出现明显的时长挤压和连续行错位。

    返回值包含逐句 ``rows``、逐句 ``issues`` 和供局部 LLM 校对使用的
    ``suspect_windows``。``sequence_shift`` 只有在相邻两句没有真实停顿、
    且一长一短同时发生时才触发；单独的长译文只报告
    ``target_duration_overrun``，避免把正常中英长度差异误诊成错位。
    """

    source = list(lines)
    rows = [_row(line, source[index + 1] if index + 1 < len(source) else None)
            for index, line in enumerate(source)]
    issues: list[dict] = []
    overrun_ids: set[int] = set()
    under_ids: set[int] = set()
    for row in rows:
        available = row["available"]
        estimated = row["estimated"]
        excess = estimated - available
        overrun = (available > 0 and excess >= max(0.0, float(overrun_seconds))
                   and estimated > available * max(1.0, float(overrun_ratio)))
        under = (available - estimated >= max(0.0, float(under_seconds))
                 and estimated < available * min(1.0, max(0.0, float(under_ratio))))
        if overrun:
            overrun_ids.add(row["id"])
            issues.append({"kind": "target_duration_overrun", "id": row["id"],
                           "estimated": round(estimated, 3),
                           "available": round(available, 3),
                           "excess": round(excess, 3)})
        if under:
            under_ids.add(row["id"])
            kind = ("target_duration_underuse"
                    if row["source_gap"] <= float(contiguous_gap)
                    else "unused_source_gap")
            issues.append({"kind": kind, "id": row["id"],
                           "estimated": round(estimated, 3),
                           "available": round(available, 3),
                           "unused": round(available - estimated, 3)})

    windows: list[dict] = []
    for index, (left, right) in enumerate(zip(rows, rows[1:])):
        if (left["source_gap"] > float(contiguous_gap)
                or right["source_gap"] > float(contiguous_gap)):
            continue
        left_long, right_short = left["id"] in overrun_ids, right["id"] in under_ids
        left_short, right_long = left["id"] in under_ids, right["id"] in overrun_ids
        if not ((left_long and right_short) or (left_short and right_long)):
            continue
        ids = [left["id"], right["id"]]
        issues.append({"kind": "sequence_shift_suspect", "ids": ids,
                       "reason": "连续台词一侧明显超时、另一侧明显空闲，可能发生跨编号翻译"})
        windows.append({"first_id": left["id"], "last_id": right["id"],
                        "line_indices": [index, index + 1],
                        "reason": "连续台词一长一短，建议局部逐句复核"})

    # 将相邻的可疑对合并成小窗口，供一次局部复核；不跨真实停顿，且有界。
    merged: list[dict] = []
    for window in windows:
        if merged and window["line_indices"][0] <= merged[-1]["line_indices"][1] + 1:
            merged[-1]["line_indices"][1] = window["line_indices"][1]
            merged[-1]["last_id"] = window["last_id"]
        else:
            merged.append(dict(window, line_indices=list(window["line_indices"])))
    return {"rows": rows, "issues": issues, "suspect_windows": merged[:max(0, int(max_windows))]}


def blocking_issues(result: dict, *,
                    overrun_ratio: float = DEFAULT_BLOCKING_OVERRUN_RATIO,
                    overrun_seconds: float = DEFAULT_BLOCKING_OVERRUN_SECONDS) -> list[dict]:
    """筛选需要重点提示的估时差异，兼容原有调用名称。

    字数与相邻起点间隔无法证明语义错位，也没有考虑自适应排布的实际
    容量。返回项只用于日志与审查定位；调用方不得将其作为制作硬门禁。
    无效时间轴和真实音频的最终排布冲突仍由各自的严格校验负责。
    """
    ratio = max(1.0, float(overrun_ratio))
    seconds = max(0.0, float(overrun_seconds))
    rows = {
        int(row["id"]): row for row in (result.get("rows") or [])
        if isinstance(row, dict) and "id" in row
    }
    severe = []
    for issue in result.get("issues") or []:
        kind = issue.get("kind") if isinstance(issue, dict) else None
        if kind == "sequence_shift_suspect":
            severe.append(issue)
            continue
        if kind != "target_duration_overrun":
            continue
        row = rows.get(int(issue.get("id", -1)))
        if not row:
            continue
        if (float(row.get("estimated", 0)) >= float(row.get("available", 0)) * ratio
                and float(row.get("estimated", 0)) - float(row.get("available", 0)) >= seconds):
            severe.append(issue)
    return severe


def summary(result: dict) -> str:
    """生成适合步骤日志/界面的中文摘要。"""

    issues = result.get("issues") or []
    over = sum(item.get("kind") == "target_duration_overrun" for item in issues)
    under = sum(item.get("kind") == "target_duration_underuse" for item in issues)
    gaps = sum(item.get("kind") == "unused_source_gap" for item in issues)
    shifts = sum(item.get("kind") == "sequence_shift_suspect" for item in issues)
    if not (over or under or gaps or shifts):
        return "翻译时长预检通过：未发现明显的目标语音时长挤压或连续行错位。"
    return (f"翻译时长预检：{over}句可能超出连续可用时段，{under}句连续台词时段明显未利用，"
            f"{gaps}句后方存在可复核空档，"
            f"{shifts}处连续行可能错位；建议对标记窗口做逐句复核。")
