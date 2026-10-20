"""单帧预览有界运行，不因跨标签或快速重开页面积累编码任务。"""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from dubforge import comment_preview
from dubforge.comment_studio import CommentError


def test_preview_admits_two_renders_and_rejects_extra_without_queue(monkeypatch):
    started = threading.Barrier(3)
    release = threading.Event()
    calls = []

    def render(video, plan, seconds):
        calls.append(seconds)
        started.wait(timeout=5)
        assert release.wait(timeout=5)
        return b'frame', {'seconds': seconds}

    monkeypatch.setattr(comment_preview.comment_overlay, 'preview_result', render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(comment_preview.render, 'fixture.mp4', {}, index) for index in (1, 2)]
        try:
            started.wait(timeout=5)
            # 被拒请求不会进入渲染函数，也不会在旧请求完成后再后台执行。
            for _ in range(10):
                with pytest.raises(CommentError) as error:
                    comment_preview.render('fixture.mp4', {}, 3)
                assert error.value.code == 'COMMENTS_PREVIEW_BUSY'
                assert error.value.message == '其他预览正在生成，请稍后重试'
            assert sorted(calls) == [1, 2]
        finally:
            release.set()
        assert [future.result(timeout=5) for future in futures] == [
            (b'frame', {'seconds': 1}), (b'frame', {'seconds': 2})]
    assert sorted(calls) == [1, 2]
    # 正常完成释放名额，下一次普通请求不受此前忙碌影响。
    monkeypatch.setattr(comment_preview.comment_overlay, 'preview_result', lambda *args: (b'next', None))
    assert comment_preview.render('fixture.mp4', {}, 4) == (b'next', None)


def test_preview_failure_releases_slot_and_preserves_original_error(monkeypatch):
    def fail(*args):
        raise CommentError('当前位置没有画面', code='COMMENTS_PREVIEW_EMPTY')

    monkeypatch.setattr(comment_preview.comment_overlay, 'preview_result', fail)
    for _ in range(5):
        with pytest.raises(CommentError) as error:
            comment_preview.render('fixture.mp4', {}, 0)
        assert error.value.code == 'COMMENTS_PREVIEW_EMPTY'
    monkeypatch.setattr(comment_preview.comment_overlay, 'preview_result', lambda *args: (b'next', None))
    assert comment_preview.render('fixture.mp4', {}, 0) == (b'next', None)


def test_preview_result_passes_frame_and_layout_through_without_recalculation(monkeypatch):
    frame, details = b'frame', {'min_reached': True, 'height_exceeded': False}
    calls = []

    def render(*args):
        calls.append(args)
        return frame, details

    monkeypatch.setattr(comment_preview.comment_overlay, 'preview_result', render)
    plan = {'settings': {'width_percent': 100}, 'items': []}
    result = comment_preview.render('fixture.mp4', plan, 1.5)
    assert result == (frame, details)
    assert result[1] is details
    assert calls == [('fixture.mp4', plan, 1.5)]
