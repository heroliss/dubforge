"""评论单帧预览的轻量资源准入；草稿与正式制作状态互不修改。"""
import threading

from dubforge.comment_studio import CommentError
from dubforge.pipeline import comment_overlay


# 普通页面自身串行；两个名额允许离开页面后的旧请求与新页面短暂交叠。
# 更多标签不排后台队列，避免连续切换页面积累无用的 FFmpeg 进程。
_RENDERING = threading.BoundedSemaphore(2)


def render(video, plan, seconds):
    if not _RENDERING.acquire(blocking=False):
        raise CommentError('其他预览正在生成，请稍后重试', code='COMMENTS_PREVIEW_BUSY',
                           suggestion='等待正在生成的预览完成后，点击“重试预览”。')
    try:
        return comment_overlay.preview_result(video, plan, seconds)
    finally:
        _RENDERING.release()
