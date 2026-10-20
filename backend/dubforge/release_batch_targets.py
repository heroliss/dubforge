"""新顺序发布只编排项目已保存的投稿目标；历史批次继续使用旧冻结配方。"""
import copy
import json

from dubforge import configuration, workflows
from dubforge.publishing import service
from dubforge.workflow_plan import digest


def saved_scope(uid):
    values = configuration.resolve(uid)['values']
    return {key: values['publish.' + key] for key in ('account_id', 'season_id', 'section_id')}


def candidate(uid):
    from dubforge import release_batches as batches
    spec = configuration.run_spec(uid, target_driven=True)
    target = spec.get('update') or {}
    row = batches._candidate(uid, target.get('publication_id', ''), new_publication=spec.get('mode') != 'update')
    row['workflow_id'] = ''
    status = workflows.status(uid)
    if spec.get('mode') == 'update':
        row['blockers'] = [reason for reason in row['blockers'] if reason != '稿件审核未通过，请先处理原稿件']
        row['publication_action'] = 'update_existing'
        # 新批次不能接管同项目任意历史流程；真正创建后才绑定自己的执行行。
    elif status['has_remote_publication'] and not spec.get('confirm_new_publication'):
        row['publication_id'], row['publication_action'] = '', 'prepare'
        row['blockers'] = [reason for reason in row['blockers'] if reason != '项目有多份发布记录，请在本批次明确选择要继续的稿件']
        row['blockers'].append('项目已有BV；另发新BV请先使用单项目运行明确确认，顺序批次不会选择最近稿件或重复投稿。')
    row.update(target_recipe=spec, saved_scope=saved_scope(uid),
               project_goal=status['project_goal'])
    if spec.get('delivery_mode') != 'publish':
        row['blockers'].append('顺序发布只接收已保存为“上传并提交”的项目，请到发布步骤修改；不会自动开启投稿。')
    return row


