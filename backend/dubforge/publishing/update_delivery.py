"""原稿换片后的章节与合集交付；冻结新时间轴，未知写入只读核对。"""
import copy
import json
import math
import time

from sqlmodel import Session

from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.execution_progress import Recorder
from dubforge.publishing import chapters, service as pub, studio
from dubforge.publishing.models import PublishChange, now


def _check(cancel):
    if callable(cancel):
        cancel()
    elif cancel is not None and cancel.is_set():
        raise pub.PublishError('更新已暂停，已完成的视频和设置保留', code='PUBLISH_UPDATE_PAUSED')


def _save(execution, save):
    execution['updated_at'] = now().isoformat()
    save(execution)


def _stage(execution, save, name, status, label, *, error='', error_code=''):
    execution.setdefault('stages', {}).setdefault(name, {}).update(
        status=status, label=label, error=error, error_code=error_code)
    execution.update(phase=label, error=error, error_code=error_code)
    _save(execution, save)


def _finish(execution, save, outcome, label, *, error='', code='', next_check=0):
    execution.update(outcome=outcome, phase=label, error=error, error_code=code, next_check=next_check)
    _save(execution, save)
    return execution


def _id(value):
    if type(value) is int and 0 < value < 2**53:
        return value
    if isinstance(value, str) and value.isdigit() and 0 < int(value) < 2**53:
        return int(value)
    return None


def _change(key, account, bvid):
    if not key:
        return None
    with Session(get_engine()) as session:
        change = session.get(PublishChange, key)
        if change is None or change.account_id != account or change.bvid != bvid:
            raise pub.PublishError('更新设置的修改记录无法核验，请先核对原稿件',
                                   code='PUBLISH_UPDATE_CHANGE_INVALID')
        return change


def _binding(snapshot, execution, detail):
    """只用本次上传 CID 绑定；完整分P清单用于防止官网并发换片。"""
    if (not isinstance(detail, dict) or detail.get('mid') != snapshot['account_id']
            or detail.get('bvid') != snapshot['bvid'] or _id(detail.get('aid')) != _id(snapshot['aid'])):
        raise pub.PublishError('B站稿件归属或编号与本次更新不一致，未同步设置',
                               code='PUBLISH_UPDATE_BINDING_INVALID')
    remote = detail.get('videos')
    final = execution.get('final_parts')
    if not isinstance(remote, list) or not remote or not isinstance(final, list) or not final:
        raise pub.PublishError('换片后的完整分P回执尚未核验，未同步设置',
                               code='PUBLISH_UPDATE_BINDING_INVALID')
    actual = [_id(item.get('cid')) for item in remote if isinstance(item, dict)]
    expected = [_id(item.get('cid')) for item in final if isinstance(item, dict)]
    if (len(actual) != len(remote) or len(expected) != len(final) or None in actual or None in expected
            or len(set(actual)) != len(actual) or len(set(expected)) != len(expected) or actual != expected):
        raise pub.PublishError('B站分P清单与本次换片结果不一致，请重新核对，未同步设置',
                               code='PUBLISH_UPDATE_BINDING_INVALID')
    result = []
    for part in snapshot['parts']:
        upload = execution.get('transfers', {}).get(str(part['artifact_id']), {})
        cid = _id(upload.get('cid'))
        old_cid = _id(part.get('old_cid'))
        replaced = part.get('replaced') is True
        retained = (part.get('replaced') is False and cid == old_cid and isinstance(part.get('sha256'), str)
                    and bool(part['sha256']) and upload.get('sha256') == part['sha256'])
        if cid is None or cid not in actual or (cid == old_cid if replaced else not retained):
            raise pub.PublishError('无法把新成片可靠对应到换片后的分P，未套用旧CID或标题',
                                   code='PUBLISH_UPDATE_BINDING_INVALID')
        result.append({**copy.deepcopy(part), 'cid': cid})
    if len({item['cid'] for item in result}) != len(result):
        raise pub.PublishError('多个新成片对应同一分P，未同步设置', code='PUBLISH_UPDATE_BINDING_INVALID')
    return result


