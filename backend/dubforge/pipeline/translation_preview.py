"""翻译窗口的过程快照；独立于正式产物，不参与下游输入与指纹。"""
import json
from pathlib import Path

from dubforge.utils.fs import atomic_write_bytes


def _path(pdir: Path, job_id: int) -> Path:
    return pdir / 'cache' / 'translate' / f'progress_{int(job_id)}.json'


def save(ctx, text: str, completed: int, total: int) -> None:
    ctx.check_cancel()
    job_id = getattr(ctx, 'job_id', None) or 0
    payload = dict(job_id=job_id, text=text, completed=completed, total=total)
    atomic_write_bytes(_path(ctx.pdir, job_id), json.dumps(payload, ensure_ascii=False).encode('utf-8'))


def read(pdir: Path, job_id: int) -> dict | None:
    """只读取指定任务的快照；旧任务迟到写入或坏缓存不能冒充本轮结果。"""
    try:
        value = json.loads(_path(pdir, job_id).read_text(encoding='utf-8'))
        if (isinstance(value, dict) and value.get('job_id') == job_id
                and isinstance(value.get('text'), str)
                and type(value.get('completed')) is int and type(value.get('total')) is int
                and 0 < value['completed'] <= value['total']):
            return value
    except (OSError, ValueError, UnicodeError):
        pass
    return None


def available(pdir: Path, job_id: int) -> bool:
    """状态轮询只看快照是否存在，不反复读取整份长译文。"""
    try:
        return _path(pdir, job_id).is_file()
    except OSError:
        return False


def clear(ctx) -> None:
    try:
        _path(ctx.pdir, getattr(ctx, 'job_id', None) or 0).unlink(missing_ok=True)
    except OSError:
        # 过程快照清理失败不能把已提交的完整译文判为失败；任务身份仍防止串用。
        ctx.log('翻译过程预览暂未清理，完整产物不受影响。')