def item_plan(row, scope, *, online=True, saved=None, collection=None):
    from dubforge import release_batches as batches
    uid = row['project_uid']
    spec = copy.deepcopy(saved['recipe']) if saved else configuration.run_spec(uid, target_driven=True)
    blockers = list(row['blockers'])
    if spec.get('mode') != 'update' and not (saved and saved.get('workflow_id')):
        # 恢复尚未启动的明确新稿时，也不能借候选扫描选中最近的旧稿件。
        row = {**row, 'publication_id': '', 'workflow_id': ''}
        from sqlmodel import Session
        from dubforge.db import get_engine
        with Session(get_engine()) as session:
            blockers.extend(workflows._new_publication_issues(session, uid, spec))
    if spec.get('mode') == 'update':
        blockers = [reason for reason in blockers if reason != '稿件审核未通过，请先处理原稿件']
    publication = service.view(row['publication_id']) if row.get('publication_id') else None
    continue_upload = publication and not publication['receipt'].get('bvid') and publication['status'] != 'draft'
    stored_run = workflows.get(row['workflow_id']) if row.get('workflow_id') else None
    own_receipt = bool(saved and stored_run and publication and publication['receipt'].get('bvid')
        and saved.get('workflow_id') == stored_run['id'] and stored_run['project_uid'] == uid
        and stored_run['publication_id'] == publication['id'] and stored_run['recipe'] == spec)
    update_task = workflows._update_task(stored_run) if stored_run and spec.get('mode') == 'update' else None
    configured = row.get('saved_scope') or saved_scope(uid)
    if saved:
        configured = dict(account_id=spec.get('account_id', ''),
            season_id=saved['plan'].get('settings', {}).get('season_id'),
            section_id=saved['plan'].get('settings', {}).get('section_id')) if spec.get('mode') != 'update' else scope
    if spec.get('delivery_mode') != 'publish':
        blockers.append('顺序发布只接收已保存为“上传并提交”的项目，请到发布步骤修改；不会自动开启投稿。')
    if configured != batches._scope_identity(scope):
        blockers.append('项目保存的账号、合集或小节与其他项目不同，请在发布步骤统一后重新检查。')
    if blockers:
        checked = dict(recipe=spec, steps=[], labels={}, version_specs=[], blockers=blockers, warnings=[],
                       fingerprint=digest(dict(recipe=spec, blockers=blockers)), settings={})
    elif update_task:
        # 视频编辑成功后的远端版本理应变化，恢复只核对原不可变任务，不能再拿旧远端版本重建方案。
        checked = copy.deepcopy(stored_run['plan'])
        checked['recipe'], checked['blockers'] = stored_run['recipe'], []
        checked['recovery'], checked['recovery_fingerprint'] = workflows._update_recovery(stored_run, update_task)
        checked['publication_update'] = update_task
        if update_task['status'] == 'unknown':
            checked['_update_reconcile_once'] = True
    elif own_receipt and spec.get('mode') != 'update':
        # 仅恢复本批次自己创建并绑定的同一流程/稿件，不能猜测最近一条历史BV。
        checked = copy.deepcopy(stored_run['plan'])
        checked.update(recipe=spec,steps=[],labels={},blockers=[],warnings=[
            '继续核对本批次已绑定的原投稿回执，不重新制作或另发新BV。'])
    elif continue_upload and spec.get('mode') != 'update':
        checked = dict(recipe=spec, steps=[], labels={}, version_specs=[], blockers=[], warnings=[
            '继续已上传记录的固定文件与资料，不按后来制作设置重新生成。'],
            fingerprint=digest(dict(recipe=spec, publication=publication['id'], revision=publication['revision'],
                                    snapshot=publication['snapshot'])), settings=publication['snapshot'].get('settings', {}))
    else:
        checked = workflows.prepared_plan(uid, spec, online=online,
            dubbing_operation_id=row.get('workflow_id') or None,
            previous_update=(saved or {}).get('plan', {}).get('update_plan'))
        blockers.extend(checked['blockers'])
    binding = None
    action = 'update_existing' if spec.get('mode') == 'update' else 'prepare'
    if action == 'update_existing':
        key = spec['update']['publication_id']
        if row.get('publication_id') != key:
            blockers.append('原稿目标与保存的项目设置不一致，请重新检查。')
        source = service.view(key)
        if source.get('snapshot', {}).get('account_id') != scope['account_id']:
            blockers.append('原稿账号与本批次已保存的发布账号不同，不能跨账号更新原稿。')
        binding = dict(id=key, revision=source['revision'], bvid=source['receipt'].get('bvid'),
                       aid=source['receipt'].get('aid'), reuse_existing=False)
        if (collection is not None and not batches._collection_contains(collection, source['receipt'])
                and not checked.get('update_plan', {}).get('collection', {}).get('enabled')):
            blockers.append('原稿不在项目保存的目标小节，且未选择同步合集；请到发布步骤明确合集更新范围。')
    elif row.get('publication_id'):
        source = service.view(row['publication_id'])
        if source['receipt'].get('bvid'):
            if own_receipt:
                binding = batches._publication_binding(source,scope,collection=collection)
                action = 'continue'
            else:
                blockers.append('项目已存在BV；请在发布步骤明确选择更新原稿，顺序发布不会猜测或另发新BV。')
        elif source['status'] == 'draft':
            binding = batches._publication_binding(source, scope, collection=collection, prepare=True)
            checked.update(refresh_publication=source['id'], publication_binding=binding)
        else:
            binding = batches._publication_binding(source, scope, collection=collection)
            action = 'continue'
            checked['steps'], checked['labels'] = [], {}
    checked.setdefault('recovery', dict(rows=[]))
    if row.get('workflow_id') and not row.get('publication_id'):
        checked, _ = workflows._recovery_plan(workflows.get(row['workflow_id']), checked)
    return {**row, 'recipe': checked['recipe'], 'plan': checked, 'publication': binding,
        'selected_version_ids': checked['recipe'].get('version_ids') or [], 'publication_action': action,
        'production': checked.get('recovery', {}), 'blockers': list(dict.fromkeys(blockers)),
        'status': saved['status'] if saved else 'queued', 'error': '',
        'phase': saved['phase'] if saved else '等待前项完成'}


def allows_episode_update(item, before, after):
    """仅本批已核验完成的更新，才可解释合集条目的新首P CID或稿件标题。"""
    if item.get('status') != 'done' or item.get('publication_action') != 'update_existing' or not item.get('workflow_id'):
        return False
    if any(before.get(key) != after.get(key) for key in ('id', 'aid', 'bvid')):
        return False
    run = workflows.get(item['workflow_id'])
    task = workflows._update_task(run)
    if (not task or task['status'] != 'completed' or task['source_publication_id'] != item.get('publication_id')
            or task['bvid'] != before.get('bvid')):
        return False
    from sqlmodel import Session
    from dubforge.db import get_engine
    from dubforge.publishing.models import PublicationUpdate
    with Session(get_engine()) as session:
        row = session.get(PublicationUpdate, task['id'])
        snapshot, execution = json.loads(row.plan), json.loads(row.execution)
        if row.project_uid != run['project_uid'] or service._hash(snapshot) != row.fingerprint:
            return False
    if snapshot.get('aid') != before.get('aid') or execution.get('stages', {}).get('edit', {}).get('status') != 'completed':
        return False
    parts = execution.get('final_parts') or []
    expected = dict(title=snapshot.get('settings', {}).get('title'), cid=parts[0].get('cid') if parts else None)
    return all(before.get(key) == after.get(key) or expected.get(key) == after.get(key) and expected.get(key) is not None
               for key in ('cid', 'title'))
