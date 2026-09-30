"""在模型步骤交接时归还空闲显存；不加载 CUDA，也不触碰仍在使用的张量。"""
import gc
import sys


def release_unused(log=lambda message: None):
    torch = sys.modules.get('torch')
    try:
        if torch is None or not torch.cuda.is_initialized():
            return
        gc.collect()
        before = torch.cuda.memory_reserved()
        torch.cuda.empty_cache()
        after = torch.cuda.memory_reserved()
        if before > after:
            log(f'GPU 资源交接：已归还空闲显存缓存 {(before - after) / 1024**2:.0f} MiB，供后续模型使用')
    except Exception as exc:
        # 资源统计失败不能覆盖原始步骤结果；活跃模型的显存不会被强制释放。
        log(f'GPU 空闲缓存释放未完成：{type(exc).__name__}；请检查显存占用')
