"""发布记录的本地生命周期；回收保留原行，远端身份与防重复证据永不清除。"""
import json
import re

from sqlmodel import select, or_

from dubforge.domain.models import Project
from dubforge.project_archive import archived_at_iso
from dubforge.publishing.models import Publication, PublishChange, PublicationUpdate


FILTERS = frozenset({'active', 'archived', 'deleted', 'all'})


def state(row):
    return 'deleted' if row.deleted_at is not None else 'archived' if row.archived_at is not None else 'active'


def assert_active(row, *, allow_archived_read=False):
    from dubforge.publishing.service import PublishError
    if (allow_archived_read and state(row) == 'archived'
            and re.fullmatch(r'BV[0-9A-Za-z]{10}', str(json.loads(row.receipt).get('bvid', '')))):
        return
    if state(row) != 'active':
        label = '已回收' if row.deleted_at is not None else '已归档'
        raise PublishError(f'此本地发布记录{label}，请先恢复再操作', code='PUBLISH_RECORD_INACTIVE')


def apply_filter(query, value):
    from dubforge.publishing.service import PublishError
    if not isinstance(value, str) or value not in FILTERS:
        raise PublishError('发布记录范围无效，请选择当前、归档或回收站', code='PUBLISH_RECORD_FILTER')
    if value == 'active':
        return query.where(Publication.archived_at.is_(None), Publication.deleted_at.is_(None))
    if value == 'archived':
        return query.where(Publication.archived_at.is_not(None), Publication.deleted_at.is_(None))
    if value == 'deleted':
        return query.where(Publication.deleted_at.is_not(None))
    return query


def context(session, rows):
    """整页批量核对占用，不随每条记录重复扫描项目和任务表。"""
    from dubforge import runtime_edit, workflows, release_batches
    from dubforge.publishing import service, studio, chapter_dispatch
    from dubforge.workflow_models import WorkflowRun
    from dubforge.release_batch_models import ReleaseBatch
    if not rows:
        return {}
    uids = {row.project_uid for row in rows}
    reasons = {}
    for uid in uids:
        if uid in workflows._leases:
            reasons[uid] = '来源项目的完整流程正在执行或收尾，请先停止流程'
        if uid in release_batches._leases:
            reasons[uid] = '来源项目属于正在执行的顺序发布批次，请先暂停或停止批次'
    existing = session.exec(select(Project.uid).where(Project.uid.in_(uids))).all()
    report = runtime_edit._report(session, existing)
    # ADR-0132 将设置字段锁与项目级资源锁分开：运行中的步骤可以让无关
    # 设置继续可写，但归档/回收等发布记录管理仍必须看 resource_project_uids。
    for uid in report.get('resource_project_uids', report['project_uids']):
        reasons[uid] = '来源项目有任务正在执行或后台收尾，请先停止并等待结束'
    runs = session.exec(select(WorkflowRun).where(WorkflowRun.project_uid.in_(uids),
        or_(WorkflowRun.status.in_(workflows.OPEN), WorkflowRun.id.in_(tuple(workflows._running))))).all()
    for run in runs:
        reasons[run.project_uid] = '来源项目的完整流程仍在执行或跟踪审核，请先停止流程'
    batches = session.exec(select(ReleaseBatch).where(or_(ReleaseBatch.status.in_(release_batches.ACTIVE),
        ReleaseBatch.id.in_(tuple(release_batches._running))))).all()
    for batch in batches:
        for item in json.loads(batch.items):
            if isinstance(item, dict) and item.get('project_uid') in uids:
                reasons[item['project_uid']] = '来源项目属于正在执行的顺序发布批次，请先暂停或停止批次'
    changes = session.exec(select(PublishChange).where(or_(PublishChange.status.in_({'committing', 'unknown'}),
        PublishChange.id.in_(tuple(studio._inflight))))).all()
    result = {}
    updates = session.exec(select(PublicationUpdate).where(PublicationUpdate.status.in_(
        {'queued', 'uploading', 'submitting', 'waiting', 'unknown', 'delivering'}))).all()
    for row in rows:
        reason = reasons.get(row.project_uid, '')
        if row.status in service.ACTIVE | {'preparing', 'stopping'} or row.id in service._running:
            reason = '本地发布记录正在上传、投稿、查询或后台收尾，请等待结束'
        elif row.id in service._suggesting:
            reason = '本地发布记录正在生成文案，请等待结束'
        elif row.id in chapter_dispatch._running:
            reason = '本地发布记录正在处理视频章节，请等待结束'
        elif row.status == 'unknown':
            reason = '投稿结果尚未确认，请先核对并关联原BV回执'
        snapshot, receipt = json.loads(row.snapshot), json.loads(row.receipt)
        account = snapshot.get('account_id') or json.loads(row.draft).get('account_id')
        bvid = receipt.get('bvid')
        if any(change.publication_id == row.id or (bvid and change.account_id == account and change.bvid == bvid)
               for change in changes):
            reason = '稿件修改正在提交或结果尚未确认，请先核对修改结果'
        if any(update.source_publication_id == row.id for update in updates):
            reason = '原稿更新尚未完成，请先暂停或核对更新任务后收纳记录'
        result[row.id] = reason
    return result


def describe(row, busy_reason=''):
    value = state(row)
    clean_draft = (row.status == 'draft' and not json.loads(row.snapshot)
                   and not {key: value for key, value in json.loads(row.transfers).items() if key != '_progress'}
                   and not json.loads(row.receipt))
    archive_reason = busy_reason or ('此本地记录已回收，请先恢复' if value == 'deleted'
                                    else '此本地记录已归档' if value == 'archived' else '')
    delete_reason = busy_reason or ('此本地记录已在回收站' if value == 'deleted' else '' if clean_draft
                                    else '仅未上传的草稿可回收；上传或投稿记录请归档，保留回执与防重复保护')
    restore_reason = busy_reason or ('此本地记录已在当前列表' if value == 'active' else '')
    return dict(lifecycle=value, archived_at=archived_at_iso(row.archived_at),
        deleted_at=archived_at_iso(row.deleted_at), lifecycle_actions={
            'archive': dict(allowed=not archive_reason, reason=archive_reason),
            'delete': dict(allowed=not delete_reason, reason=delete_reason),
            'restore': dict(allowed=not restore_reason, reason=restore_reason)})
