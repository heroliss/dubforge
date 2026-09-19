# -*- coding: utf-8 -*-
"""
步骤 2: preprocess —— 预处理
============================
产物:
  audio/raw.wav          16kHz 单声道源音轨 (供转录与 ASR 备用)
  audio/vocal.wav        demucs 人声 44.1k 立体声 (供动态情感参考 + 工作台)
  audio/background.wav   demucs 背景音 44.1k 立体声 (供成片混音)
  separate.json          分离元信息

- 分离输入恒为 44.1kHz 立体声 (从原视频抽取, 用完即删) —— htdemucs 的训练规格;
  模型/位移平均/重叠比例可配 (separate.model/shifts/overlap, 步骤页可改)
- 显存不足自动降级; ctx.check_cancel 取消粒度=整曲 (demucs 无细粒度回调)
- "取消不产生半成品" 由临时目录保证: 输出写 artifacts/preprocess/.tmp_* 完成后改名
"""
import os
import shutil
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_replace


def _extract_audio(ctx, video: Path, out_wav: Path) -> None:
    """ffmpeg 提取 16k mono wav (faster-whisper 与 demucs 的公共输入格式)"""
    tmp = out_wav.with_suffix(".wav.tmp")
    # 临时文件 .tmp 后缀无法让 ffmpeg 推断格式, 须显式 -f wav
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
           "-f", "wav", str(tmp)]
    rc, _, err = ctx.run_subprocess(cmd)
    if rc != 0:
        raise DubForgeError("音轨提取失败 (ffmpeg)", code="FFMPEG_FAIL",
                            detail=err[-1500:])
    atomic_replace(tmp, out_wav)


def _demucs_callback(ctx, total_sec: float, shifts: int, sr: int):
    """demucs 分段回调 → 进度上报 + 段间暂停/取消检查点。

    demucs (4.1.0) 的 apply_model 在 GPU 下用 DummyPoolExecutor 同步逐段执行,
    每段计算前后各回调一次 ({state: start|end, segment_offset, shift_idx, ...});
    回调在我们自己的线程里运行 —— 在回调里阻塞即暂停, 抛异常即取消
    (apply_model 捕获后 cancel_futures 终止剩余分段并向上传播)。
    单段是一次前向推理, 原子不可分 —— 检查点粒度 = 分段长度 (htdemucs 每段数秒)。

    注意: segment_offset 单位是采样点 (shifts>0 时是相对位移后分块的),
    须除以 sr 换算成秒; shifts>0 时 demucs 只跑 shifts 遍位移平均 (没有额外
    的干净遍), 遍数分母 = max(1, shifts)。"""
    passes = max(1, shifts)

    def cb(d: dict) -> None:
        if d.get("state") != "end":
            return
        ctx.check_pause()          # 暂停: 段间阻塞 (显存保持占用, 不再计算)
        ctx.check_cancel()         # 取消: 抛出后 demucs 终止剩余分段
        offset_sec = float(d.get("segment_offset") or 0) / max(sr, 1)
        shift_idx = int(d.get("shift_idx") or 0)
        frac = (shift_idx + min(1.0, offset_sec / max(total_sec, 1e-6))) / passes
        ctx.report_progress(30 + frac * 55)     # 30 → 85 区间
    return cb