def _items(part, options):
    """仅消费本次新成片冻结的时间轴，不能读取旧稿件或当前项目补缺。"""
    items, timeline = part.get('chapter_items'), part.get('chapter_timeline')
    duration = part.get('duration')
    if (not isinstance(items, list) or not isinstance(timeline, dict)
            or timeline.get('kind') not in {'source', 'cut'} or isinstance(duration, bool)
            or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
        raise pub.PublishError('新成片缺少已核验的章节时间轴，请重新合成或核对后更新',
                               code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    cuts = part.get('output_cuts')
    if timeline['kind'] == 'cut':
        try:
            _, checked = chapters.map_items(cuts, [])
        except (ValueError, TypeError, KeyError) as exc:
            raise pub.PublishError('剪辑成片缺少有效的冻结剪辑记录，不能套用源章节时间',
                                   code='PUBLISH_CHAPTER_TIMELINE_INVALID') from exc
        # 实际封装时长可能比剪辑时间长数帧，遵循章节端点已有的一秒核验容差。
        if checked['kind'] != 'cut' or abs(checked['output_duration'] - duration) > 1:
            raise pub.PublishError('新成片时长与冻结剪辑时间轴不一致，未同步章节',
                                   code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    elif cuts is not None:
        raise pub.PublishError('新成片章节时间轴与剪辑记录不一致，未同步章节',
                               code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    if 'items' in options:
        try:
            expected, _ = chapters.map_items(cuts, options['items'])
        except (ValueError, TypeError, KeyError) as exc:
            raise pub.PublishError('冻结源章节无法映射到新成片，未同步章节',
                                   code='PUBLISH_CHAPTER_TIMELINE_INVALID') from exc
        if expected != items:
            raise pub.PublishError('新成片章节与冻结来源不一致，未同步章节',
                                   code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    result = copy.deepcopy(items)
    for index, item in enumerate(result):
        if not isinstance(item, dict):
            raise pub.PublishError('新成片章节格式无效', code='PUBLISH_CHAPTER_TIMELINE_INVALID')
        if item.get('end_time') is None:
            item['end_time'] = result[index + 1]['start_time'] if index + 1 < len(result) else duration
    # 章节领域统一校验时间、标题、末章，LLM仍只负责连续分组和表述。
    chapters._source_rows(result, duration)
    return result


def _adapt(snapshot, execution, node, target, options, save, cancel):
    _stage(execution, save, 'chapters', 'running', f'读取新分P章节规则：{target["title"]}')
    _check(cancel)
    rule_view = studio.read(snapshot['account_id'], 'chapter_rules',
                            dict(bvid=snapshot['bvid'], aid=snapshot['aid'], cid=target['cid']))
    if (not isinstance(rule_view, dict) or rule_view.get('bvid') != snapshot['bvid']
            or _id(rule_view.get('aid')) != _id(snapshot['aid']) or _id(rule_view.get('cid')) != target['cid']
            or not isinstance(rule_view.get('rules'), dict)):
        raise pub.PublishError('B站章节规则响应与新分P不一致，请稍后核对',
                               code='PUBLISH_CHAPTER_RULE_UNAVAILABLE')
    rules = rule_view['rules']
    limits = chapters.platform_constraints(rules)
    source = _items(target, options)
    title = snapshot.get('context_title') or snapshot.get('settings', {}).get('title') or ''
    key = pub._hash(dict(version=chapters.ADAPTATION_VERSION, items=source, duration=target['duration'],
                         translate=options['translate'], rules=limits, instruction=options['instruction'],
                         context_title=title))
    plans = execution['delivery'].setdefault('plans', {})
    result = plans.get(key)
    if result:
        try:
            result = chapters.validate_adaptation(result, source, duration=target['duration'], rules=rules,
                                                  instruction=options['instruction'], context_title=title)
        except pub.PublishError as exc:
            if exc.code != 'PUBLISH_CHAPTER_ADAPT_CACHE_INVALID':
                raise
            result = None
    def progress_saved(value):
        execution['delivery']['execution'] = value
        # 章节整理使用独立 Recorder，但外层更新任务也必须反映其进度。
        # 这样项目空间/总览无需读取内部 LLM trace 就能显示连续进度。
        stage = execution.setdefault('stages', {}).setdefault('chapters', {})
        if isinstance(value, dict):
            if isinstance(value.get('progress'), (int, float)) and math.isfinite(value['progress']):
                stage['progress'] = max(0.0, min(100.0, float(value['progress'])))
            if value.get('label'):
                stage['label'] = str(value['label'])[:160]
        _save(execution, save)
    progress = Recorder(progress_saved, previous=execution['delivery'].get('execution'),
                        owner='publication_update', run_id=execution.get('update_id', execution.get('id', '')))
    if not result:
        from dubforge import llm_conversations
        try:
            with llm_conversations.context(project_uid=snapshot.get('project_uid', ''), operation='chapters',
                                           label='更新原稿件：整理并审查新视频章节',
                                           owner_id=execution.get('update_id', execution.get('id', ''))):
                result = chapters.adapt(source, duration=target['duration'], rules=rules,
                                        translate=options['translate'], instruction=options['instruction'],
                                        context_title=title, cancel_check=lambda: _check(cancel), progress=progress)
            result = chapters.validate_adaptation(result, source, duration=target['duration'], rules=rules,
                                                  instruction=options['instruction'], context_title=title)
        except Exception as exc:
            paused = (getattr(exc, 'code', '') == 'PUBLISH_UPDATE_PAUSED'
                      or cancel is not None and not callable(cancel) and cancel.is_set())
            progress.finish('paused' if paused else 'failed',
                phase='chapters.adapt.paused' if paused else 'chapters.adapt.failed',
                label='章节整理已暂停' if paused else '章节整理未完成',
                detail=exc.message if isinstance(exc, DubForgeError) else '模型整理或结果校验未完成，请核对后重试')
            raise
        plans[key] = copy.deepcopy(result)
        progress.finish('completed', phase='chapters.adapt.completed', label='章节整理与标题审查已完成',
                        detail=f'{len(source)}条源章节已整理为{len(result["items"])}章；后续交付状态以章节同步结果为准')
    else:
        # 复用成功方案也要修正旧进程遗留的“接收模型输出中”，但不伪造新模型请求。
        progress.finish('completed', phase='chapters.adapt.cached', label='已复用核验通过的章节方案',
                        detail=f'复用{len(result["items"])}章整理结果，本次无需再次请求模型',
                        metrics=dict(cached=len(result['items'])))
    indices = list(range(len(source)))
    if 'items' in options:
        indexed = [{**item, '_source_index': index} for index, item in enumerate(options['items'])]
        mapped, _ = chapters.map_items(target.get('output_cuts'), indexed)
        indices = [item['_source_index'] for item in mapped]
    node.update(items=[{**copy.deepcopy(item), 'source_indices': [indices[index] for index in item['source_indices']]}
                       for item in result['items']], adaptation=copy.deepcopy(result['adaptation']), plan_key=key)
    for finding in node['adaptation']['review']['findings']:
        finding['source_indices'] = [indices[index] for index in finding['source_indices']]
    _save(execution, save)
    if any(item['action'] == 'needs_review' for item in result['adaptation']['review']['findings']):
        raise pub.PublishError('新视频章节标题仍需人工核对，已保留方案和审查说明，未修改B站',
                               code='PUBLISH_CHAPTER_REVIEW_REQUIRED')
    return studio._chapter_rows([{name: item[name] for name in ('title', 'start_time', 'end_time')}
                                 for item in result['items']], duration=target['duration'], rules=rules)


def _commit(snapshot, execution, node, kind, values, save, cancel):
    """发送前保存计划ID；中断后通过真实修改记录恢复而非盲重发。"""
    account, bvid = snapshot['account_id'], snapshot['bvid']
    change = _change(node.get('change_id'), account, bvid)
    if change and change.status == 'done':
        node.update(status='completed', error='', error_code='')
        _save(execution, save)
        return
    if node.get('status') in {'committing', 'unknown'} or change and change.status in {'committing', 'unknown'}:
        node.update(status='unknown', error='设置写入结果待核对，不能重复提交', error_code='PUBLISH_WRITE_UNKNOWN')
        _save(execution, save)
        return
    fresh = change and change.status == 'prepared' and time.time() - change.created_at.replace(tzinfo=now().tzinfo).timestamp() < 570
    if fresh:
        after = json.loads(change.preview).get('after') or {}
        collection = after.get('collection') or {}
        fresh = ((after.get('items') == values['items'] and after.get('permanent') is values['permanent'])
                 if kind == 'chapters' else collection.get('id') == values['season_id']
                 and collection.get('section_id') == values['section_id'])
    if not fresh:
        _check(cancel)
        prepared = studio.prepare(account, dict(bvid=bvid, kind=kind, values=values))
        node.update(change_id=prepared['id'], status='prepared')
        _save(execution, save)
        before = prepared['preview']['before']
    else:
        before = json.loads(change.preview)['before']
    if kind == 'chapters' and before.get('items') and snapshot['chapters'].get('overwrite_existing') is not True:
        raise pub.PublishError('B站新分P已有不同章节，请明确允许覆盖后重新确认更新',
                               code='PUBLISH_CHAPTER_EXISTING')
    _check(cancel)
    node.update(status='committing', error='', error_code='')
    _save(execution, save)
    try:
        result = studio.commit(account, node['change_id'], {'confirmed': True})
    except Exception as exc:
        persisted = _change(node['change_id'], account, bvid)
        unknown = persisted.status in {'committing', 'unknown'}
        message = exc.message if isinstance(exc, DubForgeError) else '设置修改未完成，请先核对结果'
        node.update(status='unknown' if unknown else 'failed', error=message,
                    error_code=getattr(exc, 'code', 'PUBLISH_WRITE_UNKNOWN' if unknown else 'PUBLISH_WRITE_FAILED'))
        _save(execution, save)
        return
    node.update(status='completed' if result.get('status') == 'done' else 'unknown',
                error='' if result.get('status') == 'done' else '设置写入结果尚未确认，请先核对',
                error_code='' if result.get('status') == 'done' else 'PUBLISH_WRITE_UNKNOWN')
    _save(execution, save)


def _collection_matches(snapshot, execution, cancel):
    """小节必须核对完整成员，只有合集ID不能证明小节归属。"""
    _check(cancel)
    account, bvid = snapshot['account_id'], snapshot['bvid']
    detail = studio.read(account, 'detail', dict(bvid=bvid))
    _binding(snapshot, execution, detail)
    target = snapshot['collection']
    if target.get('season_id') is None:
        return 'collection' in detail and detail['collection'] is None and detail.get('in_season') is False
    if (detail.get('collection') or {}).get('id') != target['season_id']:
        return False
    section = pub.read_collection_order(account, target['season_id'], target['section_id'],
                                        **({'cancel': cancel} if cancel is not None and not callable(cancel) else {}))
    return any(_id(row.get('aid')) == _id(snapshot['aid']) for row in section['episodes'])


def _verify_collection(snapshot, execution, node, save, cancel):
    try:
        matched = _collection_matches(snapshot, execution, cancel)
        node.update(status='completed' if matched else 'unknown',
                    error='' if matched else 'B站尚未核对到所选合集小节，保留提交回执，请稍后只读核对',
                    error_code='' if matched else 'PUBLISH_UPDATE_COLLECTION_UNKNOWN')
    except Exception as exc:
        node.update(status='unknown', error=exc.message if isinstance(exc, DubForgeError) else '合集写后核对未完成，请稍后重试核对',
                    error_code=getattr(exc, 'code', 'PUBLISH_UPDATE_COLLECTION_UNKNOWN'))
    _save(execution, save)


def _reconcile(snapshot, execution, save, cancel):
    """任何未知写入存在时，本轮仅核对，不发送其他尚未执行的设置。"""
    unresolved = False
    collection = execution['delivery']['collection']
    for node in [*execution['delivery']['parts'], collection]:
        if node['status'] in {'completed', 'skipped'}:
            continue
        change = _change(node.get('change_id'), snapshot['account_id'], snapshot['bvid'])
        if change and change.status == 'done':
            if node is collection:
                _verify_collection(snapshot, execution, node, save, cancel)
                unresolved |= node['status'] == 'unknown'
            else:
                node.update(status='completed', error='', error_code='')
        elif node.get('status') in {'unknown', 'committing'} or change and change.status in {'unknown', 'committing'}:
            if change and change.status in {'unknown', 'committing'}:
                try:
                    _check(cancel)
                    result = studio.reconcile(snapshot['account_id'], change.id, {})
                    if result.get('status') == 'done':
                        if node is collection:
                            _verify_collection(snapshot, execution, node, save, cancel)
                        else:
                            node.update(status='completed', error='', error_code='')
                    else:
                        node.update(status='unknown', error='B站尚未回读到本次设置，请继续核对，不能重复提交',
                                    error_code='PUBLISH_WRITE_UNKNOWN')
                except DubForgeError as exc:
                    node.update(status='unknown', error=exc.message, error_code=exc.code)
            else:
                node.update(status='unknown', error='修改记录尚未完成核对，请先检查B站结果',
                            error_code='PUBLISH_UPDATE_CHANGE_INVALID')
            unresolved |= node['status'] == 'unknown'
        _save(execution, save)
    return unresolved


def _aggregate(execution, save):
    delivery = execution['delivery']
    part_nodes = delivery['parts']
    completed = {'completed', 'skipped'}
    for key, nodes in [('chapters', part_nodes), ('collection', [delivery['collection']])]:
        status = ('unknown' if any(node['status'] in {'unknown', 'committing'} for node in nodes)
                  else 'failed' if any(node['status'] == 'failed' for node in nodes)
                  else 'skipped' if not nodes or all(node['status'] == 'skipped' for node in nodes)
                  else 'completed' if all(node['status'] in completed for node in nodes) else 'waiting')
        node = next((node for node in nodes if node['status'] in {'failed', 'unknown'}), {})
        label = (('章节' if key == 'chapters' else '合集') + ('已核验' if status == 'completed' else '待处理')
                 if status != 'skipped' else '本次不修改章节' if key == 'chapters' else '保持B站现有合集归属')
        _stage(execution, save, key, status,
               label,
               error=node.get('error', ''), error_code=node.get('error_code', ''))
    nodes = [*part_nodes, delivery['collection']]
    if any(node['status'] in {'unknown', 'committing'} for node in nodes):
        node = next(node for node in nodes if node['status'] in {'unknown', 'committing'})
        return _finish(execution, save, 'unknown', '视频已更新，部分设置结果待核对',
                       error=node.get('error', ''), code=node.get('error_code', 'PUBLISH_WRITE_UNKNOWN'))
    if any(node['status'] == 'failed' for node in nodes):
        node = next(node for node in nodes if node['status'] == 'failed')
        return _finish(execution, save, 'failed', '视频已更新，部分设置待处理',
                       error=node.get('error', ''), code=node.get('error_code', 'PUBLISH_UPDATE_DELIVERY_FAILED'))
    if all(node['status'] in completed for node in nodes):
        _stage(execution, save, 'verify', 'completed', '原BV与本次所选更新已核验')
        return _finish(execution, save, 'completed', '原稿件更新完成，已保留原BV')
    return _finish(execution, save, 'waiting', '设置核对已完成，等待继续未完成部分', next_check=time.time() + 1)


def run(snapshot, execution, *, save, cancel=None, read_only=False):
    """执行一次可恢复的后续交付；等待审核只登记下次时间，不占用线程睡眠。"""
    try:
        options = snapshot.get('chapters') or {}
        options = dict(enabled=options.get('enabled') is True, translate=options.get('translate', True),
                       instruction=options.get('instruction', ''), permanent=options.get('permanent', True),
                       overwrite_existing=options.get('overwrite_existing') is True,
                       **({'items': options['items']} if 'items' in options else {}))
        collection = snapshot.get('collection') or {}
        if (not isinstance(snapshot.get('parts'), list)
                or type(options['translate']) is not bool or type(options['permanent']) is not bool
                or not isinstance(options['instruction'], str)):
            raise pub.PublishError('更新原稿件的冻结设置无效', code='PUBLISH_UPDATE_SNAPSHOT_INVALID')
        identity = pub._hash(dict(account=snapshot['account_id'], bvid=snapshot['bvid'], aid=snapshot['aid'],
                                  parts=snapshot['parts'], chapters=options, collection=collection))
        delivery = execution.setdefault('delivery', {})
        if delivery.get('fingerprint') and delivery['fingerprint'] != identity:
            raise pub.PublishError('本次更新的冻结文件或设置已变化，请重新确认，未复用旧交付结果',
                                   code='PUBLISH_UPDATE_SNAPSHOT_CHANGED')
        if not delivery:
            delivery.update(fingerprint=identity, plans={}, parts=[dict(artifact_id=part['artifact_id'],
                title=part['title'], status='pending', change_id='', error='', error_code='')
                for part in snapshot['parts']], collection=dict(status='pending', change_id='', error='', error_code=''))
            _save(execution, save)
        _check(cancel)
        _stage(execution, save, 'review', 'running', '核对换片后的原稿件与审核状态')
        detail = studio.read(snapshot['account_id'], 'detail', {'bvid': snapshot['bvid']},
                             **({'cancel': cancel} if cancel is not None and not callable(cancel) else {}))
        targets = _binding(snapshot, execution, detail)
        remote_state = detail.get('state', detail.get('remote_state'))
        execution['delivery']['review'] = dict(state=remote_state, state_desc=detail.get('state_desc', ''),
                                               reject_reason=detail.get('reject_reason', ''))
        needs_ready_video = (any(part.get('replaced') for part in snapshot['parts'])
                             or options['enabled'] and bool(targets) or collection.get('enabled') is True)
        if not needs_ready_video:
            _stage(execution, save, 'review', 'skipped', '资料已回读，本次不等待视频审核')
        elif remote_state in {-2, -16}:
            message = detail.get('reject_reason') or detail.get('state_desc') or 'B站审核退回，请修改视频或资料后重新更新'
            _stage(execution, save, 'review', 'failed', '新视频审核退回', error=message,
                   error_code='PUBLISH_UPDATE_REJECTED')
            return _finish(execution, save, 'failed', '新视频审核退回，尚未同步后续设置',
                           error=message, code='PUBLISH_UPDATE_REJECTED')
        elif type(remote_state) is not int or remote_state != 0:
            _stage(execution, save, 'review', 'waiting', '视频已提交更新，等待B站转码和审核')
            return _finish(execution, save, 'waiting', '等待新视频审核通过后同步章节和合集', next_check=time.time() + 30)
        else:
            _stage(execution, save, 'review', 'completed', '视频审核已通过')
        for node, target in zip(delivery['parts'], targets):
            if node.get('cid') and node['cid'] != target['cid']:
                raise pub.PublishError('已保存的章节目标与本次新视频不一致，未复用旧设置',
                                       code='PUBLISH_UPDATE_BINDING_INVALID')
            node['cid'] = target['cid']
        _save(execution, save)
        nodes = [*delivery['parts'], delivery['collection']]
        unresolved = any(node['status'] in {'unknown', 'committing'} for node in nodes)
        for node in nodes:
            change = _change(node.get('change_id'), snapshot['account_id'], snapshot['bvid'])
            if change and change.status in {'unknown', 'committing'}:
                unresolved = True
        if unresolved or read_only:
            _reconcile(snapshot, execution, save, cancel)
            result = _aggregate(execution, save)
            if read_only and result['outcome'] == 'waiting':
                return _finish(execution, save, 'waiting', '核对已完成，可继续剩余步骤', next_check=0)
            return result
        for node, target in zip(delivery['parts'], targets):
            if node['status'] in {'completed', 'skipped'}:
                continue
            try:
                _check(cancel)
                if not options['enabled'] or not _items(target, options):
                    node.update(status='skipped', error='', error_code='',
                                label='保持B站现有章节' if not options['enabled'] else '新成片未保留源章节')
                    _save(execution, save)
                    continue
                items = _adapt(snapshot, execution, node, target, options, save, cancel)
                _stage(execution, save, 'chapters', 'running', f'同步新分P章节：{target["title"]}')
                _commit(snapshot, execution, node, 'chapters', dict(aid=snapshot['aid'], cid=target['cid'],
                        items=items, permanent=options['permanent']), save, cancel)
            except Exception as exc:
                if getattr(exc, 'code', '') == 'PUBLISH_CHAPTER_NOOP':
                    node.update(status='completed', error='', error_code='')
                else:
                    node.update(status='failed', error=exc.message if isinstance(exc, DubForgeError) else '新视频章节处理失败，请核对后重试',
                                error_code=getattr(exc, 'code', 'PUBLISH_UPDATE_DELIVERY_FAILED'))
                _save(execution, save)
            if node['status'] == 'unknown':
                return _aggregate(execution, save)
            if node.get('error_code') == 'PUBLISH_UPDATE_PAUSED':
                return _finish(execution, save, 'failed', '更新后续设置已暂停', error=node['error'], code=node['error_code'])
        node = delivery['collection']
        if node['status'] not in {'completed', 'skipped'}:
            if collection.get('enabled') is not True:
                node.update(status='skipped', label='保持B站现有合集归属', error='', error_code='')
                _save(execution, save)
            else:
                try:
                    _check(cancel)
                    _stage(execution, save, 'collection', 'running', '同步所选合集和小节')
                    if _collection_matches(snapshot, execution, cancel):
                        node.update(status='completed', label='合集小节已一致', error='', error_code='')
                        _save(execution, save)
                    else:
                        _commit(snapshot, execution, node, 'collection', dict(season_id=collection.get('season_id'),
                                section_id=collection.get('section_id')), save, cancel)
                        if node['status'] == 'completed':
                            _verify_collection(snapshot, execution, node, save, cancel)
                except Exception as exc:
                    node.update(status='failed', error=exc.message if isinstance(exc, DubForgeError) else '合集设置未完成，请核对后重试',
                                error_code=getattr(exc, 'code', 'PUBLISH_UPDATE_DELIVERY_FAILED'))
                    _save(execution, save)
        return _aggregate(execution, save)
    except Exception as exc:
        message = exc.message if isinstance(exc, DubForgeError) else '更新后的设置交付未完成，请核对后重试'
        return _finish(execution, save, 'failed', '视频更新后续检查未完成', error=message,
                       code=getattr(exc, 'code', 'PUBLISH_UPDATE_DELIVERY_FAILED'))
