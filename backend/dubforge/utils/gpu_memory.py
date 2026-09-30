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


def free_mb() -> int | None:
    """设备级空闲显存 (MiB, 含其他进程占用后的余量); torch 不可用或无 CUDA 时 None。"""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        free, _total = torch.cuda.mem_get_info()
        return int(free // (1024 * 1024))
    except Exception:
        return None
