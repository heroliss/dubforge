"""用 IndexTTS 自带环境运行 WebUI，将内部迭代进度接入 Gradio。

只在服务进程内启用进度跟踪，不改第三方文件、不从共享日志推断任务归属。
此文件由独立 Python 直接执行，因此不依赖 DubForge 的运行环境。
"""
import re
import runpy
import sys
from functools import wraps
from pathlib import Path


def enable_progress(progress_class, local_context=None):
    if getattr(progress_class, '_dubforge_tracking', False):
        return
    original_init = progress_class.__init__
    original_call = progress_class.__call__
    original_tqdm = progress_class.tqdm

    def init(self, track_tqdm=True):
        original_init(self, track_tqdm=track_tqdm)
        self._dubforge_segment = None

    def report(self, progress, desc=None, *args, **kwargs):
        # IndexTTS 的 progress 在 *args 后，Gradio 不会自动绑定 tqdm 跟踪器。
        # 在当前事件上下文中创建独立对象，避免使用函数默认对象共享迭代状态。
        if local_context is not None and self.track_tqdm and local_context.event_id.get(None):
            tracker = local_context.progress.get(None)
            if tracker is None:
                tracker = progress_class(track_tqdm=True)
                local_context.progress.set(tracker)
            self = tracker
        match = re.fullmatch(r'speech synthesis (\d+)/(\d+)\.\.\.', desc or '')
        self._dubforge_segment = match.groups() if match else None
        return original_call(self, progress, desc, *args, **kwargs)

    def track(self, iterable=None, desc=None, *args, **kwargs):
        segment = self._dubforge_segment
        if segment and not desc:
            # 每次迭代都附带分段号，客户端漏过短暂的阶段事件也能正确定位。
            desc = f'DubForge speech {segment[0]}/{segment[1]}'
        return original_tqdm(self, iterable, desc, *args, **kwargs)

    progress_class.__init__ = init
    progress_class.__call__ = report
    progress_class.tqdm = track
    progress_class._dubforge_tracking = True


def main():
    import gradio as gr
    from gradio.context import LocalContext
    enable_progress(gr.Progress, LocalContext)
    enable_error_details(gr.Blocks)
    script = Path(sys.argv[1]).resolve()
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(script.parent))
    print('DubForge：已启用 IndexTTS 迭代进度上报', flush=True)
    runpy.run_path(str(script), run_name='__main__')


def enable_error_details(blocks_class):
    """只对本程序管理的服务启用原始错误回执，不修改第三方 WebUI 文件。"""
    if getattr(blocks_class.launch, '_dubforge_diagnostics', False):
        return
    launch = blocks_class.launch

    @wraps(launch)
    def with_details(self, *args, **kwargs):
        kwargs.setdefault('show_error', True)
        return launch(self, *args, **kwargs)

    with_details._dubforge_diagnostics = True
    blocks_class.launch = with_details


if __name__ == '__main__':
    main()
