# -*- coding: utf-8 -*-
"""
步骤实现注册表
==============
key → run(ctx) 的映射; 九步均已为真实实现。
契约: 签名 run(ctx: StepContext), 幂等 (由执行器的指纹跳过机制保证语义);
通过 ctx.log 打点 / ctx.check_cancel / ctx.check_pause 响应停止与暂停 /
ctx.write_* 原子产出。
测试可用 stub_heavy_steps 夹具临时替换 _RUNNERS[key] (状态机测试与真实现解耦)。
"""
import time
from typing import Callable

from dubforge.pipeline.registry import STEP_KEYS


def _stub(step_key: str):
    """兜底桩 (当前注册表全覆盖, 仅测试/未来新步骤占位用)"""
    def run(ctx) -> None:
        ctx.log(f"[stub] 桩实现: 模拟 {step_key} 的工作")
        delays = ctx.project_params.get("delay_step", {})
        delay = float(delays.get(step_key, 0)) if isinstance(delays, dict) else 0.0
        for i in range(3):
            if delay:
                time.sleep(delay)
            ctx.check_cancel()
            ctx.log(f"[stub] 工作分片 {i + 1}/3 完成")
        if ctx.project_params.get("fail_step") == step_key:
            from dubforge.errors import StepFailedError
            raise StepFailedError(f"模拟失败: 步骤 {step_key} (项目参数 fail_step 触发)")
        ctx.write_json(f"{step_key}_result.json", {
            "step": step_key,
            "note": "M1 桩产物; M2 起替换为真实管线产物",
        })
    return run


# 真实实现 (M2a 媒体链 + M2b 文本链)
from dubforge.pipeline.step_download import run as _download_run
from dubforge.pipeline.step_preprocess import run as _preprocess_run
from dubforge.pipeline.step_transcribe import run as _transcribe_run
from dubforge.pipeline.step_split import run as _split_run
from dubforge.pipeline.step_translate import run as _translate_run
from dubforge.pipeline.step_subtitles import run as _subtitles_run

from dubforge.pipeline.step_dubtask import run as _dubtask_run
from dubforge.pipeline.step_tts import run as _tts_run

from dubforge.pipeline.step_assemble import run as _assemble_run

_REAL = {
    "download": _download_run,
    "preprocess": _preprocess_run,
    "transcribe": _transcribe_run,
    "split": _split_run,
    "translate": _translate_run,
    "subtitles": _subtitles_run,
    "dubtask": _dubtask_run,
    "tts": _tts_run,
    "assemble": _assemble_run,
}

_RUNNERS = {**_REAL, **{key: _stub(key) for key in STEP_KEYS if key not in _REAL}}


def get_runner(step_key: str) -> Callable:
    """执行器按 key 取实现函数; 未注册视为配置错误"""
    if step_key not in _RUNNERS:
        from dubforge.errors import ConfigError
        raise ConfigError(f"步骤 {step_key} 没有注册实现")
    return _RUNNERS[step_key]
