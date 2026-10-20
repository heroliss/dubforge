"""按原片时间轴拼接的非破坏性试听预览。

虚拟试听只生成临时 MP4，不创建正式 ``assemble`` 任务，也不写入成片
登记。每个输出区间都带有源时间和实际媒介时间，因而删除/恢复可以在原片
坐标上编辑；正式成片仍必须重新运行合成步骤。
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions, output_cuts
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.video_source import resolve_video
from dubforge.utils.fs import atomic_replace, atomic_write_bytes


MAX_SEGMENTS = 160
MAX_PREVIEW_SECONDS = 2 * 60 * 60
SEEK_MARGIN_SECONDS = 1.0
TASK_TTL_SECONDS = 2 * 60 * 60
PREVIEW_RENDER_VERSION = 2
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")
_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dubforge-virtual-preview")
_tasks: dict[str, dict] = {}
_processes: dict[str, subprocess.Popen] = {}


class VirtualPreviewError(DubForgeError):
    code = "VIRTUAL_PREVIEW_INVALID"
    suggestion = "请检查当前成片、原视频和恢复来源是否仍来自同一个原视频，然后重试。"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value: str | None, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise VirtualPreviewError(f"{label}标识无效", code="VIRTUAL_PREVIEW_ID_INVALID")
    return value


def _number(value, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise VirtualPreviewError(f"{label}必须是有限的秒数")
    value = round(float(value), 6)
    if value < 0:
        raise VirtualPreviewError(f"{label}不能小于 0 秒")
    return value


def _intervals(raw, duration: float, label: str = "删除清单") -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 200:
        raise VirtualPreviewError(f"{label}最多保存 200 段", code="VIRTUAL_PREVIEW_RANGES_INVALID")
    result = []
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise VirtualPreviewError(f"{label}第 {index} 段格式无效", code="VIRTUAL_PREVIEW_RANGES_INVALID")
        start = _number(row.get("start"), f"{label}第 {index} 段起点")
        end = _number(row.get("end"), f"{label}第 {index} 段终点")
        if end <= start or end > duration + 1e-6:
            raise VirtualPreviewError(f"{label}第 {index} 段必须位于原视频时长内", code="VIRTUAL_PREVIEW_RANGES_INVALID")
        if row.get("enabled", True) is not True:
            continue
        result.append({"start": start, "end": min(end, duration)})
    return output_cuts.merge_ranges(result)


def _snapshot(value: dict | None, duration: float) -> dict | None:
    if not isinstance(value, dict):
        return None
    saved_duration = value.get("source_duration")
    if type(saved_duration) not in (int, float) or not math.isfinite(saved_duration) or saved_duration <= 0:
        saved_duration = duration
    # 旧快照的范围按其历史时长校验；调用方随后比较历史/当前时长，不能
    # 直接用当前时长覆盖掉证据，否则换源后可能错误映射。
    ranges = _intervals(value.get("ranges", []), float(saved_duration))
    return {
        "source_duration": float(saved_duration),
        "source_fingerprint": value.get("source_fingerprint"),
        "ranges": ranges,
        "output_duration": round(float(saved_duration) - sum(x["end"] - x["start"] for x in ranges), 6),
    }


def _contains(ranges: list[dict], start: float, end: float) -> bool:
    return any(start >= row["start"] - 1e-6 and end <= row["end"] + 1e-6 for row in ranges)


def _subtract_intervals(ranges: list[dict], removed: list[dict]) -> list[dict]:
    """返回一组保留区间在某个成片快照上的可用输出位置。"""
    result = []
    for source in ranges:
        cursor = source["start"]
        for cut in removed:
            if cut["end"] <= cursor:
                continue
            if cut["start"] >= source["end"]:
                break
            if cut["start"] > cursor:
                result.append({"start": cursor, "end": min(cut["start"], source["end"])})
            cursor = max(cursor, cut["end"])
            if cursor >= source["end"]:
                break
        if cursor < source["end"] - 1e-6:
            result.append({"start": cursor, "end": source["end"]})
    return result


def _map_source_to_media(cuts: dict | None, start: float, end: float) -> list[tuple[float, float, float, float]]:
    """将源区间映射为媒介文件时间区间，删除内部的部分不可用。"""
    source_segments = [{"start": 0.0, "end": float("inf"), "output_start": 0.0}]
    if cuts is not None:
        source_segments = []
        cursor = 0.0
        output = 0.0
        for cut in cuts.get("ranges", []):
            if cut["start"] > cursor + 1e-6:
                source_segments.append({"start": cursor, "end": cut["start"], "output_start": output})
                output += cut["start"] - cursor
            cursor = max(cursor, cut["end"])
        if cursor < cuts["source_duration"] - 1e-6:
            source_segments.append({"start": cursor, "end": cuts["source_duration"], "output_start": output})
    result = []
    for row in source_segments:
        left, right = max(start, row["start"]), min(end, row["end"])
        if right <= left + 1e-6:
            continue
        result.append((round(left, 6), round(right, 6),
                       round(row["output_start"] + left - row["start"], 6),
                       round(row["output_start"] + right - row["start"], 6)))
    return result


def _input_seek_offsets(segments: list[dict]) -> dict[str, float]:
    """为每个输入取最早片段前一秒的快速定位点。

    原先所有输入都从 0 秒解码，再由 ``trim`` 丢弃前面的帧；长视频尾部
    的 10–20 秒试听因此会等待完整解码。输入侧 ``-ss`` 只把解码起点移到
    目标附近，保留一秒缓冲供关键帧精确寻址，不改变输出时间坐标。
    """
    offsets: dict[str, float] = {}
    for row in segments:
        path = str(row.get("path") or "")
        value = row.get("media_start")
        if not path or type(value) not in (int, float) or not math.isfinite(value):
            continue
        candidate = max(0.0, float(value) - SEEK_MARGIN_SECONDS)
        offsets[path] = min(offsets.get(path, candidate), candidate)
    return offsets


@lru_cache(maxsize=32)
def _hash_file(path_text: str, size: int, modified: int) -> str:
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_hash(path: Path) -> tuple[int, str]:
    try:
        stat = path.stat()
        if not path.is_file() or path.is_symlink() or path.is_junction():
            raise OSError("not regular file")
    except OSError as exc:
        raise VirtualPreviewError("恢复来源文件不存在或不是普通文件", code="VIRTUAL_PREVIEW_MEDIA_MISSING") from exc
    return stat.st_size, _hash_file(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _probe(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,width,height,avg_frame_rate",
             "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False)
        if result.returncode:
            raise ValueError(result.stderr)
        raw = json.loads(result.stdout or "{}")
        streams = raw.get("streams") or []
        video = next((row for row in streams if row.get("codec_type") == "video"), None)
        if not video:
            raise ValueError("video missing")
        duration = float((raw.get("format") or {}).get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration missing")
        return {"duration": duration, "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                "frame_rate": str(video.get("avg_frame_rate") or ""),
                "has_audio": any(row.get("codec_type") == "audio" for row in streams)}
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise VirtualPreviewError("无法读取试听来源的视频流信息，请检查文件和 FFmpeg", code="VIRTUAL_PREVIEW_MEDIA_INVALID") from exc


def _numeric_frame_rate(value) -> float | None:
    """公开数值帧率，前端不必把 30000/1001 当作普通小数字符串。"""
    try:
        rate = float(Fraction(str(value)))
        return rate if math.isfinite(rate) and rate > 0 else None
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _immutable_source_identity(saved: dict) -> tuple[dict, float | None]:
    """从成功成片快照读取制作时的原视频身份，绝不读取当前编辑草稿。"""
    sentence_inputs = saved.get("sentence_inputs")
    if not isinstance(sentence_inputs, dict):
        return {}, None
    global_inputs = sentence_inputs.get("global_inputs")
    source = global_inputs.get("source") if isinstance(global_inputs, dict) else None
    duration = sentence_inputs.get("duration")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        duration = None
    return source if isinstance(source, dict) else {}, duration


def _validated_artifact_cuts(deps: Dependencies, path: Path, saved: dict,
                             source_duration: float, source_fingerprint: str) -> tuple[dict, dict]:
    """只用该文件的不可变快照还原时间轴。

    ``output_cuts=None`` 表示制作时没有删除片段，和今天编辑器里的删除
    清单无关。旧成片即使字幕或参数已待更新，只要原视频身份、实际文件和
    时间轴一致，仍可用作剪辑基准。缺少历史证据时拒绝猜测。
    """
    if not isinstance(saved, dict):
        raise VirtualPreviewError("所选成片的制作快照格式无效", code="VIRTUAL_PREVIEW_VERSION_INVALID")
    immutable_source, historical_duration = _immutable_source_identity(saved)
    saved_cuts = saved.get("output_cuts")
    if "output_cuts" not in saved:
        sentence_inputs = saved.get("sentence_inputs") or {}
        global_inputs = sentence_inputs.get("global_inputs") if isinstance(sentence_inputs, dict) else None
        if isinstance(global_inputs, dict) and "output_cuts" in global_inputs:
            saved_cuts = global_inputs["output_cuts"]
        else:
            raise VirtualPreviewError("旧成片缺少制作时的剪辑快照，无法安全映射；请重新生成该成片",
                                      code="VIRTUAL_PREVIEW_CUTS_SNAPSHOT_MISSING")
    if saved_cuts is not None and not isinstance(saved_cuts, dict):
        raise VirtualPreviewError("所选成片的剪辑快照格式无效", code="VIRTUAL_PREVIEW_VERSION_INVALID")
    if immutable_source.get("sha256"):
        source_path = resolve_video(deps.project, deps.root)
        if source_path is None:
            raise VirtualPreviewError("原视频文件不存在", code="VIRTUAL_PREVIEW_SOURCE_UNAVAILABLE")
        source_size, source_hash = _path_hash(source_path)
        if (source_hash != immutable_source["sha256"] or
                (immutable_source.get("size") is not None and source_size != immutable_source["size"])):
            raise VirtualPreviewError("所选成片制作时使用的原视频已变化，无法安全映射时间轴",
                                      code="VIRTUAL_PREVIEW_BASE_CHANGED")
        identity_source = "immutable_source_hash"
    elif isinstance(saved_cuts, dict) and saved_cuts.get("source_fingerprint") == source_fingerprint:
        identity_source = "immutable_cut_fingerprint"
    else:
        raise VirtualPreviewError("旧成片缺少可核对的原视频身份，无法安全映射；请重新生成该成片",
                                  code="VIRTUAL_PREVIEW_CUTS_SNAPSHOT_MISSING")
    if historical_duration is not None and abs(historical_duration - source_duration) > 0.08:
        raise VirtualPreviewError("所选成片制作时的原视频时长已变化", code="VIRTUAL_PREVIEW_BASE_CHANGED")
    if saved_cuts is None:
        cuts = {"source_duration": historical_duration or source_duration,
                "source_fingerprint": source_fingerprint, "ranges": [],
                "output_duration": historical_duration or source_duration}
        snapshot_source = "immutable_uncut"
    else:
        cuts = _snapshot(saved_cuts, historical_duration or source_duration)
        if abs(cuts["source_duration"] - source_duration) > 0.08:
            raise VirtualPreviewError("所选成片剪辑快照的原视频时长已变化", code="VIRTUAL_PREVIEW_BASE_CHANGED")
        # 字节摘要已确认同源时，文件路径或修改时间改变产生的新指纹不应
        # 阻止使用旧成片；仅为本次映射绑定当前源指纹，不改历史快照。
        cuts["source_fingerprint"] = source_fingerprint
        snapshot_source = "immutable"
    actual_media = _probe(path)
    actual_duration = actual_media["duration"]
    if abs(actual_duration - cuts["output_duration"]) > 0.08:
        raise VirtualPreviewError("所选成片文件时长与制作时剪辑快照不一致，无法安全映射；请重新生成该成片",
                                  code="VIRTUAL_PREVIEW_CUTS_SNAPSHOT_MISMATCH")
    return cuts, {"cut_snapshot_source": snapshot_source,
                  "source_identity": identity_source, "actual_duration": actual_duration,
                  'frame_rate': _numeric_frame_rate(actual_media.get('frame_rate'))}


def _version_media(deps: Dependencies, version_id: str, *, verify_hash=True) -> tuple[Path, dict, dict]:
    records = assembly_versions.results(deps.project.uid)
    version = {"id": version_id}
    record = records.get(version_id)
    if record is None:
        raise VirtualPreviewError("所选成片版本没有成功产物", code="VIRTUAL_PREVIEW_VERSION_MISSING")
    artifact = assembly_versions.result_artifact(deps, version, records)
    if artifact is None:
        raise VirtualPreviewError("所选成片文件已不存在", code="VIRTUAL_PREVIEW_MEDIA_MISSING")
    path = deps.path(artifact.step_key, artifact.name)
    if not path or not path.is_file() or path.stat().st_size != artifact.size:
        raise VirtualPreviewError("所选成片文件已变化或缺失", code="VIRTUAL_PREVIEW_MEDIA_CHANGED")
    if verify_hash:
        size, digest = _path_hash(path)
        if size != artifact.size or digest != record.artifact_hash:
            raise VirtualPreviewError("所选成片哈希已变化，不能安全试听", code="VIRTUAL_PREVIEW_MEDIA_CHANGED")
    source = output_cuts.source_context(deps)
    source_duration = source.get("duration")
    source_fingerprint = source.get("source_fingerprint")
    if not source_duration or not source_fingerprint:
        raise VirtualPreviewError("原视频尚未就绪或无法读取时长", code="VIRTUAL_PREVIEW_SOURCE_UNAVAILABLE")
    try:
        saved = json.loads(record.snapshot or "{}")
    except (TypeError, ValueError) as exc:
        raise VirtualPreviewError("所选成片缺少有效制作快照，无法映射原片时间轴", code="VIRTUAL_PREVIEW_VERSION_INVALID") from exc
    cuts, snapshot_meta = _validated_artifact_cuts(
        deps, path, saved, float(source_duration), source_fingerprint)
    return path, cuts, {"version_id": version_id, "artifact_id": artifact.id, "artifact_hash": record.artifact_hash,
                        **snapshot_meta}


def context(uid: str, version_id: str | None = None) -> dict:
    """读取可试听文件的真实剪辑基准，与当前未生成的删除清单分开。"""
    project = crud.get_project(uid)
    if project is None:
        raise VirtualPreviewError("项目不存在", code="PROJECT_NOT_FOUND")
    deps = Dependencies(project)
    source = output_cuts.source_context(deps)
    result = {"source_fingerprint": source.get("source_fingerprint"), "duration": source.get("duration"),
              "artifact_cuts": None, "artifact_validation": {"available": False, "valid": False}}
    try:
        version_id = _safe_id(version_id, "当前成片")
        if not source.get("source_fingerprint") or not source.get("duration"):
            raise VirtualPreviewError("原视频尚未就绪", code="VIRTUAL_PREVIEW_SOURCE_UNAVAILABLE")
        if version_id:
            _path, cuts, metadata = _version_media(deps, version_id)
        else:
            cuts = {"source_duration": source["duration"], "source_fingerprint": source["source_fingerprint"],
                    "ranges": [], "output_duration": source["duration"]}
            metadata = {"cut_snapshot_source": "source", "source_identity": "current_source"}
        result["artifact_cuts"] = cuts
        result["artifact_validation"] = {"available": True, "valid": True,
            "message": "文件与原视频身份、实际剪辑时间轴已核对；设置待更新不影响剪辑试听。", **metadata}
    except VirtualPreviewError as exc:
        result["artifact_validation"].update(message=exc.message, code=exc.code)
    return result


def _build_plan(uid: str, body: dict) -> dict:
    if not isinstance(body, dict):
        raise VirtualPreviewError("虚拟试听请求格式无效")
    project = crud.get_project(uid)
    if project is None:
        raise VirtualPreviewError("项目不存在", code="PROJECT_NOT_FOUND")
    root = crud.project_dir_by_project(project).resolve()
    deps = Dependencies(project)
    context = output_cuts.source_context(deps)
    source_fp = context.get("source_fingerprint")
    duration = float(context.get("duration") or 0)
    if not source_fp or duration <= 0:
        raise VirtualPreviewError("原视频尚未就绪或无法读取时长", code="VIRTUAL_PREVIEW_SOURCE_UNAVAILABLE")
    requested_fp = body.get("source_fingerprint") or (body.get("draft") or {}).get("source_fingerprint")
    if requested_fp and requested_fp != source_fp:
        raise VirtualPreviewError("原视频已变化，请刷新删除/恢复清单后重试", code="VIRTUAL_PREVIEW_SOURCE_CHANGED")
    source_path = resolve_video(project, root)
    if source_path is None or not source_path.is_file():
        raise VirtualPreviewError("原视频文件不存在", code="VIRTUAL_PREVIEW_SOURCE_UNAVAILABLE")

    base_id = _safe_id(body.get("version_id") or body.get("base_version_id"), "当前成片")
    if base_id:
        base_path, base_cuts, base_meta = _version_media(deps, base_id)
        if base_cuts.get("source_fingerprint") and base_cuts["source_fingerprint"] != source_fp:
            raise VirtualPreviewError("当前成片与原视频不是同一版本，无法映射时间轴", code="VIRTUAL_PREVIEW_BASE_CHANGED")
        if abs(float(base_cuts.get("source_duration", duration)) - duration) > 0.08:
            raise VirtualPreviewError("当前成片记录的原片时长与当前原视频不一致，无法映射时间轴", code="VIRTUAL_PREVIEW_BASE_CHANGED")
    else:
        base_path, base_cuts, base_meta = source_path, {"source_duration": duration, "ranges": [], "source_fingerprint": source_fp}, None

    donor_raw = body.get("donor") or {"kind": "source"}
    if not isinstance(donor_raw, dict) or donor_raw.get("kind") not in {"source", "version"}:
        raise VirtualPreviewError("恢复来源必须是原视频或其他成片", code="VIRTUAL_PREVIEW_DONOR_INVALID")
    donor_id = _safe_id(donor_raw.get("version_id"), "恢复成片")
    if donor_raw["kind"] == "version":
        if not donor_id:
            raise VirtualPreviewError("请选择恢复来源成片", code="VIRTUAL_PREVIEW_DONOR_INVALID")
        donor_path, donor_cuts, donor_meta = _version_media(deps, donor_id)
        if donor_cuts.get("source_fingerprint") and donor_cuts["source_fingerprint"] != source_fp:
            raise VirtualPreviewError("恢复成片与原视频不是同一版本，无法安全恢复", code="VIRTUAL_PREVIEW_DONOR_CHANGED")
        if abs(float(donor_cuts.get("source_duration", duration)) - duration) > 0.08:
            raise VirtualPreviewError("恢复成片记录的原片时长与当前原视频不一致，无法安全恢复", code="VIRTUAL_PREVIEW_DONOR_CHANGED")
    else:
        donor_path, donor_cuts, donor_meta = source_path, {"source_duration": duration, "ranges": [], "source_fingerprint": source_fp}, None

    preview_window = body.get("preview_range") or body.get("range")
    window_start, window_end = 0.0, duration
    if preview_window is not None:
        if not isinstance(preview_window, dict):
            raise VirtualPreviewError("试听范围格式无效", code="VIRTUAL_PREVIEW_RANGE_INVALID")
        window_start = _number(preview_window.get("start"), "试听起点")
        window_end = _number(preview_window.get("end"), "试听终点")
        if window_end <= window_start or window_end > duration + 1e-6:
            raise VirtualPreviewError("试听范围必须位于原视频时长内", code="VIRTUAL_PREVIEW_RANGE_INVALID")
        window_end = min(window_end, duration)

    draft = body.get("draft") if isinstance(body.get("draft"), dict) else body
    draft_enabled = draft.get("enabled", True)
    if type(draft_enabled) is not bool:
        raise VirtualPreviewError("删除清单启用状态无效", code="VIRTUAL_PREVIEW_RANGES_INVALID")
    draft_ranges = _intervals(draft.get("ranges", []), duration) if draft_enabled else []
    base_ranges = _intervals(base_cuts.get("ranges", []), duration)
    boundaries = {window_start, window_end}
    for row in base_ranges + draft_ranges:
        if row["end"] > window_start and row["start"] < window_end:
            boundaries.update((max(window_start, row["start"]), min(window_end, row["end"])))
    ordered = sorted(boundaries)
    segments = []
    virtual_cursor = 0.0
    for start, end in zip(ordered, ordered[1:]):
        if end <= start + 1e-6 or _contains(draft_ranges, start, end):
            continue
        use_donor = _contains(base_ranges, start, end)
        path, cuts, kind, version_id = (donor_path, donor_cuts, donor_raw["kind"], donor_id) if use_donor else (base_path, base_cuts, "target" if base_id else "source", base_id)
        media_meta = donor_meta if use_donor else base_meta
        media = _map_source_to_media(cuts, start, end)
        mapped_source = sum(item[1] - item[0] for item in media)
        if not media or mapped_source < end - start - 1e-6:
            raise VirtualPreviewError(f"{start:.3f}–{end:.3f} 秒的恢复来源缺少完整片段，请改选原视频或其他成片",
                                      code="VIRTUAL_PREVIEW_SEGMENT_UNAVAILABLE")
        for source_start, source_end, media_start, media_end in media:
            if media_end <= media_start + 1e-6:
                continue
            length = media_end - media_start
            segments.append({"source_start": source_start, "source_end": source_end,
                             "media_kind": kind, "media_version_id": version_id,
                             "media_start": media_start, "media_end": media_end,
                             "virtual_start": round(virtual_cursor, 6),
                             "virtual_end": round(virtual_cursor + length, 6),
                             "output_start": round(virtual_cursor, 6),
                             "output_end": round(virtual_cursor + length, 6),
                             "artifact_hash": (media_meta or {}).get("artifact_hash"),
                             "path": str(path), "available": True})
            virtual_cursor += length
    total = round(sum(x["media_end"] - x["media_start"] for x in segments), 6)
    if not segments or total <= 0:
        raise VirtualPreviewError("当前删除清单没有可试听的保留片段", code="VIRTUAL_PREVIEW_EMPTY")
    if len(segments) > MAX_SEGMENTS:
        raise VirtualPreviewError(f"试听片段过多（{len(segments)} 段），请先合并相邻删除范围", code="VIRTUAL_PREVIEW_TOO_MANY_SEGMENTS")
    if total > MAX_PREVIEW_SECONDS:
        raise VirtualPreviewError("虚拟试听最长支持 2 小时，请缩小试听范围后重试", code="VIRTUAL_PREVIEW_TOO_LONG")
    return {"project": project, "root": root, "source_fingerprint": source_fp, "source_duration": duration,
            "source_validation": {"status": "verified", "fingerprint": source_fp,
                                  "duration": duration, "message": "原视频指纹与时长已核对"},
            "base": base_meta, "donor": donor_meta, "segments": segments, "duration": total,
            "draft_ranges": draft_ranges, "base_ranges": base_ranges,
            "preview_range": {"start": window_start, "end": window_end}}


def _metadata_path(root: Path, task_id: str) -> Path:
    return root / "artifacts" / "output_cuts" / "virtual_preview" / f"{task_id}.json"


def _output_path(root: Path, task_id: str) -> Path:
    return root / "artifacts" / "output_cuts" / "virtual_preview" / f"{task_id}.mp4"


def _prepare_render(plan: dict) -> None:
    """临时试听复用正式接缝编码，但不建立成片计划、媒体副本或数据库登记。"""
    from dubforge.pipeline import segment_cut
    inputs = {}
    for row in plan['segments']:
        path = Path(row['path']).resolve()
        key = str(path)
        if key not in inputs:
            size, digest = _path_hash(path)
            stat = path.stat()
            inputs[key] = dict(path=key, size=size, modified=stat.st_mtime_ns, sha256=digest, **_probe(path))
        row['path'] = key
    plan['media_inputs'] = list(inputs.values())
    first = plan['media_inputs'][0]
    plan['encoding'] = dict(method='full_encode', copied_seconds=0., encoded_seconds=plan['duration'],
        output_duration=plan['duration'], audio_mode='encode_once' if first['has_audio'] else 'none',
        fallback_reason='恢复片段或混合来源需要统一编码。', fallback_code='SEGMENT_CUT_MIXED_SOURCES')
    kind = 'target' if plan.get('base') else 'source'
    pure = len(inputs) == 1 and all(row.get('media_kind') == kind for row in plan['segments'])
    if pure:
        keep = output_cuts.merge_ranges([dict(start=row['media_start'], end=row['media_end']) for row in plan['segments']])
        analysis = segment_cut.analyze(Path(first['path']), keep)
        if analysis.get('keep_ranges'):
            updated, cursor = [], 0.
            for row in plan['segments']:
                for kept in analysis['keep_ranges']:
                    left, right = max(row['media_start'], kept['start']), min(row['media_end'], kept['end'])
                    if right <= left + 1e-9:
                        continue
                    item = copy.deepcopy(row)
                    item.update(media_start=left, media_end=right,
                        source_start=round(row['source_start'] + left - row['media_start'], 9),
                        source_end=round(row['source_start'] + right - row['media_start'], 9),
                        virtual_start=cursor, output_start=cursor,
                        virtual_end=cursor + right - left, output_end=cursor + right - left)
                    cursor += right - left
                    updated.append(item)
            plan.update(segments=updated, duration=analysis['output_duration'])
        plan['encoding'] = segment_cut.summary(analysis)
        plan['encoding']['audio_mode'] = 'encode_once' if first['has_audio'] else 'none'
        if analysis['method'] == 'smart_cut':
            plan['smart_cut'] = analysis
            # 复制主体要求接缝 PPS 与基片一致；沿用码流自己记录的 CRF，
            # 不读取当前制作配置，也不把试听默认 CRF 强加给旧成片。
            try:
                crf = float(analysis['info'].get('x264_options', {}).get('crf', 23))
                plan['smart_crf'] = crf if math.isfinite(crf) and 0 <= crf <= 51 else 23.
            except (TypeError, ValueError):
                plan['smart_crf'] = 23.
    plan['applied_preview_range'] = dict(start=plan['segments'][0].get('source_start'),
                                         end=plan['segments'][-1].get('source_end'))
    # 文件版本与字节身份同时绑定；备注/编辑器行 id 不影响同一试听的缓存。
    key = dict(renderer=PREVIEW_RENDER_VERSION, smart_renderer=segment_cut.VERSION,
        source=plan['source_fingerprint'], window=plan['preview_range'], inputs=plan['media_inputs'],
        segments=plan['segments'], fps=(plan.get('smart_cut') or {}).get('info', {}).get('fps'),
        full_crf=23, smart_crf=plan.get('smart_crf'), audio_bitrate='128k-full-192k-smart')
    plan['cache_key'] = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _verify_inputs(plan: dict) -> None:
    for media in plan.get('media_inputs', []):
        path = Path(media['path'])
        try:
            stat = path.stat()
        except OSError as exc:
            raise VirtualPreviewError('试听来源文件已不存在，请重新检查后生成', code='VIRTUAL_PREVIEW_MEDIA_CHANGED') from exc
        if (stat.st_size != media['size'] or stat.st_mtime_ns != media['modified']
                or _path_hash(path)[1] != media['sha256']):
            raise VirtualPreviewError('试听来源在检查后发生变化，请重新生成', code='VIRTUAL_PREVIEW_MEDIA_CHANGED')


def _cached(root: Path, uid: str, cache_key: str) -> dict | None:
    folder = root / 'artifacts' / 'output_cuts' / 'virtual_preview'
    if not folder.is_dir():
        return None
    for sidecar in folder.glob('*.json'):
        try:
            value = json.loads(sidecar.read_text(encoding='utf-8'))
            if (value.get('uid') != uid or value.get('status') != 'done' or value.get('cache_key') != cache_key
                    or not isinstance(value.get('task_id'), str) or not _IDENTIFIER.fullmatch(value['task_id'])):
                continue
            output = _output_path(root, value['task_id'])
            stat = output.stat()
            if (not output.is_file() or output.is_symlink() or output.is_junction()
                    or stat.st_size <= 0 or value.get('output_name') != output.name
                    or value.get('output_version') != dict(size=stat.st_size, modified=stat.st_mtime_ns)):
                continue
            if 'frame_rate' not in value:
                value['frame_rate'] = _numeric_frame_rate(_probe(output).get('frame_rate'))
            with _lock:
                cached = _tasks.get(value['task_id']) or {**value, 'root': str(root)}
                cached.update(cache_hit=True, last_used_at=_now(), message='复用已完成的虚拟试听',
                              frame_rate=value.get('frame_rate'))
                _tasks[value['task_id']] = cached
                _persist(cached)
            return cached
        except (OSError, ValueError, TypeError):
            continue
    return None


def _cleanup_expired(root: Path) -> None:
    """回收超过保留时间的试听片，避免拖动/重试把磁盘逐渐填满。"""
    folder = root / "artifacts" / "output_cuts" / "virtual_preview"
    if not folder.is_dir():
        return
    cutoff = time.time() - TASK_TTL_SECONDS
    for sidecar in folder.glob("*.json"):
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            created = dt.datetime.fromisoformat(str(raw.get("last_used_at") or raw.get("created_at", "")).replace("Z", "+00:00")).timestamp()
            if created > cutoff or raw.get("status") in {"queued", "running"}:
                continue
            task_id = sidecar.stem
            with _lock:
                if task_id in _tasks and _tasks[task_id].get("status") in {"queued", "running"}:
                    continue
            _output_path(root, task_id).unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            continue


def _persist(task: dict) -> None:
    value = {key: copy.deepcopy(value) for key, value in task.items() if key not in {"cancel", "plan", "future"}}
    atomic_write_bytes(_metadata_path(Path(task["root"]), task["task_id"]),
                       (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _set(task_id: str, **changes):
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.update(changes)
        _persist(task)


def _stop_process(proc: subprocess.Popen) -> None:
    """终止本任务及其包装器子进程，取消不依赖 stdout 再次输出。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # Windows 的 ffmpeg 入口可能是包装器；只终止父进程会留下占用
        # stdout 的编码子进程，使阻塞 readline 一直不能返回。
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


