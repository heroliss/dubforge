# -*- coding: utf-8 -*-
"""
步骤 9: assemble —— 成片合成
============================
两阶段:
  A) 铺轨: 逐句配音按时间轴铺到与视频等长的完整人声轨 (audio/dub_full.wav),
     峰值归一 + 句间淡入淡出; loudnorm 开启时再做 EBU R128 单遍归一
  B) 封装: ffmpeg 混音 —— 背景音量 + 人声 ducking (sidechaincompress,
     说话时自动压低背景) + 可选字幕烧录; 视频流直拷不重编码

产物: output/final_YYYYmmdd_HHMM.mp4 (时间戳归档, 多版本并存) + 伴生 trans.srt
"""
import datetime
import json
import os
import subprocess
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.utils import audio as au


def _ffprobe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _mix_timeline(ctx, pdir: Path, total: float) -> Path:
    """阶段 A: 铺轨 + 归一 → audio/dub_full.wav"""
    task = json.loads(ctx.upstream("dubtask", "dubtask.json").read_text(encoding="utf-8"))
    entries = []
    for line in task["lines"]:
        ctx.check_cancel()
        wav = pdir / "artifacts" / "tts" / f"{line['id']}.wav"
        if not wav.exists():
            ctx.log(f"⚠ 句 {line['id']} 无配音文件, 跳过")
            continue
        entries.append((line["start"], au.load_mono_32k(wav)))

    if not entries:
        raise DubForgeError("没有任何已合成的配音句", code="NO_DUB_LINES",
                            suggestion="请先完成 tts 步骤或在工作台合成部分句子。")
    ctx.log(f"铺轨: {len(entries)} 句 → {total:.1f}s 人声轨")
    # 句首尾 10ms 淡入淡出, 消除逐句拼接处的咔哒声 (au.fade_in_out)
    entries = [(s, au.fade_in_out(a)) for s, a in entries]
    mixed = au.mix_over_timeline(entries, total)
    mixed = au.peak_normalize(mixed, -3.0)

    out = pdir / "audio" / "dub_full.wav"
    out.parent.mkdir(exist_ok=True)
    import soundfile as sf
    tmp = out.with_suffix(".wav.tmp")
    # .tmp 后缀无法推断格式, 显式指定 WAV (与 ffmpeg 原子写同理)
    sf.write(str(tmp), mixed, au.SR, subtype="PCM_16", format="WAV")
    os.replace(tmp, out)

    # EBU R128 响度归一 (单遍, 足够配音场景)
    if ctx.cfg("assemble.loudnorm", True):
        norm = out.with_suffix(".tmp_norm.wav")
        rc, _, err = ctx.run_subprocess(
            ["ffmpeg", "-y", "-i", str(out),
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ar", "32000", "-ac", "1", "-f", "wav", str(norm)])
        if rc != 0:
            raise DubForgeError("响度归一失败 (ffmpeg)", code="FFMPEG_FAIL",
                                detail=err[-1500:])
        os.replace(norm, out)
        ctx.log("响度归一 (EBU R128, I=-16) 完成")
    ctx.register_file("dub_full.wav", out, "audio")
    return out


def _mux(ctx, pdir: Path, video: Path, dub: Path, out: Path) -> None:
    """阶段 B: ffmpeg 封装 (背景/ducking/烧录)"""
    bg = pdir / "audio" / "background.wav"
    bgm_vol = float(ctx.cfg("assemble.bgm_volume", 0.3))
    ducking = bool(ctx.cfg("assemble.ducking", True))
    burn = bool(ctx.cfg("assemble.burn_subtitles", False))
    crf = int(ctx.cfg("assemble.crf", 18))
    has_bg = bg.exists()

    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(dub)]
    filters = []
    if has_bg:
        cmd += ["-i", str(bg)]
        if ducking:
            # 背景作为主输入被人声 (sidechain) 压低: 说话时背景自动避让
            filters.append(
                f"[2:a]volume={bgm_vol}[bg];"
                f"[bg][1:a]sidechaincompress=threshold=0.02:ratio=8:attack=60:release=450[duck];"
                f"[1:a][duck]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            filters.append(
                f"[2:a]volume={bgm_vol}[bg];"
                f"[1:a][bg]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        filters.append("[1:a]anull[aout]")

    if burn:
        srt_dir = pdir / "srt"
        srt = srt_dir / "trans.srt"
        if not srt.exists():
            raise DubForgeError("烧录字幕开启但 trans.srt 不存在", code="SRT_MISSING")
        # 字幕滤镜路径转义繁琐 → 以 srt 目录为工作目录用相对文件名
        filters.append(
            "[0:v]subtitles=trans.srt:force_style='FontSize=18,Outline=1,MarginV=25'[v]")
        cmd += ["-map", "[v]", "-c:v", "libx264", "-crf", str(crf),
                "-preset", "medium"]
    else:
        cmd += ["-map", "0:v", "-c:v", "copy"]
    cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-filter_complex", ";".join(filters), str(out)]
    if burn:
        cmd[cmd.index(str(out))] = out.name   # 相对输出
        cwd = srt_dir
    else:
        cwd = None

    # 先写临时名, 成功才转正 —— 中断/取消不留下半成品 mp4
    tmp_out = out.with_name(f".tmp_{out.name}")
    if cwd:
        cmd[cmd.index(out.name)] = tmp_out.name
    else:
        cmd[cmd.index(str(out))] = str(tmp_out)
    rc, _, err = ctx.run_subprocess(cmd, cwd=cwd)
    if rc != 0:
        tmp_out.unlink(missing_ok=True)
        raise DubForgeError("成片封装失败 (ffmpeg)", code="FFMPEG_FAIL",
                            detail=err[-1500:])
    os.replace((cwd / tmp_out.name) if cwd else tmp_out, out)


def run(ctx) -> None:
    pdir = ctx.pdir
    from dubforge.pipeline.video_source import resolve_video
    video = resolve_video(ctx.project, pdir)
    if video is None:
        raise DubForgeError("项目没有可用的源视频", code="VIDEO_MISSING",
                            suggestion="本地导入的项目请确认原文件还在原位置; 下载项目请重跑「获取视频」。")

    total = _ffprobe_duration(video)
    if total <= 0:
        raise DubForgeError("无法探测视频时长", code="VIDEO_DURATION")

    dub = _mix_timeline(ctx, pdir, total)
    ctx.report_progress(40)
    ctx.check_pause()
    ctx.log("人声轨就绪, 开始封装...")
    ctx.report_progress(70)

    out_dir = pdir / "output"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out = out_dir / f"final_{stamp}.mp4"
    _mux(ctx, pdir, video, dub, out)
    ctx.register_file(f"final_{stamp}.mp4", out, "video")

    # 伴生字幕 (双语优先)
    srt_dir = pdir / "srt"
    for name in ("bilingual.srt", "trans.srt"):
        if (srt_dir / name).exists():
            import shutil
            shutil.copy2(srt_dir / name, out_dir / f"final_{stamp}.srt")
            break

    size = out.stat().st_size // 1048576
    ctx.log(f"✅ 成片: {out.name} ({size} MB) | 输出目录: {out_dir}")
