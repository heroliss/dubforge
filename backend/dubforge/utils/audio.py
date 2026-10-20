"""音频工具: 时间轴铺轨 / 峰值归一；异采样率使用 FFmpeg 带限重采样。
约定: 配音句统一 32kHz 单声道 wav (tts 后端保证)"""
import numpy as np
import soundfile as sf
import subprocess

from dubforge.errors import DubForgeError

SR = 32000


def load_mono_32k(path) -> np.ndarray:
    """读音频为 float32 mono @32k；异采样率使用带限重采样，避免混叠。"""
    try:
        info = sf.info(str(path))
    except (OSError, RuntimeError) as exc:
        raise DubForgeError('无法读取声音文件，请核对或重新生成该声音', code='AUDIO_DATA_INVALID') from exc
    if info.samplerate == SR:
        data, _ = sf.read(str(path), dtype='float32', always_2d=True)
        mono = data.mean(axis=1)
    else:
        if not info.frames:
            return np.zeros(0, dtype=np.float32)
        # FFmpeg 直接读取文件，避免长片在 Python 同时保留三份大数组。
        # 显式平均维持原本多声道转 mono 的语义。
        pan = 'pan=mono|c0=' + '+'.join(f'{1 / info.channels:.12g}*c{i}' for i in range(info.channels))
        try:
            result = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                '-i', str(path), '-vn', '-af', f'{pan},aresample={SR}:filter_size=64:phase_shift=10',
                '-f', 'f32le', 'pipe:1'], capture_output=True, check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DubForgeError('音频采样率转换不可用或超时', code='AUDIO_RESAMPLE_FAILED') from exc
        if result.returncode:
            raise DubForgeError('音频采样率转换失败', code='AUDIO_RESAMPLE_FAILED',
                                detail=result.stderr[-1200:].decode('utf-8', 'replace'))
        mono = np.frombuffer(result.stdout, dtype='<f4').copy()
    if not np.isfinite(mono).all():
        raise DubForgeError('音频含无效采样值，请核对或重新生成该声音', code='AUDIO_DATA_INVALID')
    return mono.astype(np.float32, copy=False)


def mix_over_timeline(entries: list[tuple[float, "np.ndarray"]],
                      total_seconds: float) -> np.ndarray:
    """把 (开始秒, 单声道音频) 铺到 total_seconds 的静音轨上; 重叠段叠加"""
    total = max(1, round(total_seconds * SR))
    out = np.zeros(total, dtype=np.float32)
    for start, audio in entries:
        # 用 round 把浮点秒数映射到最近采样点，避免 0.999999... 帧因
        # 二进制浮点误差被 int 向下取整。负起点必须先裁掉片头再写入，
        # 不能直接使用负数组索引，否则会从成品尾部开始叠加。
        i0 = round(start * SR)
        dst_start = max(0, i0)
        src_start = max(0, -i0)
        if dst_start >= total or src_start >= len(audio):
            continue
        seg = audio[src_start:src_start + (total - dst_start)]
        out[dst_start:dst_start + len(seg)] += seg
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
