"""
IndexTTS2.5 后端 (gradio /gen_single)
=====================================
通过 Gradio 接口分别传递音色与情感参考：
  音色 timbre: preset(固定文件) / stable(指定句原声) / dynamic(每句原声)
  情感 emo:    same / preset / stable / dynamic / vector(8维) / text
调用方按全局、模板、项目和本句解析有效配置，后端只使用显式传入的参数。
参考切片统一由调用方 (step_tts) 通过 resolve 参数传入路径。
"""
import math
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

import httpx
import soundfile as sf

from dubforge.errors import DubForgeError, ServiceOfflineError
from dubforge.utils.fs import atomic_replace

# WebUI 枚举 (中英双语界面兼容)
METHOD_SAME = "与音色参考音频相同"
# IndexTTS 的 Kaldi fbank 以 16 kHz、25 ms 帧提取参考特征；不足一帧会触发断言。
MINIMUM_REFERENCE_SECONDS = .025


class ReferenceAudioError(DubForgeError):
    code = 'REF_AUDIO_INVALID'
    suggestion = '请检查本句时间轴与原声文件，或为本句选择同一说话人的有效参考音频后重试。'


class IndexTTSRequestError(DubForgeError):
    code = 'TTS_REQUEST_FAILED'


class IndexTTSAudioError(DubForgeError):
    code = 'TTS_AUDIO_INVALID'


def validate_reference(path: Path, label: str, *, minimum_seconds: float = 0) -> None:
    """按文件版本复用成功校验，固定参考不再每句重复解码或启动 FFmpeg。"""
    path = Path(path)
    try:
        resolved = path.resolve()
        version = resolved.stat()
    except (OSError, RuntimeError):
        # 保留既有中文错误与兼容格式的探测逻辑；失败不能进入成功缓存。
        return _validate_reference(path, label, minimum_seconds=minimum_seconds)
    _validate_reference_version(str(resolved), version.st_size, version.st_mtime_ns,
                                version.st_ctime_ns, version.st_ino, label, minimum_seconds)


@lru_cache(maxsize=128)
def _validate_reference_version(path, size, mtime_ns, ctime_ns, inode, label, minimum_seconds):
    # 文件变化即重新检查；只缓存校验结果，不缓存音频内容，也不缓存异常。
    _validate_reference(Path(path), label, minimum_seconds=minimum_seconds)


def _validate_reference(path: Path, label: str, *, minimum_seconds: float = 0) -> None:
    """切片检查可读帧，上传模型时另核对最低特征帧时长。"""
    try:
        info = sf.info(str(path))
    except (OSError, RuntimeError, ValueError) as exc:
        # librosa 也能通过 FFmpeg 读取 m4a 等格式；不能因 libsndfile 不支持而拒绝有效预设。
        try:
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError('参考文件不存在或为空')
            decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-t', '0.1',
                '-ar', '16000', '-ac', '1', '-f', 's16le', 'pipe:1'],
                check=True, capture_output=True, timeout=15)
            if len(decoded.stdout) < 2:
                raise ValueError('音频没有有效采样帧')
            duration = (len(decoded.stdout) // 2) / 16000
        except (OSError, subprocess.SubprocessError, ValueError) as decode_error:
            detail = (decode_error.stderr.decode('utf-8', errors='replace')
                      if isinstance(decode_error, subprocess.CalledProcessError) and decode_error.stderr
                      else str(decode_error))
            raise ReferenceAudioError(f'{label}参考音频无效或没有音频帧：{path}',
                                      detail=detail) from exc
    else:
        if info.frames <= 0 or info.samplerate <= 0 or not math.isfinite(info.duration) or info.duration <= 0:
            raise ReferenceAudioError(f'{label}参考音频无效或没有音频帧：{path}',
                                      detail='音频没有有效采样帧')
        duration = info.duration
    if duration < minimum_seconds:
        raise ReferenceAudioError(
            f'{label}参考音频过短：{duration:.6f} 秒；IndexTTS 至少需要 {minimum_seconds:g} 秒参考音频。',
            detail={'path': str(path), 'duration_seconds': duration, 'minimum_seconds': minimum_seconds})


def _enum_emo(emo_mode: str) -> str:
    return {
        "preset": "使用情感参考音频",
        "dynamic": "使用情感参考音频",
        "stable": "使用情感参考音频",
        "vector": "使用情感向量控制",
        "text": "使用情感描述文本控制",
    }.get(emo_mode, METHOD_SAME)


def is_online(url: str) -> bool:
    try:
        with httpx.Client(timeout=3, trust_env=False) as c:
            return c.get(url.rstrip("/") + "/config").status_code == 200
    except Exception:
        return False


# 本后端支持的语言 (index-tts webui.py 的 lang_dropdown 写死五项; 换版本时同步)
# 中文名与「翻译目标语言」的取值对齐 —— 相同名字即可贯穿全流程 (识别→翻译→配音)
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("ZH", "简体中文"), ("EN", "English"), ("JA", "日本語"),
    ("AR", "阿拉伯语"), ("ES", "西班牙语"),
)


