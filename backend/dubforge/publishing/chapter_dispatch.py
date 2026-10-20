"""章节交付的轻量调度；网络和 LLM 不占用审核监控线程。"""
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.publishing import service
from dubforge.publishing.models import Publication, PublicationUpdate

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='publish-chapters')
_running = set()
_closing = True
APPROVED = {'published', 'private', 'reviewed'}


def _assert_current(session, row):
    """旧章节只消费原上传绑定；换片任务接管后不能再次套用旧时间轴。"""
    from dubforge.publishing import updates
    active = session.exec(select(PublicationUpdate).where(
        PublicationUpdate.source_publication_id == row.id,
        PublicationUpdate.status.in_(updates.OPEN))).first()
    if active:
        raise service.PublishError('原稿更新正在执行或等待核对，请在更新原稿件任务中继续章节',
                                   code='PUBLISH_UPDATE_BUSY')
    snapshot, receipt = json.loads(row.snapshot), json.loads(row.receipt)
    account, bvid = snapshot.get('account_id'), receipt.get('bvid')
    if account and bvid:
        updates.assert_available(session, account=account, bvid=bvid)
    if updates.superseding_update(row.id):
        raise service.PublishError('原视频已由新成片替换，请在更新原稿件任务中继续章节；不会重新套用旧视频时间轴',
                                   code='PUBLISH_CHAPTER_SUPERSEDED')


def startup():
    global _closing
    from dubforge.publishing import chapter_sync
    chapter_sync.startup()
    chapter_sync.recover()
    with _lock:
        _closing = False


def shutdown():
    global _closing
    with _lock:
        _closing = True
    from dubforge.publishing import chapter_sync
    chapter_sync.shutdown()


def _execute(key, reconcile_only=False):
    try:
        with _lock:
            if _closing:
                return
        with service._lock, service._execution_gate(), Session(get_engine()) as session:
            try:
                _assert_current(session, service._get(session, Publication, key))
            except service.PublishError as exc:
                if exc.code in {'PUBLISH_UPDATE_BUSY', 'PUBLISH_CHAPTER_SUPERSEDED'}:
                    return
                raise
        from dubforge.publishing import chapter_sync
        from dubforge import workflows
        if not reconcile_only and workflows.post_processing_paused(key):
            return
        # 手动发布没有 WorkflowRun 跟踪审核，也须在后台只读检查，不能依赖网页常开。
        publication = service.view(key)
        if publication['status'] == 'submitted' and not reconcile_only:
            _check_review(key)
            return
        chapter_sync.run(key, reconcile_only=reconcile_only)
    except Exception:
        # 领域模块负责持久化可恢复错误；意外异常也不可变成重新投稿。
        logger.exception('章节后台任务未完成：%s', key)
    finally:
        with _lock:
            _running.discard(key)
        # 章节重试成功后让原流程重新读取结果，恢复不经过上传或投稿分支。
        from dubforge import workflows
        workflows.chapter_status_changed(key)


def _enqueue(key, *, reconcile_only=False):
    # 与原稿更新的启动使用相同门禁；占位成功后更新任务不得越过旧章节。
    with service._lock, service._execution_gate(), _lock:
        if _closing or key in _running:
            return False
        _running.add(key)
        try:
            _pool.submit(_execute, key, reconcile_only)
        except Exception:
            _running.discard(key)
            raise service.PublishError('章节任务未能加入后台队列，请稍后重试', code='PUBLISH_CHAPTER_QUEUE') from None
        return True


