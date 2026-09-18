# -*- coding: utf-8 -*-
"""
步骤 8: tts —— 逐句配音
=======================
输入: dubtask/dubtask.json + preprocess 的 vocal.wav (dynamic/stable 参考源)
后端: config tts.backend → indextts25 | gptsovits
句级缓存: artifacts/tts/{id}.wav 存在即跳过 (workbench 采纳语义兼容)
参考解析 (双维度, 移植自工作台验证过的语义):
  preset  → config 路径; stable → vocal 开头切片; dynamic → vocal 按行时间切
短行保护: dynamic 参考时长 < 2s 时回退 stable (短句情感不可靠, 避免漂移)
"""
import json
import subprocess
from pathlib import Path

from dubforge.backends import tts_gptsovits, tts_indextts25
from dubforge.errors import DubForgeError


def _cut(src: Path, dst: Path, start: float, dur: float) -> Path:
    """ffmpeg 切片; 目标存在即复用"""
    if dst.exists() and dst.stat().st_size > 1024:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.2f}",
                    "-t", f"{dur:.2f}", "-ar", "32000", "-ac", "1",
                    "-f", "wav", str(dst)], check=True, capture_output=True)
    return dst


def _resolve_ref(ctx, mode: str, custom: str, line: dict,
                 vocal: Path, cache_dir: Path, tag: str) -> Path:
    if mode == "preset":
        p = Path(custom or "")
        if not p.exists():
            raise DubForgeError(
                f"{tag} 参考音频不存在: {p}", code="REF_MISSING",
                suggestion="请在设置页填写有效的参考音频路径, 或切换 stable/dynamic 模式。")
        return p
    if mode == "stable":
        return _cut(vocal, cache_dir / "stable_ref.wav", 0.0, 8.0)
    if mode == "dynamic":
        dur = max(0.0, line["end"] - line["start"])
        if dur < 2.0:
            # 短行回退 stable (每句原声太短, 克隆不可靠)
            return _cut(vocal, cache_dir / "stable_ref.wav", 0.0, 8.0)
        return _cut(vocal, cache_dir / f"ref_{tag}_{line['id']}.wav",
                    line["start"], dur)
    raise DubForgeError(f"未知参考模式: {mode}", code="REF_MODE")


def synth_line(project, line: dict, pdir: Path, force: bool = False,
               log=lambda m: None) -> dict:
    """合成单句 (工作台与批量步骤共用)。返回 {status, dur}"""
    from dubforge import config
    from dubforge.domain import crud

    out = pdir / "artifacts" / "tts" / f"{line['id']}.wav"
    if out.exists() and not force:
        return {"status": "skipped", "file": out}

    backend = config.get("tts.backend", "indextts25")
    out.parent.mkdir(parents=True, exist_ok=True)

    if backend == "indextts25":
        cfg = config.get("tts.indextts25") or {}
        vocal = pdir / "audio" / "vocal.wav"
        cache_dir = pdir / "artifacts" / "tts"
        timbre = _resolve_ref(None, cfg.get("timbre_mode", "preset"),
                              cfg.get("timbre_audio", ""), line, vocal,
                              cache_dir, "timbre")
        emo_mode = cfg.get("emo_mode", "preset")
        emo_ref = None
        if emo_mode in ("preset", "stable", "dynamic"):
            emo_ref = _resolve_ref(None, emo_mode, cfg.get("emo_audio", ""),
                                   line, vocal, cache_dir, "emo")
        tts_indextts25.synth(
            cfg.get("url", "http://127.0.0.1:7860/"),
            line["text"], out, cfg.get("lang", "ZH"),
            timbre_ref=timbre, emo_mode=emo_mode, emo_ref=emo_ref,
            emo_weight=float(cfg.get("emo_weight", 1.0)),
            emo_vector=cfg.get("emo_vector") or [],
            duration_factor=float(cfg.get("duration_factor", 1.0)),
            log=log)
    elif backend == "gptsovits":
        gcfg = config.get("tts.gptsovits") or {}
        tts_gptsovits.synth(gcfg.get("url", "http://127.0.0.1:9880/"),
                            line["text"], out, "zh",
                            character=gcfg.get("character"), log=log)
    else:
        raise DubForgeError(f"未知 TTS 后端: {backend}", code="TTS_BACKEND")

    crud.add_artifact(project.id, "tts", "audio", f"{line['id']}.wav",
                      out, str(out.relative_to(pdir)))
    return {"status": "synthesized", "file": out}


def run(ctx) -> None:
    task = json.loads(ctx.upstream("dubtask", "dubtask.json").read_text(encoding="utf-8"))
    pdir = ctx.pdir
    done = skipped = failed = 0
    for li, line in enumerate(task["lines"]):
        ctx.check_pause()
        ctx.check_cancel()
        ctx.report_progress(li / max(1, len(task["lines"])) * 99)
        if not line["text"].strip():
            continue
        try:
            r = synth_line(ctx.project, line, pdir, force=False, log=ctx.log)
            if r["status"] == "skipped":
                skipped += 1
            else:
                done += 1
            ctx.log(f"句 {line['id']}: {r['status']}")
        except DubForgeError:
            raise
        except Exception as e:  # noqa: BLE001 单句失败不阻断整批 (重跑补)
            failed += 1
            ctx.log(f"句 {line['id']} 失败: {type(e).__name__}: {e}")

    if failed and done == 0 and skipped == 0:
        raise DubForgeError(f"全部 {failed} 句合成失败", code="TTS_ALL_FAILED",
                            suggestion="查看逐句日志定位首错; 常见: 服务未启动/参考音频无效。")
    ctx.log(f"✅ 逐句配音完成: 新合成 {done} / 复用 {skipped} / 失败 {failed}")
