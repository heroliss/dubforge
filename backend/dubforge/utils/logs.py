"""日志尾部读取：轮询开销不随完整日志体积增长。"""
import os
import hashlib
import json
from collections import deque
from pathlib import Path

from dubforge.errors import DubForgeError


def read_tail(path: Path, count: int, *, max_bytes: int = 1024 * 1024) -> tuple[list[str], bool]:
    """倒序分块读最近若干行；超长单行也受字节预算限制，并返回截断标记。"""
    if count < 1 or max_bytes < 1:
        raise DubForgeError("日志行数和读取字节上限必须大于零", code="LOG_TAIL_INVALID_INPUT")
    chunks = []
    newlines = consumed = 0
    with path.open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        # 多找一个换行，确保舍弃分块开头可能不完整的行。
        while position > 0 and newlines <= count and consumed < max_bytes:
            size = min(position, 8192, max_bytes - consumed)
            position -= size
            stream.seek(position)
            chunk = stream.read(size)
            chunks.append(chunk)
            consumed += size
            newlines += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    # 字节预算可能恰好切在中文字符中间，跳过开头的 UTF-8 续字节。
    if position > 0:
        offset = 0
        while offset < len(data) and 0x80 <= data[offset] <= 0xBF:
            offset += 1
        data = data[offset:]
    lines = data.decode("utf-8", errors="replace").splitlines()
    truncated = position > 0 and len(lines) <= count
    return lines[-count:], truncated


def read_llm_trace(path: Path, *, tail=500, after=None, trace_id=''):
    """按字节游标增量读对话；新任务、半行与并发轮转不混入旧对话。"""
    events, cursor = [], 0
    try:
        with path.open('rb') as stream:
            first = stream.readline()
            if not first.endswith(b'\n'):
                return dict(events=[], exists=True, retry=True)
            generation = hashlib.sha256(first).hexdigest()
            reset = after is not None and (trace_id != generation or after > os.fstat(stream.fileno()).st_size)
            stream.seek(0 if after is None or reset else after)
            if after is None:
                lines = deque(stream, maxlen=tail)
                cursor = stream.tell()
            else:
                lines = []
                cursor = stream.tell()
                for _ in range(tail):
                    line = stream.readline()
                    if not line.endswith(b'\n'):
                        break
                    lines.append(line)
                    cursor = stream.tell()
            more = cursor < os.fstat(stream.fileno()).st_size
            for line in lines:
                if not line.endswith(b'\n'):
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        events.append(item)
                except ValueError:
                    continue
    except OSError:
        return dict(events=[], exists=False, active=False)
    starts = [i for i, event in enumerate(events) if event.get('event') == 'run_start']
    if starts:
        events = events[starts[-1]:]
    return dict(events=events, exists=True, cursor=cursor, trace_id=generation, reset=reset, more=more)
