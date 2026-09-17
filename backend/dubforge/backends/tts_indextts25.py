# -*- coding: utf-8 -*-
"""
IndexTTS2.5 后端 (gradio /gen_single)
=====================================
移植自 VideoLingo-Workspace 验证过的双维度参考设计:
  音色 timbre: preset(固定文件) / stable(源音频开头切片) / dynamic(每句原声)
  情感 emo:    preset / stable / dynamic / vector(8维)
单一配置源 config.yaml tts.indextts25.*; 每次合成实时读取。
参考切片统一由调用方 (step_tts) 通过 resolve 参数传入路径。
"""
import re
import subprocess
from pathlib import Path

import httpx

from dubforge import config
from dubforge.errors import ServiceOfflineError

# WebUI 枚举 (中英双语界面兼容)
METHOD_SAME = "与音色参考音频相同"


def _enum_emo(emo_mode: str) -> str:
    return {
        "preset": "使用情感参考音频",
        "dynamic": "使用情感参考音频",
        "stable": "使用情感参考音频",
        "vector": "使用情感向量控制",
    }.get(emo_mode, METHOD_SAME)


def is_online(url: str) -> bool:
    try:
        with httpx.Client(timeout=3, trust_env=False) as c:
            return c.get(url.rstrip("/") + "/config").status_code == 200
    except Exception:
        return False


def synth(url: str, text: str, out_path: Path, lang: str,
          timbre_ref: Path, emo_mode: str = "preset", emo_ref: Path | None = None,
          emo_weight: float = 1.0, emo_vector: list | None = None,
          duration_factor: float = 1.0, log=lambda m: None) -> Path:
    """合成一句; 返回 out_path (原子落盘由调用方保证临时名)"""
    from gradio_client import Client, handle_file

    if not is_online(url):
        raise ServiceOfflineError(
            f"IndexTTS WebUI 未启动 ({url})",
            suggestion="启动 index-tts 的 webui (端口 7860) 后重试; 或在设置页切换 TTS 后端。")

    from dubforge.backends import llm
    client = Client(url, ssl_verify=False)       # gradio_client 自带代理处理

    kwargs = {
        "emo_control_method": _enum_emo(emo_mode),
        "prompt": handle_file(str(timbre_ref)),
        "text": text,
        "lang_choice": lang,
        "emo_ref_path": handle_file(str(emo_ref if emo_ref else timbre_ref)),
        "emo_weight": float(emo_weight),
        "emo_text": "",
        "emo_random": False,
        "max_text_tokens_per_segment": 120,
        "duration_factor": float(duration_factor),
        "param_18": True,                        # do_sample
        "param_19": 0.8,                         # top_p
        "param_20": 30,                          # top_k
        "param_21": 0.8,                         # temperature
        "param_22": 0.0,                         # length_penalty
        "param_23": 3,                           # num_beams
        "param_24": 10.0,                        # repetition_penalty
        "param_25": 1500,                        # max_mel_tokens
        "api_name": "/gen_single",
    }
    vec = emo_vector or []
    if emo_mode == "vector":
        for i in range(8):
            kwargs[f"vec{i + 1}"] = float(vec[i]) if i < len(vec) else 0.0

    result = client.predict(**kwargs)
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
        raise RuntimeError(f"IndexTTS 返回值中未找到音频: {result!r}")

    # 统一转 32k mono wav (铺轨/试听的公共格式), 临时名 → 原子替换
    tmp = out_path.with_suffix(".wav.tmp")
    subprocess.run(["ffmpeg", "-y", "-i", str(out), "-ar", "32000", "-ac", "1",
                    "-f", "wav", str(tmp)], check=True, capture_output=True)
    import os
    os.replace(tmp, out_path)
    return out_path
