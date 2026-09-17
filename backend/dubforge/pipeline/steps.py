# -*- coding: utf-8 -*-
"""
步骤实现注册表 (M1: 全部为桩实现; M2/M3 逐个替换为真实管线)
============================================================
桩实现契约与真实实现完全一致:
  - 签名 run(ctx: StepContext), 幂等 (由执行器的指纹跳过机制保证语义)
  - 通过 ctx.log 打点 / ctx.check_cancel 协作取消 / ctx.write_json 产出原子产物
  - 测试支持: 项目 params {"fail_step": "translate"} 可让指定步骤模拟失败,
    {"delay_step": {"translate": 2}} 可模拟耗时 (测取消)
"""
import time
from typing import Callable

from dubforge.pipeline.registry import STEP_KEYS


def _stub(step_key: str):
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


# 真实实现 (M2a 媒体链): 已交付的逐步覆盖桩实现
from dubforge.pipeline.step_download import run as _download_run
from dubforge.pipeline.step_preprocess import run as _preprocess_run
from dubforge.pipeline.step_transcribe import run as _transcribe_run

_RUNNERS = {
    "download": _download_run,
    "preprocess": _preprocess_run,
    "transcribe": _transcribe_run,
    **{key: _stub(key) for key in STEP_KEYS
       if key not in ("download", "preprocess", "transcribe")},
}


def get_runner(step_key: str) -> Callable:
    """执行器按 key 取实现函数; 未注册视为配置错误"""
    if step_key not in _RUNNERS:
        from dubforge.errors import ConfigError
        raise ConfigError(f"步骤 {step_key} 没有注册实现")
    return _RUNNERS[step_key]
