"""评论工作阶段的轻量投影；保存事实与制作就绪状态分开表达。"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class SavedOverlay:
    saved: bool = False
    count: int = 0
    signature: str = ''
    confirmed_at: str = ''
    unreadable: bool = False


@lru_cache(maxsize=64)
def _read_saved_overlay(path: str, modified_ns: int, size: int) -> SavedOverlay:
    """文件变化时仅解析一次，缓存只保留小摘要，不保留评论正文或译文。"""
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(document, dict):
            return SavedOverlay(unreadable=True)
        plan = document.get('overlay')
        if plan is None:
            return SavedOverlay()
        if (not isinstance(plan, dict) or not isinstance(plan.get('items'), list)
                or not isinstance(plan.get('settings'), dict)):
            return SavedOverlay(unreadable=True)
        signature = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return SavedOverlay(saved=True, count=len(plan['items']), signature=signature,
                            confirmed_at=plan.get('confirmed_at', '') if isinstance(plan.get('confirmed_at', ''), str) else '')
    except (OSError, ValueError, UnicodeError):
        return SavedOverlay(unreadable=True)


def saved_overlay(path: Path) -> SavedOverlay:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return SavedOverlay()
    except OSError:
        return SavedOverlay(unreadable=True)
    return _read_saved_overlay(str(path), stat.st_mtime_ns, stat.st_size)


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def _job_time(job):
    return next((stamp for key in ('finished_at', 'updated_at', 'started_at')
                 if (stamp := _timestamp(job.get(key))) is not None),
                datetime.min.replace(tzinfo=timezone.utc))


def workspace(saved: SavedOverlay, *, collection=None, jobs=None, prepare_job=None) -> dict:
    """只判断真实操作与是否保存，不检查评分有效性、审查或渲染画幅。"""
    collection, jobs, preparation = collection or {}, jobs or {}, prepare_job or {}
    records = [job for job in (*jobs.values(), collection, preparation) if isinstance(job, dict) and job]
    saved_note = (f'已保存 {saved.count} 条上屏评论。' if saved.count
                  else '已保存空上屏方案，当前选择不叠加评论。') if saved.saved else ''

    def result(state, label, detail):
        return dict(state=state, label=label, detail=detail, confirmed_count=saved.count)

    if collection.get('busy') or collection.get('status') == 'running':
        return result('running', '正在获取评论', saved_note + '正在获取平台评论；已有条数是缓存快照。')
    active = next((job for job in jobs.values() if isinstance(job, dict) and job.get('status') == 'running'), None)
    if active:
        action = '翻译评论' if active.get('action') == 'translate' else '评论评分'
        return result('running', f'正在{action}', saved_note + f'正在{action}，完成结果会保留。')
    if preparation.get('status') == 'running':
        return result('running', '正在准备上屏评论', saved_note + '正在复用评分、按需翻译并整理上屏草稿。')
    if saved.unreadable:
        return result('attention', '上屏方案待核对', '无法读取已保存的评论方案，请进入评论页检查；没有重写资料。')

    # 比较保存内容签名，而非工作区文件 stat；评分和翻译也会更新同一文件。
    base_signature = preparation.get('overlay_signature')
    preparation_confirmed = saved.saved and (
        preparation.get('confirmed_overlay_signature') == saved.signature
        or isinstance(base_signature, str) and bool(base_signature) and base_signature != saved.signature)
    prepared = preparation.get('status') in {'done', 'partial'}
    if prepared and not preparation_confirmed:
        known_unsaved = isinstance(base_signature, str) and bool(base_signature)
        newer = _job_time(preparation) > (_timestamp(saved.confirmed_at) or datetime.min.replace(tzinfo=timezone.utc))
        if not saved.saved or known_unsaved or newer:
            return result('attention', '待确认方案', saved_note + '评论草稿已准备，尚未确认保存；请进入评论页核对。')

    latest = max(records, key=_job_time, default={})
    incomplete = latest.get('status') in {'failed', 'partial', 'interrupted', 'cancelled'}
    if saved.saved:
        confirmed_time = _timestamp(saved.confirmed_at)
        after_save = confirmed_time is not None and _job_time(latest) > confirmed_time
        failed_preparation = (latest is preparation and isinstance(base_signature, str)
                              and base_signature == saved.signature and not preparation_confirmed)
        if incomplete and (after_save or failed_preparation):
            return result('attention', '本次评论处理需检查', saved_note + '后续处理未完成，已有方案仍保留；请查看评论进度和错误。')
        detail = saved_note + ('另有未完成处理记录，可在评论页查看。' if incomplete else '')
        return result('done', '已保存', detail + '此状态不代表已核验当前规则与成片排版。')
    if incomplete:
        return result('failed' if latest.get('status') == 'failed' else 'attention', '评论处理未完成',
                      '尚未保存上屏方案；已完成的评分与译文仍保留，可进入评论页继续。')
    available = collection.get('count')
    detail = f'已获取 {available} 条评论，尚未保存上屏方案。' if type(available) is int and available > 0 else '可按需获取、评分和翻译评论，再确认上屏方案。'
    return result('pending', '待准备', detail)
