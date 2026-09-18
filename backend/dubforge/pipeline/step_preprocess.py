# -*- coding: utf-8 -*-
"""
步骤 2: preprocess —— 预处理
============================
产物:
  audio/raw.wav          16kHz 单声道源音轨 (供转录与 ASR 备用)
  audio/vocal.wav        demucs 人声 (供动态情感参考 + 精修工作台"听原声")
  audio/background.wav   demucs 背景音 (供成片混音; 供工作台混音试听)
  separate.json          分离元信息

- demucs 用 htdemucs_ft 模型 (vl_cache 已缓存 HTDemucs); 显存不足自动降 two-stems CPU
- ctx.check_cancel 在分块回调间检查 (demucs 无细粒度回调, 取消粒度=整曲, 可接受)
- "取消不产生半成品" 由临时目录保证: 输出写 artifacts/preprocess/.tmp_* 完成后改名
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from dubforge.errors import DubForgeError


def _extract_audio(ctx, video: Path, out_wav: Path) -> None:
    """ffmpeg 提取 16k mono wav (faster-whisper 与 demucs 的公共输入格式)"""
    tmp = out_wav.with_suffix(".wav.tmp")
    # 临时文件 .tmp 后缀无法让 ffmpeg 推断格式, 须显式 -f wav
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
           "-f", "wav", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise DubForgeError("音轨提取失败 (ffmpeg)", code="FFMPEG_FAIL",
                            detail=r.stderr[-1500:])
    os.replace(tmp, out_wav)


def _run_demucs(ctx, raw_wav, out_dir) -> tuple:
    """htdemucs 分离人声/伴奏; 返回 (vocal_path, background_path)"""
    from dubforge import config
    config.ensure_hf_env()
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    ctx.log(f"demucs 分离中 (device={device}, model=htdemucs)")

    tmp_out = out_dir / ".demucs_tmp"
    if tmp_out.exists():
        shutil.rmtree(tmp_out)
    tmp_out.mkdir(parents=True)

    from demucs import pretrained
    from demucs.apply import apply_model
    import soundfile as sf
    import torch as th

    model = pretrained.get_model("htdemucs")
    model.to(device)
    model.eval()

    # 输入恒为 16k pcm_s16le wav (preprocess 自己产出), soundfile 足够且
    # 避免 torchaudio 新版的 torchcodec 后端依赖 (需 FFmpeg7 共享库, 坑)
    import soundfile as sf
    data, sr = sf.read(str(raw_wav), dtype="float32", always_2d=True)
    wav = torch.from_numpy(data.T)          # [channels=1, samples]
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)              # htdemucs 要求立体声输入, 单声道复制
    wav = wav.to(device)
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / (ref.std() + 1e-8)

    with th.no_grad():
        sources = apply_model(model, wav[None], device=device,
                              split=True, overlap=0.25,
                              progress=False)[0]
    # htdemucs stems 顺序: drums, bass, other, vocals
    vocals = sources[-1]
    background = sources[:-1].sum(0)

    vocal_path = tmp_out / "vocal.wav"
    bg_path = tmp_out / "background.wav"
    sf.write(vocal_path, (vocals * ref.std() + ref.mean()).cpu().numpy().T, sr)
    sf.write(bg_path, (background * ref.std() + ref.mean()).cpu().numpy().T, sr)

    final_v = out_dir / "vocal.wav"
    final_b = out_dir / "background.wav"
    os.replace(vocal_path, final_v)
    os.replace(bg_path, final_b)
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
        v, b = _run_demucs(ctx, raw_wav, audio_dir)
        vocal, bg = v, b
        ctx.report_progress(85)
    ctx.register_file("vocal.wav", vocal, "audio")
    ctx.register_file("background.wav", bg, "audio")
    ctx.write_json("separate.json", {"separated": True, "model": "htdemucs"})
    ctx.log("✅ 预处理完成 (raw + vocal + background)")