class _PreviewContext:
    """让接缝编码复用同一取消/进度合同，每个子进程都登记到试听任务。"""
    def __init__(self, task_id: str):
        self.task_id = task_id

    def check_cancel(self):
        if _tasks[self.task_id]['cancel'].is_set():
            raise VirtualPreviewError('虚拟试听已取消', code='VIRTUAL_PREVIEW_CANCELLED')

    def check_pause(self):
        self.check_cancel()

    def log(self, _message):
        pass

    def report_execution(self, stage, label, **fields):
        fixed = {'checking_render_resources': 2., 'joining_cut_video': 78.,
                 'cutting_audio_once': 86., 'validating_smart_cut': 96.}
        progress = fixed.get(stage)
        if stage in {'encoding_cut_seams', 'copying_complete_gops'}:
            progress = 5 + 70 * float(fields.get('completed', 0)) / max(1, float(fields.get('total', 1)))
        changes = dict(stage=stage, message=label)
        if progress is not None:
            changes['progress'] = progress
        if fields.get('detail'):
            changes['stage_detail'] = fields['detail']
        _set(self.task_id, **changes)

    def run_subprocess(self, args, *, cwd=None, on_stdout=None):
        self.check_cancel()
        proc = subprocess.Popen([str(arg) for arg in args], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        with _lock:
            _processes[self.task_id] = proc
        try:
            while True:
                self.check_cancel()
                try:
                    stdout, stderr = proc.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            self.check_cancel()
            if on_stdout:
                for line in stdout.splitlines():
                    on_stdout(line)
            return proc.returncode, stdout, stderr
        finally:
            _stop_process(proc)
            with _lock:
                if _processes.get(self.task_id) is proc:
                    _processes.pop(self.task_id, None)


def _complete(task_id: str, plan: dict, tmp: Path, out: Path):
    if _tasks[task_id]['cancel'].is_set():
        raise VirtualPreviewError('虚拟试听已取消', code='VIRTUAL_PREVIEW_CANCELLED')
    if not tmp.is_file() or tmp.stat().st_size <= 0:
        raise VirtualPreviewError('虚拟试听未生成有效文件', code='VIRTUAL_PREVIEW_ENCODE_FAILED')
    _verify_inputs(plan)
    final_media = _probe(tmp)
    if abs(final_media['duration'] - plan['duration']) > .08:
        raise VirtualPreviewError('试听时长与已应用的剪辑映射不一致，请重新检查来源', code='VIRTUAL_PREVIEW_DURATION')
    # 取消与最终成功只在同一短锁内竞争；媒体探测不占用任务锁。
    with _lock:
        if _tasks[task_id]['cancel'].is_set():
            raise VirtualPreviewError('虚拟试听已取消', code='VIRTUAL_PREVIEW_CANCELLED')
        atomic_replace(tmp, out)
        stat = out.stat()
        _set(task_id, status='done', stage='ready', progress=100., message='虚拟试听已生成', finished_at=_now(),
             frame_rate=_numeric_frame_rate(final_media.get('frame_rate')),
             output_name=out.name, output_version=dict(size=stat.st_size, modified=stat.st_mtime_ns),
             output_url=f'/api/projects/{_tasks[task_id]["uid"]}/output-cuts/virtual-preview/{task_id}/raw')


def _run(task_id: str):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return
    plan = task["plan"]
    out = _output_path(Path(task["root"]), task_id)
    tmp = out.with_name(f".tmp_{out.name}")
    proc = None
    try:
        _set(task_id, status="running", stage="encoding", progress=1.0, message="准备虚拟试听片段", started_at=_now())
        if task['cancel'].is_set():
            raise VirtualPreviewError('虚拟试听已取消', code='VIRTUAL_PREVIEW_CANCELLED')
        _verify_inputs(plan)
        if plan.get('smart_cut'):
            from dubforge.pipeline import segment_cut, segment_replace
            try:
                actual = segment_cut.render(_PreviewContext(task_id), Path(plan['media_inputs'][0]['path']),
                                             tmp, plan['smart_cut'], crf=plan.get('smart_crf', 23))
                _set(task_id, encoding=actual)
                _complete(task_id, plan, tmp, out)
                return
            except segment_replace.SegmentReplaceError as exc:
                allowed = {'SEGMENT_REPLACE_UNSUPPORTED', 'SEGMENT_REPLACE_INCOMPATIBLE',
                           'SEGMENT_REPLACE_FRAME_COUNT', 'SEGMENT_REPLACE_TIMESTAMPS', 'SEGMENT_CUT_DURATION'}
                if exc.code not in allowed:
                    raise
                encoding = {**plan['encoding'], 'method': 'full_encode', 'copied_seconds': 0.,
                    'encoded_seconds': plan['duration'], 'fallback_code': exc.code,
                    'fallback_reason': '接缝编码未通过校验，改用完整编码：' + exc.message,
                    'fallback_detail': exc.detail}
                _set(task_id, stage='smart_cut_fallback', message=encoding['fallback_reason'], encoding=encoding)
        paths = []
        for item in plan["segments"]:
            path = Path(item["path"])
            if path not in paths:
                paths.append(path)
        _set(task_id, stage="probing", message="正在读取恢复来源媒体信息")
        probes = {path: _probe(path) for path in paths}
        first = probes[paths[0]]
        audio_flags = {bool(info["has_audio"]) for info in probes.values()}
        if len(audio_flags) > 1:
            raise VirtualPreviewError("当前成片与恢复来源的音轨不一致，无法安全拼接试听；请换用具有相同音轨的来源",
                                      code="VIRTUAL_PREVIEW_AUDIO_MISMATCH")
        has_audio = bool(next(iter(audio_flags), False))
        width, height = first["width"] or 1280, first["height"] or 720
        index = {path: i for i, path in enumerate(paths)}
        seek_offsets = _input_seek_offsets(plan["segments"])
        _set(task_id, stage="seeking", progress=2.0, message="正在定位恢复来源的时间窗口")
        # 进度通过 stdout 传输，stderr 只保留错误；否则长任务的诊断输出可能
        # 填满管道，导致读取 out_time_us 时表现为“任务卡住”。
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]
        for path in paths:
            # 输入侧快速定位，trim 的时间仍按该输入的相对偏移计算。
            command += ["-ss", f"{seek_offsets.get(str(path), 0.0):.6f}", "-i", str(path)]
        filters, labels = [], []
        for i, item in enumerate(plan["segments"]):
            inp = index[Path(item["path"])]
            offset = seek_offsets.get(str(item["path"]), 0.0)
            start, end = item["media_start"] - offset, item["media_end"] - offset
            filters.append(f"[{inp}:v:0]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
            if has_audio:
                filters.append(f"[{inp}:a:0]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
                labels.append(f"[v{i}][a{i}]")
            else:
                labels.append(f"[v{i}]")
        concat = "".join(labels) + f"concat=n={len(labels)}:v=1:a={1 if has_audio else 0}[vout]" + ("[aout]" if has_audio else "")
        filters.append(concat)
        command += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        if has_audio:
            command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
        command += ["-movflags", "+faststart", "-t", f"{plan['duration']:.6f}", str(tmp)]
        # 合并 stderr 避免 ffmpeg 警告管道填满后阻塞，进度和错误仍保存在
        # 有界的日志尾部中；这类试听任务不应因为诊断输出而卡住。
        from dubforge.pipeline.render_resources import bounded_ffmpeg_command
        if task['cancel'].is_set():
            raise VirtualPreviewError('虚拟试听已取消', code='VIRTUAL_PREVIEW_CANCELLED')
        proc = subprocess.Popen(bounded_ffmpeg_command(command), cwd=str(out.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        with _lock:
            _processes[task_id] = proc
        lines: queue.Queue[str] = queue.Queue(maxsize=256)
        reader_done = threading.Event()

        def read_progress():
            try:
                for line in proc.stdout or ():
                    while not task["cancel"].is_set():
                        try:
                            lines.put(line, timeout=.1)
                            break
                        except queue.Full:
                            continue
                    if task["cancel"].is_set():
                        break
            finally:
                reader_done.set()

        threading.Thread(target=read_progress, daemon=True,
                         name=f"virtual-preview-log-{task_id[:8]}").start()
        log_tail: list[str] = []
        while True:
            if task["cancel"].is_set():
                _stop_process(proc)
                raise VirtualPreviewError("虚拟试听已取消", code="VIRTUAL_PREVIEW_CANCELLED")
            try:
                line = lines.get(timeout=.2)
            except queue.Empty:
                if proc.poll() is not None and reader_done.is_set():
                    break
                continue
            if line:
                log_tail.append(line)
                if len(log_tail) > 300:
                    del log_tail[:-300]
                if line.startswith("out_time_us="):
                    try:
                        completed = float(line.split("=", 1)[1]) / 1_000_000
                        _set(task_id, stage="encoding", progress=round(min(99.0, max(1.0, completed / plan["duration"] * 98)), 2), message="正在拼接当前成片与恢复来源")
                    except ValueError:
                        pass
        stderr = "".join(log_tail)
        if task["cancel"].is_set():
            raise VirtualPreviewError("虚拟试听已取消", code="VIRTUAL_PREVIEW_CANCELLED")
        if proc.returncode != 0:
            raise VirtualPreviewError("虚拟试听编码失败，请检查媒体文件和 FFmpeg 日志", code="VIRTUAL_PREVIEW_ENCODE_FAILED", detail=stderr[-2000:])
        _complete(task_id, plan, tmp, out)
    except DubForgeError as exc:
        tmp.unlink(missing_ok=True)
        status = "cancelled" if exc.code == "VIRTUAL_PREVIEW_CANCELLED" else "failed"
        _set(task_id, status=status, stage=status, progress=0.0, message=exc.message, error_code=exc.code, error_detail=exc.detail, finished_at=_now())
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        _set(task_id, status="failed", stage="failed", progress=0.0, message="虚拟试听生成失败", error_code="VIRTUAL_PREVIEW_FAILED", error_detail=str(exc), finished_at=_now())
    finally:
        if proc is not None:
            _stop_process(proc)
        with _lock:
            _processes.pop(task_id, None)


def start(uid: str, body: dict) -> dict:
    plan = _build_plan(uid, body)
    _prepare_render(plan)
    task_id = uuid.uuid4().hex
    root = plan["root"]
    _cleanup_expired(root)
    cached = _cached(root, uid, plan['cache_key'])
    if cached:
        with _lock:
            for old in _tasks.values():
                if old.get('uid') == uid and old.get('status') in {'queued', 'running'}:
                    old['cancel'].set()
        return public(cached)
    task = {"task_id": task_id, "uid": uid, "root": str(root), "status": "queued", "stage": "queued", "progress": 0.0,
            "message": "已排队等待生成虚拟试听", "error_code": "", "error_detail": "", "created_at": _now(),
            "finished_at": None, "output_name": None, "output_url": None, "source_fingerprint": plan["source_fingerprint"],
            "source_duration": plan["source_duration"], "duration": plan["duration"], "base": plan["base"], "donor": plan["donor"],
            "source_validation": plan["source_validation"],
            "preview_range": plan["preview_range"],
            'applied_preview_range': plan['applied_preview_range'],
            'cache_key': plan['cache_key'], 'cache_hit': False, 'last_used_at': _now(),
            'encoding': copy.deepcopy(plan['encoding']),
            "segments": [{k: v for k, v in row.items() if k != "path"} for row in plan["segments"]],
            "cancel": threading.Event(), "plan": plan}
    with _lock:
        # 同一项目只保留最近一次排队/运行的试听，避免用户拖动时不断占用编码器。
        for old_id, old in list(_tasks.items()):
            if old.get("uid") == uid and old.get("status") in {"queued", "running"}:
                old["cancel"].set()
        _tasks[task_id] = task
        _persist(task)
        task["future"] = _executor.submit(_run, task_id)
    return public(task)


def _load(uid: str, task_id: str) -> dict:
    _safe_id(task_id, "试听任务")
    with _lock:
        task = _tasks.get(task_id)
    if task and task.get("uid") == uid:
        return task
    project = crud.get_project(uid)
    if project is None:
        raise VirtualPreviewError("项目不存在", code="PROJECT_NOT_FOUND")
    path = _metadata_path(crud.project_dir_by_project(project), task_id)
    if not path.is_file():
        raise VirtualPreviewError("试听任务不存在或已过期", code="VIRTUAL_PREVIEW_TASK_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VirtualPreviewError("试听任务状态损坏，请重新生成", code="VIRTUAL_PREVIEW_TASK_INVALID") from exc
    if value.get("uid") != uid:
        raise VirtualPreviewError("试听任务不属于当前项目", code="VIRTUAL_PREVIEW_TASK_INVALID")
    # 线程池不跨进程持久化；重启后不能把旧任务伪装成仍在运行。
    if value.get("status") in {"queued", "running"}:
        value.update(status="failed", stage="expired", phase="expired", progress=0.0, percent=0.0,
                     message="服务已重启，旧试听任务已过期，请重新生成", detail="服务已重启，旧试听任务已过期，请重新生成",
                     error_code="VIRTUAL_PREVIEW_EXPIRED", finished_at=_now())
        try:
            atomic_write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
        except OSError:
            pass
    return value


def public(task: dict) -> dict:
    value = {key: copy.deepcopy(value) for key, value in task.items() if key not in {"cancel", "plan", "future", "root"}}
    # ``stage/message`` 是后端内部命名；phase/detail 为步骤面板采用的通用
    # 任务合同，两个字段同时返回，旧前端不需立刻升级。
    value.setdefault("phase", value.get("stage", ""))
    value.setdefault("detail", value.get("message", ""))
    value.setdefault("percent", value.get("progress", 0.0))
    return value


def status(uid: str, task_id: str) -> dict:
    return public(_load(uid, task_id))


def cancel(uid: str, task_id: str) -> dict:
    task = _load(uid, task_id)
    with _lock:
        if task.get("status") in {"queued", "running"} and isinstance(task, dict) and "cancel" in task:
            task["cancel"].set()
            task["message"] = "正在取消虚拟试听"
            _persist(task)
    return public(task)


def media_path(uid: str, task_id: str) -> Path:
    task = _load(uid, task_id)
    if task.get("status") != "done" or not task.get("output_name"):
        raise VirtualPreviewError("试听尚未生成完成", code="VIRTUAL_PREVIEW_NOT_READY")
    project = crud.get_project(uid)
    path = _output_path(crud.project_dir_by_project(project), task_id)
    if not path.is_file() or path.is_symlink() or path.is_junction():
        raise VirtualPreviewError("试听文件已不存在，请重新生成", code="VIRTUAL_PREVIEW_MEDIA_MISSING")
    return path
