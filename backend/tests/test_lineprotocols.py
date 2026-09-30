"""split/translate 行协议产物的落盘格式测试。"""
import pytest

from dubforge.utils.lineprotocols import (
    parse_numbered_lines,
    parse_timed_lines,
    serialize_numbered_lines,
    serialize_timed_lines,
)


def test_timed_lines_round_trip_keeps_empty_and_ordered_payload():
    rows = [
        {"id": 1, "start": 0.1, "end": 1.234, "text": "hello"},
        {"id": 2, "start": 1.234, "end": 2.0, "text": "world"},
    ]
    raw = serialize_timed_lines(rows)
    assert raw == "1. 0.100-1.234: hello\n2. 1.234-2.000: world\n"
    assert parse_timed_lines(raw) == rows


def test_numbered_lines_round_trip_allows_optimized_empty_translation():
    raw = serialize_numbered_lines({2: "", 1: "第一句"})
    assert raw == "1. 第一句\n2. \n"
    assert parse_numbered_lines(raw) == {1: "第一句", 2: ""}


def test_protocol_parsers_reject_non_protocol_lines():
    with pytest.raises(ValueError, match="split 时间轴行格式错误"):
        parse_timed_lines("1. 0.0-1.0 缺少冒号")
    with pytest.raises(ValueError, match="编号译文行格式错误"):
        parse_numbered_lines("没有编号的译文")
