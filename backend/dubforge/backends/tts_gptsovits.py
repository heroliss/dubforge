# -*- coding: utf-8 -*-
"""
GPT-SoVITS 后端 (v2 API /tts)
=============================
基础版: 单音色 (config tts.gptsovits.character 对应整合包 configs/<角色名>.yaml),
参考音频由 GPT-SoVITS 服务端按角色配置自动选取。
"""
import subprocess
from pathlib import Path

import httpx

from dubforge import config
from dubforge.errors import ServiceOfflineError


def is_online(url: str) -> bool:
    try:
        with httpx.Client(timeout=3, trust_env=False) as c:
            return c.get(url.rstrip("/") + "/tts").status_code in (200, 405)
    except Exception:
        return False


def synth(url: str, text: str, out_path: Path, lang: str = "zh",
          character: str | None = None, log=lambda m: None) -> Path:
    if not is_online(url):
        raise ServiceOfflineError(
            f"GPT-SoVITS 推理 API 未启动 ({url})",
            suggestion="启动 GPT-SoVITS 整合包的 api_v2 服务 (端口 9880) 后重试。")
    body = {
        "text": text,
        "text_lang": "zh" if lang.upper().startswith("Z") else "en",
        "ref_audio_path": "",                     # 留空 = 服务端角色默认
        "prompt_text": "",
        "prompt_lang": "zh",
        "speed_factor": 1.0,
    }
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=300, write=60, pool=10),
                      trust_env=False) as c:
        with c.stream("POST", url.rstrip("/") + "/tts", json=body) as r:
            if r.status_code != 200:
                raise RuntimeError(f"GPT-SoVITS HTTP {r.status_code}")
            tmp = out_path.with_suffix(".wav.tmp")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    # GPT-SoVITS 返回 wav; 统一 32k mono
    subprocess.run(["ffmpeg", "-y", "-i", str(tmp), "-ar", "32000", "-ac", "1",
                    "-f", "wav", str(out_path.with_suffix(".norm"))],
                   check=True, capture_output=True)
    import os
    os.replace(out_path.with_suffix(".norm"), out_path)
    Path(tmp).unlink(missing_ok=True)
    return out_path
