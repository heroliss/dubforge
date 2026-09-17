# -*- coding: utf-8 -*-
"""音频工具: 时间轴铺轨 / 峰值归一 (numpy + soundfile, 零 ffmpeg 依赖)
约定: 配音句统一 32kHz 单声道 wav (tts 后端保证)"""
import numpy as np
import soundfile as sf

SR = 32000


def load_mono_32k(path) -> np.ndarray:
    """读音频为 float32 mono @32k (非 32k 输入自动重采样线性插值)"""
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != SR:
        n_out = int(len(mono) * SR / sr)
        x_new = np.linspace(0, len(mono) - 1, n_out)
        mono = np.interp(x_new, np.arange(len(mono)), mono)
    return mono.astype(np.float32)


def mix_over_timeline(entries: list[tuple[float, "np.ndarray"]],
                      total_seconds: float) -> np.ndarray:
    """把 (开始秒, 单声道音频) 铺到 total_seconds 的静音轨上; 重叠段叠加"""
    total = max(1, int(round(total_seconds * SR)))
    out = np.zeros(total, dtype=np.float32)
    for start, audio in entries:
        i0 = int(start * SR)
        if i0 >= total:
            continue
        seg = audio[: total - i0]
        out[i0:i0 + len(seg)] += seg
    return out


def peak_normalize(arr: np.ndarray, target_db: float = -3.0) -> np.ndarray:
    """峰值归一到 target_db (防止叠加削波后整体拉低)"""
    peak = float(np.max(np.abs(arr))) if len(arr) else 0.0
    if peak < 1e-6:
        return arr
    target = 10 ** (target_db / 20)
    return (arr / peak * target).astype(np.float32)


def fade_in_out(arr: np.ndarray, ms: int = 10) -> np.ndarray:
    """句首尾短淡入淡出, 消除拼接咔哒声"""
    n = min(len(arr), int(SR * ms / 1000))
    if n < 2:
        return arr
    ramp = np.linspace(0, 1, n, dtype=np.float32)
    arr = arr.copy()
    arr[:n] *= ramp
    arr[-n:] *= ramp[::-1]
    return arr
