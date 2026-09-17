# -*- coding: utf-8 -*-
"""
DubForge 统一错误体系
=====================
- 业务错误一律抛 DubForgeError 子类/实例, 携带 错误码 + 中文消息 + 修复建议
- API 层捕获后转 JSON: {code, message, suggestion}; 未知异常兜底 500
- 前端错误中心直接渲染这三个字段 (中文一等支持的一部分)
"""
from typing import Any


class DubForgeError(Exception):
    """业务错误基类: code 是稳定错误码 (前端可判断), message 面向用户 (中文)"""

    code = "DUBFORGE_ERROR"
    suggestion = "请查看日志详情, 或把错误信息发给 AI 助手分析。"

    def __init__(self, message: str, *, suggestion: str | None = None,
                 code: str | None = None, detail: Any = None):
        super().__init__(message)
        self.message = message
        if suggestion:
            self.suggestion = suggestion
        if code:
            self.code = code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "suggestion": self.suggestion, "detail": self.detail}


class ConfigError(DubForgeError):
    code = "CONFIG_ERROR"
    suggestion = "请检查仓库根目录 config.yaml 的对应配置项 (模板见 config.example.yaml)。"


class ServiceOfflineError(DubForgeError):
    """外部服务不可达 (IndexTTS / GPT-SoVITS / LLM)"""
    code = "SERVICE_OFFLINE"
    suggestion = "请先启动对应外部服务并确认地址端口, 再重试本步骤。"


class ArtifactMissingError(DubForgeError):
    """前置产物缺失 (不该发生: 执行器会先校验上游步骤完成)"""
    code = "ARTIFACT_MISSING"
    suggestion = "请重跑产生该产物的上游步骤。"


class StepFailedError(DubForgeError):
    """步骤执行失败 (包裹底层异常, 保留原始信息到 detail)"""
    code = "STEP_FAILED"
    suggestion = "修复后可在管线页单独重跑该步骤; 下游步骤会自动标记过期。"