def _run_demucs(ctx, src_wav: Path, out_dir: Path) -> tuple:
    """htdemucs 分离人声/伴奏; 输入须为 44.1kHz 立体声 (模型训练规格)"""
    from dubforge import config
    config.ensure_hf_env()
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_name = str(ctx.cfg("separate.model", "htdemucs"))
    shifts = int(ctx.cfg("separate.shifts", 0) or 0)
    overlap = float(ctx.cfg("separate.overlap", 0.25) or 0.25)
    ctx.log(f"demucs 分离中 (device={device}, model={model_name}, shifts={shifts})")

    tmp_out = out_dir / ".demucs_tmp"
    if tmp_out.exists():
        shutil.rmtree(tmp_out)
    tmp_out.mkdir(parents=True)

    from demucs import pretrained
    from demucs.apply import apply_model
    import soundfile as sf
    import torch as th

    model = pretrained.get_model(model_name)
    model.to(device)
    model.eval()

    data, sr = sf.read(str(src_wav), dtype="float32", always_2d=True)
    total_sec = len(data) / sr
    seg = float(getattr(model, "segment", 0) or 0)
    passes = max(1, shifts)
    if seg > 0:
        import math
        n_seg = math.ceil(total_sec / (seg * (1 - overlap)))
        ctx.log(f"整轨 {total_sec:.0f}s ≈ {n_seg} 段 × {seg:.0f}s, "
                f"共 {n_seg * passes} 次前向 (暂停/取消在段间生效)")
    if model_name.endswith("_ft") and shifts > 0:
        ctx.log("⚠ htdemucs_ft + shifts>0 计算量约为标准版 3 倍; 显存接近满载时 "
                "Windows 会溢出到共享内存, 速度可能骤降 —— 小显存建议 htdemucs + shifts=0")
    wav = torch.from_numpy(data.T)          # [channels, samples]
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)              # htdemucs 要求立体声输入, 单声道复制
    if sr != model.samplerate:
        ctx.log(f"⚠ 输入采样率 {sr} ≠ 模型规格 {model.samplerate}, 分离质量会下降")
    wav = wav.to(device)
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / (ref.std() + 1e-8)

    try:
        with th.no_grad():
            sources = apply_model(model, wav[None], device=device,
                                  split=True, overlap=overlap, shifts=shifts,
                                  progress=False,
                                  callback=_demucs_callback(ctx, total_sec,
                                                            shifts, sr))[0]
    except BaseException:
        # 取消/暂停异常: 终止剩余分段后清掉本曲工作目录再向上传播
        shutil.rmtree(tmp_out, ignore_errors=True)
        raise
    # htdemucs stems 顺序: drums, bass, other, vocals
    vocals = sources[-1]
    background = sources[:-1].sum(0)

    vocal_path = tmp_out / "vocal.wav"
    bg_path = tmp_out / "background.wav"
    sf.write(vocal_path, (vocals * ref.std() + ref.mean()).cpu().numpy().T, sr)
    sf.write(bg_path, (background * ref.std() + ref.mean()).cpu().numpy().T, sr)

    final_v = out_dir / "vocal.wav"
    final_b = out_dir / "background.wav"
    atomic_replace(vocal_path, final_v)
    atomic_replace(bg_path, final_b)
    shutil.rmtree(tmp_out, ignore_errors=True)
    return final_v, final_b


def run(ctx) -> None:
    audio_dir = ctx.pdir / "audio"
    audio_dir.mkdir(exist_ok=True)
    video = ctx.upstream("download", "video.mp4")
    raw_wav = audio_dir / "raw.wav"

    if raw_wav.exists():
        ctx.log("raw.wav 已存在, 跳过音轨提取")
    else:
        ctx.log("提取 16kHz 单声道音轨...")
        _extract_audio(ctx, video, raw_wav)
    ctx.register_file("raw.wav", raw_wav, "audio")
    ctx.report_progress(20)

    if not ctx.cfg("separate.enabled", True):
        ctx.log("配置关闭了人声分离, 跳过 demucs")
        ctx.write_json("separate.json", {"separated": False})
        return

    vocal = audio_dir / "vocal.wav"
    bg = audio_dir / "background.wav"
    if vocal.exists() and bg.exists():
        ctx.log("vocal/background 已存在, 跳过分离")
    else:
        ctx.report_progress(30)
        ctx.check_pause()
        # htdemucs 按 44.1kHz 立体声训练 —— 喂 16k 单声道会频谱错位、严重劣化。
        # 从原视频抽全规格音轨作分离输入, 用完即删 (重跑重新提取只需几秒)。
        src441 = audio_dir / ".tmp_src44100.wav"
        cmd = ["ffmpeg", "-y", "-i", str(video), "-vn",
               "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
               "-f", "wav", str(src441)]
        rc, _, err = ctx.run_subprocess(cmd)
        if rc != 0:
            raise DubForgeError("分离输入音轨提取失败 (ffmpeg)", code="FFMPEG_FAIL",
                                detail=err[-1500:])
        try:
            ctx.check_cancel()
            ctx.log("分段分离中 —— 进度逐段上报; 暂停/取消在段间检查点生效 (数秒粒度)")
            vocal, bg = _run_demucs(ctx, src441, audio_dir)
        finally:
            src441.unlink(missing_ok=True)
        ctx.report_progress(85)
        ctx.check_cancel()
    ctx.register_file("vocal.wav", vocal, "audio")
    ctx.register_file("background.wav", bg, "audio")
    ctx.write_json("separate.json",
                   {"separated": True, "model": str(ctx.cfg("separate.model", "htdemucs"))})
    ctx.log("✅ 预处理完成 (raw + vocal + background)")
