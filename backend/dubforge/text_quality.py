"""离线字幕质量回归：只读候选、报告疑点，不请求模型或修改项目产物。

样本中的人工关注点不是生产校验规则。文字/词轴不能确认谁在说话或是否无声；
因此内容匹配只记录观察，报告不生成整体质量分，也不自动删除任何词。
"""
import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.lineprotocols import NUMBERED_LINE_RE, parse_timed_lines


class TextQualityError(DubForgeError):
    code = "TEXT_QUALITY_INPUT_INVALID"
    suggestion = "请检查样本 JSON、整理时间轴和编号译文；评估工具只读取这些文件。"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise TextQualityError(f"无法读取评估输入：{path.name}") from error


def _number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _window(value) -> bool:
    return (isinstance(value, list) and len(value) == 2
            and all(_number(item) for item in value) and 0 <= value[0] < value[1])


def load_corpus(path: Path) -> dict:
    """样本只包含字幕、词轴及公开出处，不接触配置、数据库或连接凭据。"""
    try:
        corpus = json.loads(_read(path))
        if not isinstance(corpus, dict) or corpus.get("version") != 1:
            raise ValueError("样本版本必须为1")
        sources, cases = corpus["sources"], corpus["cases"]
        if not isinstance(sources, dict) or not sources or not isinstance(cases, list) or not cases:
            raise ValueError("需要来源和关注片段")
        seen = set()
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in seen:
                raise ValueError("关注片段标识缺失或重复")
            seen.add(case["id"])
            if case.get("source") not in sources or not _window(case.get("window")):
                raise ValueError("关注片段来源或时间范围无效")
            if not isinstance(case.get("focus"), str) or not isinstance(case.get("annotations", []), list):
                raise ValueError("关注片段需要关注说明和标注列表")
            for gap in case.get("reference_gaps", []):
                if not _window(gap) or gap[0] < case["window"][0] or gap[1] > case["window"][1]:
                    raise ValueError("参考词轴空档超出片段")
            for note in case.get("annotations", []):
                if (not isinstance(note, dict) or note.get("kind") not in
                        {"preserve", "boundary", "correction", "suspected_noise"}
                        or not isinstance(note.get("label"), str)):
                    raise ValueError("标注类型或说明无效")
                alternatives = note.get("alternatives")
                if (not isinstance(alternatives, list) or not alternatives
                        or any(not isinstance(item, str) or not item.strip() for item in alternatives)):
                    raise ValueError("标注需要非空候选措辞")
                if note.get("match_mode", "contains") not in {"contains", "exact", "tail"}:
                    raise ValueError("标注匹配方式无效")
        return corpus
    except (ValueError, KeyError, TypeError) as error:
        raise TextQualityError(f"质量样本格式有误：{error}") from error


def _key(text: str) -> str:
    # 标点/空格差异不作为措辞变化；不做模糊推断、翻译或同义改写判定。
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def _contains(text: str, phrase: str) -> bool:
    return f" {_key(phrase)} " in f" {_key(text)} "


def _matches(text: str, note: dict) -> bool:
    mode = note.get("match_mode", "contains")
    return any((_key(text) == _key(phrase) if mode == "exact" else
                f" {_key(text)} ".endswith(f" {_key(phrase)} ") if mode == "tail" else
                _contains(text, phrase)) for phrase in note["alternatives"])