def _reported_progress(units):
    """用实际迭代次数细化 IndexTTS 原有 20%～90% 的合成区间。"""
    values = []
    for unit in units or []:
        value = getattr(unit, 'progress', None)
        match = re.fullmatch(r'DubForge speech (\d+)/(\d+)', getattr(unit, 'desc', None) or '')
        if match:
            segment, count = map(int, match.groups())
            index, length = getattr(unit, 'index', None), getattr(unit, 'length', None)
            if (0 < segment <= count and type(index) in (int, float) and type(length) in (int, float)
                    and math.isfinite(index) and math.isfinite(length) and 0 <= index <= length and length > 0):
                value = .2 + .7 * (segment - 1 + index / length) / count
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
            values.append(float(value))
    return max(values, default=None)


def _result_with_progress(job, progress: Callable[[float], None], *, check_cancel=lambda: None,
                          timeout_seconds=600):
    """读取当前 Gradio 任务的真实进度，并区分本地等待与服务端完成状态。

    ``Job.done()`` 的状态可能比远端回执慢一个轮询周期，因此截止时间到达时
    先做一次非阻塞结果读取，避免恰好完成的请求被误报超时。超时详情保留最后
    进度和轮询阶段，便于区分模型推理长尾、服务排队和结果回传抖动。
    """
    previous = None
    started = time.monotonic()
    polls = 0
    phase = '等待服务端结果'

    def finished():
        try:
            return bool(job.done())
        except Exception:
            return False

    def timeout_error(elapsed=None):
        from dubforge.tts_requests import RequestTimeout
        elapsed = max(0.0, float(elapsed if elapsed is not None else time.monotonic() - started))
        detail = {
            'elapsed_seconds': round(elapsed, 2),
            'timeout_seconds': float(timeout_seconds),
            'last_progress': previous,
            'phase': phase,
            'status_polls': polls,
        }
        progress_text = (f'，最后已知进度 {previous * 100:.0f}%'
                         if isinstance(previous, (int, float)) else '')
        return RequestTimeout(
            f'单次配音请求超过 {timeout_seconds:g} 秒，服务可能仍在推理或回传'
            f'{progress_text}；已停止等待本批任务并保留已完成音频',
            detail=detail)

    while True:
        check_cancel()
        # 先确认已完成，避免在边界时刻先检查时钟而丢掉刚到达的结果。
        if finished():
            return job.result()
        elapsed = time.monotonic() - started
        if timeout_seconds > 0 and elapsed >= timeout_seconds:
            # Future 状态有极短的传播延迟；零等待读取只接受已到达的结果。
            try:
                return job.result(timeout=0)
            except TimeoutError:
                raise timeout_error(elapsed)
        phase = '读取服务端状态'
        polls += 1
        value = _reported_progress(job.status().progress_data)
        if value is not None and (previous is None or value > previous):
            progress(value)
            previous = value
        phase = '等待服务端结果'
        try:
            return job.result(timeout=0.5)
        except TimeoutError:
            # 区分等待超时和服务本身抛出的 TimeoutError，后者必须正常报错。
            if finished():
                return job.result()