def scan():
    """只调度明确开启的新流程；旧稿件关闭的开关绝不追溯打开。"""
    with _lock:
        if _closing:
            return
    with Session(get_engine()) as session:
        rows = session.exec(select(Publication).where(Publication.status.in_(APPROVED | {'submitted'}),
            Publication.archived_at.is_(None), Publication.deleted_at.is_(None))).all()
        from dubforge.workflow_models import WorkflowRun
        tracked = set(session.exec(select(WorkflowRun.publication_id).where(
            WorkflowRun.status.in_({'queued', 'running', 'stopping', 'monitoring'}))).all())
        paused = set(session.exec(select(WorkflowRun.publication_id).where(
            WorkflowRun.post_processing_paused.is_(True))).all())
        due = []
        for row in rows:
            if row.id in paused:
                continue
            try:
                options = json.loads(row.snapshot).get('chapter_options') or {}
                state = json.loads(row.chapter_sync)
                next_check = state.get('next_check', 0)
                if (not isinstance(options, dict) or not isinstance(state, dict)
                        or not isinstance(next_check, (int, float)) or not math.isfinite(next_check)):
                    raise ValueError()
                if row.status == 'submitted' and (row.id in tracked or not options.get('items')
                        or not json.loads(row.receipt).get('bvid')):
                    continue
                if (options.get('enabled') is True and state.get('status') in {None, 'pending'}
                        and next_check <= time.time()):
                    due.append(row.id)
            except (ValueError, TypeError, AttributeError):
                # 损坏的单条记录不能让其它稿件的审核/章节监控全部停住。
                logger.warning('跳过无法读取的章节调度记录：%s', row.id)
    for key in due:
        with service._lock, service._execution_gate(), Session(get_engine()) as session:
            if key in service._running:
                continue
            try:
                _assert_current(session, service._get(session, Publication, key))
            except service.PublishError:
                # 原稿更新有独立章节交付；旧绑定与历史保持不动。
                continue
            _enqueue(key)


def _check_review(key):
    """手动稿件复用只读审核动作；先落节流时间，再入队，不直接刷新覆盖回执。"""
    from dubforge import release_batches, workflows
    if release_batches.owns_publication(key) or workflows.post_processing_paused(key):
        return
    with service._lock, service._execution_gate(), Session(get_engine()) as session:
        row = service._get(session, Publication, key)
        try:
            _assert_current(session, row)
        except service.PublishError as exc:
            if exc.code in {'PUBLISH_UPDATE_BUSY', 'PUBLISH_CHAPTER_SUPERSEDED'}:
                return
            raise
        state = json.loads(row.chapter_sync)
        if (row.status != 'submitted' or key in service._running
                or row.archived_at is not None or row.deleted_at is not None):
            return
        state.update(schema=1, status='pending', phase='等待审核通过后设置章节',
                     next_check=time.time() + 60)
        row.chapter_sync = service._json(state)
        revision = row.revision
        session.add(row); session.commit()
    service.enqueue(key, {'action': 'check', 'revision': revision})


def request(key, body):
    """明确授权只补充/重试章节；结果不明时入口只能执行只读核对。"""
    if (not isinstance(body, dict) or set(body) - {'confirmed', 'enable', 'reconcile_only'}
            or body.get('confirmed') is not True
            or any(type(body[name]) is not bool for name in ('enable', 'reconcile_only') if name in body)):
        raise service.PublishError('请确认本次仅同步视频章节', code='PUBLISH_CHAPTER_CONFIRM')
    from dubforge.publishing import chapter_sync, lifecycle
    reconcile_only = body.get('reconcile_only', False)
    if reconcile_only and body.get('enable'):
        raise service.PublishError('核对结果不会同时启用新章节，请分别操作', code='PUBLISH_CHAPTER_INVALID')
    with service._lock, service._execution_gate():
        with Session(get_engine()) as session:
            row = service._get(session, Publication, key)
            lifecycle.assert_active(row)
            _assert_current(session, row)
        publication = service.view(key)
        if publication['status'] not in APPROVED or key in service._running:
            raise service.PublishError('请等待稿件审核通过后再处理章节', code='PUBLISH_CHAPTER_NOT_READY')
        with _lock:
            if _closing:
                raise service.PublishError('服务正在关闭，请重新打开后重试', code='PUBLISH_CHAPTER_QUEUE')
            if key in _running:
                return service.view(key)
            state = publication.get('chapter_sync') or {}
            if reconcile_only:
                if state.get('status') != 'unknown':
                    raise service.PublishError('当前没有需要核对的章节请求，请刷新状态', code='PUBLISH_CHAPTER_INVALID')
            else:
                chapter_sync.reset_for_retry(key, enable=body.get('enable', False))
                from dubforge import workflows
                workflows.resume_post_processing(key)
            _enqueue(key, reconcile_only=reconcile_only)
    return service.view(key)