def _union_length(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, None
    for start, finish in sorted(intervals):
        if finish <= start:
            continue
        total += max(0, finish - max(start, end if end is not None else start))
        end = max(finish, end if end is not None else finish)
    return round(total, 3)


def _gap_occupancy(rows: list[dict], gaps: list[list[float]]) -> list[dict]:
    return [{"start": start, "end": end,
             "occupied_seconds": _union_length([
                 (max(start, row["start"]), min(end, row["end"])) for row in rows]),
             "line_ids": [row["id"] for row in rows if row["start"] < end and row["end"] > start]}
            for start, end in gaps]


def _translation(text: str | None, source_ids: list[int]) -> dict:
    if text is None:
        return {"status": "not_evaluated", "reason": "未提供译文；不能判断对应关系"}
    ids, empty, malformed = [], [], []
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = NUMBERED_LINE_RE.fullmatch(line)
        if not match:
            malformed.append(index)
            continue
        line_id = int(match[1])
        ids.append(line_id)
        if not match[2].strip():
            empty.append(line_id)
    expected, observed = set(source_ids), set(ids)
    duplicates = sorted(line_id for line_id, count in Counter(ids).items() if count > 1)
    missing, extra = sorted(expected - observed), sorted(observed - expected)
    order_matches = ids == source_ids
    return dict(status="ok" if not (malformed or duplicates or missing or extra) and order_matches else "problem",
                source_lines=len(source_ids), translation_lines=len(ids), missing_ids=missing,
                extra_ids=extra, duplicate_ids=duplicates, malformed_file_lines=malformed,
                order_matches=order_matches, empty_ids=empty,
                content_correspondence="需人工核对；编号一致不能证明译文语义对应，空译文也不直接判错")


def evaluate(corpus: dict, source: str, split_text: str, translation_text: str | None = None) -> dict:
    """独立评估一份候选；报告完整稿结构及固定片段观察，不执行后处理。"""
    if source not in corpus["sources"]:
        raise TextQualityError(f"未知样本来源：{source}")
    try:
        rows = parse_timed_lines(split_text)
    except ValueError as error:
        raise TextQualityError(f"整理稿无法解析：{error}") from error
    if any(not _number(row[key]) for row in rows for key in ("start", "end")):
        raise TextQualityError("整理稿包含无法表示的时间，请使用有限秒数")
    invalid = [row["id"] for row in rows if not all(_number(row[key]) for key in ("start", "end"))
               or row["start"] < 0 or row["end"] <= row["start"] or not row["text"].strip()]
    ids = [row["id"] for row in rows]
    duplicates = sorted(line_id for line_id, count in Counter(ids).items() if count > 1)
    overlaps = []
    previous_end, previous_id = -1.0, None
    for row in rows:
        if row["start"] < previous_end - .011:
            overlaps.append({"left_id": previous_id, "right_id": row["id"],
                             "seconds": round(max(0, min(previous_end, row["end"]) - row["start"]), 3)})
        if row["end"] > previous_end:
            previous_end, previous_id = row["end"], row["id"]
    cases = []
    for case in corpus["cases"]:
        if case["source"] != source:
            continue
        start, end = case["window"]
        nearby = [row for row in rows if row["start"] < end and row["end"] > start]
        observations = []
        for note in case.get("annotations", []):
            matches = [row["id"] for row in nearby if _matches(row["text"], note)]
            observations.append(dict(kind=note["kind"], label=note["label"], matched_line_ids=matches,
                                     status="observed" if matches else "not_observed",
                                     interpretation=("出现疑似残词，需核听，不能据此删词" if matches else
                                                     "未观察到该措辞，不能证明已正确清理")
                                     if note["kind"] == "suspected_noise" else
                                     "仅记录标注措辞是否出现；未匹配也可能是合理改写，需复核"))
        cases.append(dict(id=case["id"], focus=case["focus"], window=case["window"],
                          rows=nearby, observations=observations,
                          reference_gaps=_gap_occupancy(nearby, case.get("reference_gaps", [])),
                          short_line_ids=[row["id"] for row in nearby if row["end"] - row["start"] < .8],
                          one_word_line_ids=[row["id"] for row in nearby if len(_key(row["text"]).split()) == 1],
                          audio_review=case.get("audio_review", "pending")))
    return dict(version=1, source=source, source_provenance=corpus["sources"][source],
                scope="离线文字和时间观察，不证明声学或翻译质量，不影响生产校验",
                structure=dict(line_count=len(rows), empty=not rows, invalid_line_ids=invalid, duplicate_ids=duplicates,
                               consecutive_ids=ids == list(range(1, len(ids) + 1)), overlaps=overlaps),
                translation=_translation(translation_text, ids), cases=cases,
                measurements=dict(llm_tokens=None, model_duration_seconds=None,
                                  reason="未请求模型；推理时间和 token 消耗未测量"))


def compare(current: dict, baseline: dict) -> dict:
    """只比较可复现的计数，不把更多删词或更少短句当作质量提高。"""
    if current["source"] != baseline["source"]:
        raise TextQualityError("两份候选必须对应同一样本来源")
    previous = {case["id"]: case for case in baseline["cases"]}
    if set(previous) != {case["id"] for case in current["cases"]}:
        raise TextQualityError("两份报告使用了不同关注片段，请使用同一份固定样本重新评估")
    return {"line_count_delta": current["structure"]["line_count"] - baseline["structure"]["line_count"],
            "overlap_count_delta": len(current["structure"]["overlaps"]) - len(baseline["structure"]["overlaps"]),
            "cases": [{"id": case["id"],
                       "reference_gap_occupancy_delta_seconds": round(
                           sum(gap["occupied_seconds"] for gap in case["reference_gaps"]) -
                           sum(gap["occupied_seconds"] for gap in previous[case["id"]]["reference_gaps"]), 3),
                       "observations": [{"label": note["label"], "before": old["status"], "after": note["status"]}
                                        for old, note in zip(previous[case["id"]]["observations"], case["observations"])]}
                      for case in current["cases"]],
            "interpretation": "变化供逐项复核；没有整体质量分，短句减少或疑词消失不自动代表改善"}


def render_markdown(report: dict) -> str:
    current = report["current"]
    structure, translation = current["structure"], current["translation"]
    labels = {"observed": "已观察到", "not_observed": "未观察到", "ok": "编号对应",
              "problem": "存在结构问题", "not_evaluated": "未提供译文", "pending": "待核听"}
    lines = ["# 字幕离线质量观察", "", current["scope"], "",
             f"来源：{current['source']}；整理稿共 {structure['line_count']} 行。",
             f"无效行：{structure['invalid_line_ids']}；重复编号：{structure['duplicate_ids']}；"
             f"连续编号：{'是' if structure['consecutive_ids'] else '否'}；时间重叠：{len(structure['overlaps'])} 处。",
             f"译文结构：{labels[translation['status']]}。编号一致仍需核对逐行语义。", ""]
    for case in current["cases"]:
        lines += [f"## {case['id']}（{case['window'][0]}–{case['window'][1]} 秒）", "", case["focus"], "",
                  f"原音复核：{labels.get(case['audio_review'], case['audio_review'])}；短于0.8秒行：{case['short_line_ids']}；"
                  f"单词行：{case['one_word_line_ids']}。这些计数不是错误判定。", ""]
        for gap in case["reference_gaps"]:
            lines.append(f"- 参考词轴空档 {gap['start']}–{gap['end']} 秒，字幕占用 {gap['occupied_seconds']} 秒；"
                         "词轴空档仍需结合原音确认。")
        for note in case["observations"]:
            lines.append(f"- {note['label']}：{labels[note['status']]}，行号 {note['matched_line_ids']}。{note['interpretation']}。")
        lines += ["", *[f"{row['id']}. {row['start']:.3f}-{row['end']:.3f}: {row['text']}" for row in case["rows"]], ""]
    if "comparison" in report:
        comparison = report["comparison"]
        lines += ["## 与基线比较", "", comparison["interpretation"], "",
                  f"总行数变化：{comparison['line_count_delta']:+d}；时间重叠处数变化：{comparison['overlap_count_delta']:+d}。", ""]
        for case in comparison["cases"]:
            lines.append(f"- {case['id']}：参考空档占用变化 {case['reference_gap_occupancy_delta_seconds']:+.3f} 秒。")
            lines += [f"  - {note['label']}：{labels[note['before']]} → {labels[note['after']]}。" for note in case["observations"]]
    lines += ["", "本次未调用 LLM、ASR 或 TTS，未测量推理时长和 token 消耗；输入文件保持只读。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读评估固定字幕样本，不调用模型，不改变生产校验")
    parser.add_argument("--corpus", type=Path, required=True, help="固定样本 JSON")
    parser.add_argument("--source", help="样本来源标识；--list 可查看")
    parser.add_argument("--list", action="store_true", help="列出样本来源和关注片段")
    parser.add_argument("--split", type=Path, help="待观察的整理协议文本")
    parser.add_argument("--translate", type=Path, help="对应编号译文，可省略")
    parser.add_argument("--baseline", type=Path, help="可选旧整理稿，比较同一来源")
    parser.add_argument("--baseline-translate", type=Path, help="对应旧编号译文")
    parser.add_argument("--report", type=Path, help="可选报告输出路径；默认只输出到终端")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)
    try:
        corpus = load_corpus(args.corpus)
        if args.list:
            print(json.dumps({source: [case["id"] for case in corpus["cases"] if case["source"] == source]
                              for source in corpus["sources"]}, ensure_ascii=False, indent=2))
            return 0
        if not args.source or args.split is None:
            raise TextQualityError("请指定 --source 和 --split，或使用 --list 查看样本")
        if args.baseline_translate and not args.baseline:
            raise TextQualityError("--baseline-translate 需要同时指定 --baseline")
        inputs = [path for path in (args.corpus, args.split, args.translate, args.baseline, args.baseline_translate) if path]
        if args.report and args.report.resolve() in {path.resolve() for path in inputs}:
            raise TextQualityError("报告路径不能覆盖任何评估输入")
        report = {"current": evaluate(corpus, args.source, _read(args.split), _read(args.translate) if args.translate else None),
                  "inputs": [{"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in inputs]}
        if args.baseline:
            baseline = evaluate(corpus, args.source, _read(args.baseline),
                                _read(args.baseline_translate) if args.baseline_translate else None)
            report["baseline"] = baseline
            report["comparison"] = compare(report["current"], baseline)
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else render_markdown(report)
        if args.report:
            atomic_write_bytes(args.report, output.encode("utf-8"))
            print(f"已保存离线观察报告：{args.report}")
        else:
            print(output)
        return 0
    except (DubForgeError, OSError) as error:
        print(f"评估未完成：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