def synth(url: str, text: str, out_path: Path, lang: str,
          timbre_ref: Path, emo_mode: str = "preset", emo_ref: Path | None = None,
          emo_weight: float = 1.0, emo_vector: list | None = None,
          emo_text: str = "", emo_random: bool = False,
          duration_factor: float = 1.0, advanced: dict | None = None,
          log=lambda m: None, progress: Callable[[float], None] | None = None,
          check_cancel=lambda: None, timeout_seconds=600) -> Path:
    """合成一句; 返回 out_path (原子落盘由调用方保证临时名)"""
    from gradio_client import Client, handle_file
    from gradio_client.exceptions import AppError

    check_cancel()
    validate_reference(timbre_ref, '音色', minimum_seconds=MINIMUM_REFERENCE_SECONDS)
    if emo_ref is not None and emo_mode in {'preset', 'stable', 'dynamic'}:
        validate_reference(emo_ref, '情感', minimum_seconds=MINIMUM_REFERENCE_SECONDS)

    if not is_online(url):
        raise ServiceOfflineError(
            f"IndexTTS WebUI 未启动 ({url})",
            suggestion="启动 index-tts 的 webui (端口 7860) 后重试; 或在设置页切换 TTS 后端。")

    adv = {
        "do_sample": True, "top_p": 0.8, "top_k": 30, "temperature": 0.8,
        "length_penalty": 0.0, "num_beams": 3, "repetition_penalty": 10.0,
        "max_mel_tokens": 1500, "max_text_tokens_per_segment": 120,
    }
    adv.update(advanced or {})
    kwargs = {
        "emo_control_method": _enum_emo(emo_mode),
        "prompt": handle_file(str(timbre_ref)),
        "text": text,
        "lang_choice": lang,
        # 非音频情感模式不上传被模型忽略的旧参考文件。
        "emo_ref_path": handle_file(str(emo_ref)) if emo_ref and emo_mode in {'preset', 'stable', 'dynamic'} else None,
        "emo_weight": float(emo_weight),
        "emo_text": emo_text if emo_mode == "text" else "",
        "emo_random": bool(emo_random) if emo_mode in {"vector", "text"} else False,
        "max_text_tokens_per_segment": int(adv["max_text_tokens_per_segment"]),
        "duration_factor": float(duration_factor),
        "param_18": bool(adv["do_sample"]),     # do_sample
        "param_19": float(adv["top_p"]),
        "param_20": int(adv["top_k"]),
        "param_21": float(adv["temperature"]),
        "param_22": float(adv["length_penalty"]),
        "param_23": int(adv["num_beams"]),
        "param_24": float(adv["repetition_penalty"]),
        "param_25": int(adv["max_mel_tokens"]),
        "api_name": "/gen_single",
    }
    vec = emo_vector or []
    # 所有输入显式传入，避免借用 WebUI 会话中遗留的向量值。
    for i in range(8):
        kwargs[f"vec{i + 1}"] = float(vec[i]) if emo_mode == "vector" and i < len(vec) else 0.0

    from dubforge.tts_requests import Request
    request = Request(url)
    try:
        client = Client(url, ssl_verify=False, verbose=False,
                        httpx_kwargs={'timeout': 30, 'trust_env': False})
    except Exception as exc:
        request.finish()
        raise ServiceOfflineError('无法建立 IndexTTS 连接，请检查服务状态', detail=str(exc)) from exc
    job = None
    try:
        check_cancel()
        job = client.submit(**kwargs)
        result = _result_with_progress(job, progress or (lambda value: None),
            check_cancel=check_cancel, timeout_seconds=timeout_seconds)
    except BaseException as exc:
        # AppError 来自服务的最终失败回执；客户端 Future.done() 也可能只是断线，不能据此释放。
        if job is not None and not isinstance(exc, AppError):
            request.detach(job, client, log)
        if isinstance(exc, DubForgeError) or not isinstance(exc, Exception):
            raise
        if isinstance(exc, AppError):
            hidden = 'has not enabled verbose error reporting' in str(exc)
            message = ('IndexTTS 服务端合成失败；服务未返回原因，请查看配音服务日志' if hidden
                       else f'IndexTTS 服务端合成失败：{exc}')
            suggestion = ('请检查当前句的时间轴与参考音频，再查看配音服务日志中的原始异常。'
                          '本次请求已经结束，修复原因后可以补齐配音，无需为此重启服务。')
        else:
            message = f'IndexTTS 请求失败：{exc}'
            from dubforge.tts_requests import RECOVERY_HINT
            suggestion = RECOVERY_HINT
        raise IndexTTSRequestError(message, suggestion=suggestion, detail=str(exc)) from exc
    finally:
        if not request.detached:
            request.finish()
            client.close()
    out = None
    if isinstance(result, str):
        out = result
    elif isinstance(result, (list, tuple)) and result:
        v = result[0]
        out = v.get("path") or v.get("url") if isinstance(v, dict) else v
    elif isinstance(result, dict):
        for v in result.values():
            cand = v.get("path") if isinstance(v, dict) else (v if isinstance(v, str) else None)
            if cand and Path(str(cand)).exists():
                out = cand
                break
    if not out or not Path(str(out)).exists():
        raise IndexTTSAudioError('IndexTTS 已结束请求，但返回值中没有可读取的音频',
                                detail=repr(result))

    # 统一转 32k mono wav (铺轨/试听的公共格式), 临时名 → 原子替换
    tmp = out_path.with_suffix(".wav.tmp")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(out), "-ar", "32000", "-ac", "1",
                        "-f", "wav", str(tmp)], check=True, capture_output=True)
        atomic_replace(tmp, out_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return out_path
