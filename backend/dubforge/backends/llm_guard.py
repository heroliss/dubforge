"""按任务契约限制模型响应；不依赖服务商、敏感词或推理标签。"""
import math
import re
import time

from dubforge.errors import StepFailedError


class ResponseGuardError(StepFailedError):
    """请求已经偏离输出契约；关闭本地连接后由上层决定有限重试。"""


class ResponseGuard:
    """增量验收正文，限制体积、持续重复与整次请求的墙钟时间。

    line_check 只检查已经收齐的物理行，语义和声学仍由最终领域校验负责。
    思考与正文各有独立的计数、尾部窗口，不将思考当成字幕解析。

    思考通道只受整次请求的 timeout_s 约束，不按字符数截断。不同模型的
    reasoning 输出量差异很大，按字符截断会把正常的低/高思考流程误判为
    失败；正文仍受 max_chars、格式和退化检查保护。
    """

    _REPETITION_WINDOW = 4096
    _MAX_PERIOD = 256

    def __init__(self, *, max_chars: int, timeout_s: float,
                 line_check=None, source_text: str = "", content_repetition_allowed=None,
                 on_abort=None, reasoning_max_chars: int | None = None):
        if max_chars < 1 or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("响应体积和请求时限必须为有限正数")
        self.max_chars = max_chars
        # 旧调用方仍可传入该参数；从此版本起忽略它，思考只受总时限约束。
        self.reasoning_max_chars = None
        self.timeout_s = timeout_s
        self.line_check = line_check
        self.content_repetition_allowed = content_repetition_allowed
        self.on_abort = on_abort
        self.started = time.monotonic()
        self.counts = {"content": 0, "reasoning": 0}
        self.tails = {"content": "", "reasoning": ""}
        self.checked_at = {"content": 0, "reasoning": 0}
        self.prefix = ""
        self.pending_line = ""
        self.last_content_line = ""
        self.source = self._normalize(source_text)

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    def _fail(self, message: str, code: str, *, cause=None, **meta):
        suggestion = "本次异常候选不会写入产物；可重试本步骤或查看原始响应日志。"
        error = ResponseGuardError(message, code=code,
            detail={"received_chars": dict(self.counts), "excerpt": self.prefix[:1200], **meta},
            suggestion=suggestion)
        error.contract_error = cause
        if self.on_abort is not None:
            self.on_abort(error)
        raise error from cause

    def check(self):
        elapsed = time.monotonic() - self.started
        if elapsed >= self.timeout_s:
            self._fail(f"LLM 请求已超过 {self.timeout_s:g} 秒总时限，已停止等待",
                       "LLM_REQUEST_DEADLINE", elapsed_s=round(elapsed, 2),
                       timeout_s=self.timeout_s)

    def feed(self, piece: str, channel: str = "content"):
        self.check()
        if not piece:
            return
        self.counts[channel] += len(piece)
        if channel == "content":
            self.prefix = (self.prefix + piece)[:1200]
            self.pending_line += piece
        # 思考内容不设字符预算；它只受 check() 的整次请求时限约束。
        # 正文仍然必须有有限上限，防止服务端失控地生成无法解析的结果。
        if channel == "content" and self.counts[channel] > self.max_chars:
            self._fail(f"LLM {('正文' if channel == 'content' else '思考')}超过任务响应体积上限",
                       "LLM_OUTPUT_BUDGET", channel=channel, max_chars=self.max_chars)
        self.tails[channel] = (self.tails[channel] + piece)[-self._REPETITION_WINDOW * 2:]
        if channel == "content" and self.line_check is not None:
            # 统一 CRLF 与 LF；末尾尚未收齐的行留到下个 token 或 finish。
            while "\n" in self.pending_line:
                line, self.pending_line = self.pending_line.split("\n", 1)
                self._check_line(line.rstrip("\r"))
                if line.strip():
                    self.last_content_line = line.rstrip("\r")
        if channel == "content" and self.counts[channel] - self.checked_at[channel] >= 256:
            self.checked_at[channel] = self.counts[channel]
            self._check_repetition(channel)

    def _check_line(self, line: str):
        try:
            self.line_check(line)
        except ResponseGuardError:
            raise
        except ValueError as error:
            self._fail(f"LLM 正文偏离输出格式：{str(error)[:240]}",
                       "LLM_OUTPUT_CONTRACT", cause=error)

    def _check_repetition(self, channel: str):
        tail = self._normalize(self.tails[channel])
        if len(tail) < self._REPETITION_WINDOW:
            return
        tail = tail[-self._REPETITION_WINDOW:]
        # 要求整个长窗口持续周期重复；普通重复对白、编号行或相似句不会匹配。
        for period in range(1, self._MAX_PERIOD + 1):
            if tail[period:] == tail[:-period]:
                if channel == "content" and tail in self.source:
                    return  # 原文明确包含同等长度的重复，不误判为模型退化。
                if (channel == "content" and self.content_repetition_allowed is not None
                        and self.content_repetition_allowed(
                            self.pending_line if self.pending_line.strip() else self.last_content_line)):
                    return  # 合法字幕正文可能包含长歌谣；结构与体积门禁仍生效。
                self._fail("LLM 连续生成大量相同片段，已中止退化输出",
                           "LLM_OUTPUT_DEGENERATED", channel=channel, period_chars=period)

    def finish(self):
        self.check()
        if self.pending_line and self.line_check is not None:
            self._check_line(self.pending_line.rstrip("\r"))
            if self.pending_line.strip():
                self.last_content_line = self.pending_line.rstrip("\r")
            self.pending_line = ""
        self._check_repetition("content")
        # reasoning 可能包含模型正常的长思考或重复校验；其唯一兜底是总时限。
