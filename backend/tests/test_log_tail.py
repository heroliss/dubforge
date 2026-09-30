"""日志轮询按尾部读取，兼容中文、换行、超长行与文件缺失。"""
import io
from pathlib import Path

import pytest
from fastapi import Response

from dubforge.domain import crud
from dubforge.utils.logs import read_tail


@pytest.mark.parametrize("content", [
    "", "中文", "中文\n", "第一行\r\n第二行\r\n", "第一行\n\n最后一行",
    "第一行\r第二行\r", "\n\n\n", "有制表符\t内容\n末行\n",
    "字幕内容\n" * 2000 + "结尾中文",
], ids=["empty", "single", "trailing-newline", "crlf", "blank-line", "cr-only",
        "empty-lines", "tab", "multi-block-chinese"])
@pytest.mark.parametrize("count", [1, 3, 200])
def test_tail_matches_normal_log_lines(isolated_env, content, count):
    path = isolated_env / "step.log"
    path.write_bytes(content.encode("utf-8"))
    lines, truncated = read_tail(path, count)
    assert lines == content.splitlines()[-count:]
    assert not truncated


def test_tail_reads_bounded_bytes_for_large_log(monkeypatch):
    class TrackedFile(io.BytesIO):
        bytes_read = 0

        def read(self, size=-1):
            assert size > 0
            result = super().read(size)
            self.bytes_read += len(result)
            return result

    stream = TrackedFile(("中文日志内容\r\n" * 100000).encode("utf-8"))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: stream)
    lines, truncated = read_tail(Path("ignored.log"), 200)
    assert len(lines) == 200
    assert all(line == "中文日志内容" for line in lines)
    assert stream.bytes_read == 8192
    assert not truncated


def test_huge_single_line_has_byte_budget_and_utf8_boundary(isolated_env):
    path = isolated_env / "step.log"
    path.write_bytes(("中文" * 10000).encode("utf-8"))
    lines, truncated = read_tail(path, 200, max_bytes=1024)
    assert truncated
    assert len(lines) == 1
    assert "\ufffd" not in lines[0]
    assert len(lines[0].encode("utf-8")) <= 1024


@pytest.mark.parametrize("count,max_bytes", [(0, 1024), (1, 0)])
def test_invalid_tail_budget_has_domain_error(isolated_env, count, max_bytes):
    from dubforge.errors import DubForgeError

    with pytest.raises(DubForgeError) as exc:
        read_tail(isolated_env / "absent.log", count, max_bytes=max_bytes)
    assert exc.value.code == "LOG_TAIL_INVALID_INPUT"


def test_log_api_missing_file_unknown_step_and_chinese(make_project):
    from app.api.steps import step_log
    from dubforge.errors import DubForgeError

    uid = make_project()
    response = Response()
    result = step_log(uid, "download", response, tail=2)
    assert result == {"lines": [], "exists": False, "truncated": False}
    assert response.headers["Cache-Control"] == "no-store"
    path = crud.project_dir(uid) / "logs" / "download.log"
    path.write_bytes("第一行\r\n第二行\r\n第三行\r\n".encode("utf-8"))
    assert step_log(uid, "download", Response(), tail=2)["lines"] == ["第二行", "第三行"]
    with pytest.raises(DubForgeError) as exc:
        step_log(uid, "unknown", Response(), tail=2)
    assert exc.value.code == "STEP_UNKNOWN"
