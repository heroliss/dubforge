"""B站创作管理：读接口有界；修改先预览、持久化再发送，失败不盲重发。"""
import json
import re
import time

from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub, transport
from dubforge.publishing.models import Publication, PublishChange, now
from dubforge.configuration_models import ConfigurationScope

_inflight = set()
EDIT_FIELDS = {'title', 'description', 'tags', 'dynamic', 'declaration_id'}


def _bvid(value):
    if not isinstance(value, str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', value):
        raise pub.PublishError('请填写完整BV号')
    return value


def _int(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise pub.PublishError(f'{name}超出允许范围')
    return value


def _text(value, length, name):
    if not isinstance(value, str) or len(value) > length:
        raise pub.PublishError(f'{name}格式无效或过长')
    return value.strip()


def _remote(account, action, body=None):
    return transport.call('studio_' + action, {**(body or {}), 'account_id': account},
                          credential=pub._credential(account))


def read(account, action, body):
    """显式只读操作，不把任意前端字段透传到 SDK。"""
    schemas = {'tags': {'title', 'description', 'tid', 'tags'},
               'archives': {'page', 'keyword', 'status'}, 'detail': {'bvid'},
               'templates': set(), 'topics': {'page', 'keyword', 'title', 'tid'},
               'comments': {'bvid', 'limit'}, 'subtitles': {'bvid', 'cid'},
               'subtitle': {'bvid', 'cid', 'subtitle_id'}}
    if action not in schemas or not isinstance(body, dict) or set(body) - schemas[action]:
        raise pub.PublishError('不支持的创作管理查询')
    p = {}
    if action in {'archives', 'topics'}:
        p.update(page=_int(body.get('page', 1), 1, 500, '页码'),
                 keyword=_text(body.get('keyword', ''), 100, '关键词'))
    if action == 'archives':
        status = body.get('status', '')
        if not isinstance(status, str) or status not in {'', 'is_pubing', 'pubed', 'not_pubed'}:
            raise pub.PublishError('稿件状态无效')
        p['status'] = status
    if action in {'tags', 'topics'}:
        p.update(title=_text(body.get('title', ''), 80, '标题'),
                 tid=_int(body.get('tid', 21), 1, 100000, '分区'))
    if action == 'tags':
        p.update(description=_text(body.get('description', ''), 2000, '简介'),
                 tags=pub.validate_values({'tags': body.get('tags', [])})['tags'])
    if action in {'detail', 'comments', 'subtitles', 'subtitle'}:
        p['bvid'] = _bvid(body.get('bvid'))
    if action == 'comments':
        p['limit'] = _int(body.get('limit', 50), 1, 200, '评论数量')
    if action in {'subtitles', 'subtitle'}:
        p['cid'] = _int(body.get('cid', 0), 0 if action == 'subtitles' else 1, 2**53-1, '分P编号')
    if action == 'subtitle':
        p['subtitle_id'] = _text(body.get('subtitle_id'), 50, '字幕编号')
        if not re.fullmatch(r'\d+', p['subtitle_id']):
            raise pub.PublishError('请选择有效字幕')
    data = _remote(account, action, p)
    if action in {'comments', 'subtitle'}:
        from dubforge.publishing.imports import stage
        return stage(account, action, data)
    return data


def _public(row):
    return dict(id=row.id, account_id=row.account_id, bvid=row.bvid, kind=row.kind,
                publication_id=row.publication_id, status=row.status, preview=json.loads(row.preview),
                result=json.loads(row.result), error=row.error, busy=row.id in _inflight,
                created_at=row.created_at.isoformat())


def changes(account):
    pub._credential(account)
    with Session(get_engine()) as session:
        return [_public(row) for row in session.exec(select(PublishChange)
                .where(PublishChange.account_id == account).order_by(PublishChange.created_at.desc()).limit(100)).all()]


def _ensure_free(session, account, bvid, publication_id='', excluding=''):
    rows = session.exec(select(PublishChange).where(PublishChange.status.in_(['committing', 'unknown']))).all()
    if any(r.id != excluding and (r.account_id == account and r.bvid == bvid
                                  or publication_id and r.publication_id == publication_id) for r in rows):
        raise pub.PublishError('此稿件有尚未确认的修改，请先核对结果', code='PUBLISH_WRITE_UNKNOWN')


def publication_available(session, key):
    if session.exec(select(PublishChange).where(PublishChange.publication_id == key,
                    PublishChange.status.in_(['committing', 'unknown']))).first():
        raise pub.PublishError('此上传清单正在追加或结果待核对，不能再次投稿或编辑', code='PUBLISH_WRITE_UNKNOWN')


def _uploaded(session, key, account):
    row = pub._get(session, Publication, key)
    snap = json.loads(row.snapshot)
    if row.status != 'uploaded' or snap.get('account_id') != account or json.loads(row.receipt).get('bvid'):
        raise pub.PublishError('追加分P请选择当前账号已上传、尚未投稿的清单')
    transfers = json.loads(row.transfers)
    videos = []
    for part in snap.get('parts', []):
        value = transfers.get(str(part['artifact_id']), {})
        if (type(value.get('cid')) is not int or not 0 < value['cid'] < 2**53
                or not isinstance(value.get('filename'), str) or not value['filename']):
            raise pub.PublishError('上传回执不完整，不能追加分P')
        videos.append(dict(cid=value['cid'], filename=value['filename'], title=part['title']))
    if not videos:
        raise pub.PublishError('上传清单为空')
    # 上传回执只证明文件已传送；新增规则或校对后，旧文件仍需重新核对。
    from dubforge.content_review_service import assert_snapshot
    assert_snapshot(snap)
    from dubforge.publishing.review_draft import assert_snapshot_current
    assert_snapshot_current(snap)
    return row, snap, videos


def prepare(account, body):
    if not isinstance(body, dict) or set(body) - {'bvid', 'kind', 'values', 'order', 'publication_id'}:
        raise pub.PublishError('修改参数无效')
    bvid, kind = _bvid(body.get('bvid')), body.get('kind')
    if not isinstance(kind, str) or kind not in {'edit', 'append', 'collection'}:
        raise pub.PublishError('不支持的稿件修改动作')
    publication_id = _text(body.get('publication_id', ''), 64, '上传清单')
    if kind != 'append' and publication_id:
        raise pub.PublishError('只有追加分P可以选择上传清单')
    with pub._lock, Session(get_engine()) as session:
        _ensure_free(session, account, bvid, publication_id)
    before = read(account, 'detail', {'bvid': bvid})
    if before.get('mid') != account or before.get('bvid') != bvid or not before.get('version'):
        raise pub.PublishError('无法确认稿件归属，未生成修改计划')
    if kind != 'collection' and before.get('edit_supported') is False:
        raise pub.PublishError(before.get('edit_limitation') or '特殊稿件请在官网修改')
    values = body.get('values', {})
    order = body.get('order')
    videos = []
    warnings = []
    if kind == 'collection':
        if not isinstance(values, dict) or set(values) != {'season_id', 'section_id'} or order is not None:
            raise pub.PublishError('请选择目标合集和小节，或明确移出合集')
        for k in values:
            if values[k] is not None:
                _int(values[k], 1, 2**53-1, '合集/小节')
        if bool(values['season_id']) != bool(values['section_id']):
            raise pub.PublishError('合集和小节必须同时选择')
        if values['season_id']:
            meta = pub.metadata(account)
            season = next((s for s in meta.get('seasons', []) if s['id'] == values['season_id']), None)
            if not season or not any(s['id'] == values['section_id'] for s in season['sections']):
                raise pub.PublishError('目标合集/小节不属于当前账号')
            target = {'id': season['id'], 'title': season['title'], 'section_id': values['section_id']}
        else:
            target = None
        after = {'collection': target}
    else:
        if not isinstance(values, dict) or set(values) - EDIT_FIELDS:
            raise pub.PublishError('本次仅支持文案、创作声明与分P顺序，不更改版权、分区或可见范围')
        values = pub.validate_values(values)
        if 'title' in values and not values['title'] or 'tags' in values and not values['tags']:
            raise pub.PublishError('标题和标签不能为空')
        if kind == 'append':
            if values or order is not None:
                raise pub.PublishError('追加分P不同时修改文案或顺序；请分别预览操作')
            with Session(get_engine()) as session:
                row, snap, videos = _uploaded(session, publication_id, account)
                revision = row.revision
            if any(p.get('has_dubbing') for p in snap['parts']):
                try:
                    meta = pub.metadata(account)
                except DubForgeError:
                    # 此读取仅用于提示，不改变原声明；失败不能变成新的投稿硬拦截。
                    meta = {}
                declaration = before['settings'].get('declaration_id')
                choice = next((d for d in meta.get('declarations', []) if d['value'] == declaration), {})
                if before['settings'].get('copyright') == 2 or not re.search('人工智能|AI|合成技术', choice.get('label', ''), re.I):
                    warnings.append('追加分P含配音版本，建议核对AI声明。此次保留目标稿件原声明，不自动修改；平台可能要求补标。可返回调整，也可确认继续追加。')
            if any(v['cid'] in [old['cid'] for old in before['videos']] for v in videos):
                raise pub.PublishError('这些分P已在目标稿件中，不能重复追加')
        if 'declaration_id' in values:
            if before['settings'].get('copyright') == 2 or values['declaration_id'] is None:
                raise pub.PublishError('当前稿件不能提交所选创作声明')
            if values['declaration_id'] not in [d['value'] for d in pub.metadata(account).get('declarations', [])]:
                raise pub.PublishError('创作声明已不可用')
        cids = [p['cid'] for p in before['videos']]
        if len(cids) + len(videos) > 100:
            raise pub.PublishError('本版本最多管理100个分P，请在官网处理更大的稿件')
        if order is not None and (not isinstance(order, list) or any(type(v) is not int for v in order)
                                 or sorted(order) != sorted(cids) or len(set(order)) != len(cids)):
            raise pub.PublishError('分P排序必须包含全部现有分P各一次，不能删除或替换')
        reordered = sorted(before['videos'], key=lambda p: order.index(p['cid'])) if order else before['videos']
        after = {'settings': {**before['settings'], **values}, 'videos': [*reordered, *videos]}
        if not videos and after == {'settings': before['settings'], 'videos': before['videos']}:
            raise pub.PublishError('没有需要提交的修改')
    payload = dict(account_id=account, bvid=bvid, kind=kind, version=before['version'], values=values,
                   order=order, videos=videos)
    if kind == 'append':
        payload['publication_revision'] = revision
    # 前端只能拿到可展示的差异，不暴露文件上传标识等内部参数。
    preview = {'before': before, 'after': after, 'warnings': warnings}
    for side in [preview['before'], preview['after']]:
        if 'videos' in side:
            side['videos'] = [{k: v for k, v in p.items() if k != 'filename'} for p in side['videos']]
    row = PublishChange(account_id=account, bvid=bvid, kind=kind, publication_id=publication_id,
                        payload=pub._json(payload), preview=pub._json(preview))
    with pub._lock, Session(get_engine()) as session:
        _ensure_free(session, account, bvid, publication_id)
        session.add(row); session.commit(); session.refresh(row)
        return _public(row)


def _finish(session, row, result):
    if row.publication_id:
        publication = pub._get(session, Publication, row.publication_id)
        snapshot = json.loads(publication.snapshot)
        # 追加只关联已有稿件，不让原上传草稿的合集/可见性设置误触发其他动作。
        snapshot['settings']['season_id'] = None
        snapshot['settings']['section_id'] = None
        snapshot['update_target'] = row.bvid
        publication.snapshot = pub._json(snapshot)
        publication.receipt = pub._json({**result, 'update_kind': 'append'})
        publication.status, publication.error, publication.updated_at = 'submitted', '', now()
        session.add(publication)
    row.status, row.result, row.error = 'done', pub._json(result), ''
    session.add(row); session.commit()


def commit(account, key, body):
    if body != {'confirmed': True} or body.get('confirmed') is not True:
        raise pub.PublishError('请先预览并明确确认修改')
    append_snapshot = None
    with pub._lock, pub._execution_gate(), Session(get_engine()) as session:
        row = pub._get(session, PublishChange, key)
        if row.account_id != account:
            raise pub.PublishError('修改计划不属于当前账号')
        if row.status == 'done':
            return _public(row)
        if row.status != 'prepared':
            raise pub.PublishError('修改正在执行或结果待核对，不能重复提交', code='PUBLISH_WRITE_UNKNOWN')
        if time.time() - row.created_at.replace(tzinfo=now().tzinfo).timestamp() > 600:
            raise pub.PublishError('修改预览已超过10分钟，请重新预览')
        _ensure_free(session, account, row.bvid, row.publication_id, key)
        payload = json.loads(row.payload)
        if row.publication_id:
            publication, append_snapshot, _ = _uploaded(session, row.publication_id, account)
            from dubforge import runtime_edit
            runtime_edit.assert_editable('project:' + publication.project_uid)
            if publication.revision != payload['publication_revision']:
                raise pub.PublishError('上传清单资料已变化，请重新预览')
        row.status = 'committing'; session.add(row); session.commit(); _inflight.add(key)
    write_started = False
    try:
        if append_snapshot is not None:
            # 状态落库后、真正发送前再读当前审查，避免预览或上传后的规则变化绕过门禁。
            from dubforge.content_review_service import assert_snapshot
            assert_snapshot(append_snapshot)
            from dubforge.publishing.review_draft import assert_snapshot_current
            assert_snapshot_current(append_snapshot)
        write_started = True
        result = _remote(account, 'write', {**payload, 'confirmed': True})
        if result.get('bvid') != payload['bvid'] or result.get('accepted') is not True:
            raise pub.PublishError('修改响应不完整', code='PUBLISH_WRITE_UNKNOWN')
        with pub._lock, Session(get_engine()) as session:
            row = pub._get(session, PublishChange, key)
            _finish(session, row, result); session.refresh(row)
            return _public(row)
    except Exception as exc:
        definite = not write_started or getattr(exc, 'code', '') in {'PUBLISH_WRITE_NOT_SENT', 'PUBLISH_WRITE_REJECTED', 'PUBLISH_REMOTE_CHANGED', 'PUBLISH_EDIT_UNSUPPORTED'}
        message = exc.message if isinstance(exc, DubForgeError) else '修改结果尚未确认，请先核对B站'
        with pub._lock, Session(get_engine()) as session:
            row = pub._get(session, PublishChange, key)
            row.status, row.error = ('failed' if definite else 'unknown'), message
            session.add(row); session.commit()
        if not write_started and isinstance(exc, DubForgeError):
            raise
        raise pub.PublishError(message, code='PUBLISH_WRITE_FAILED' if definite else 'PUBLISH_WRITE_UNKNOWN') from None
    finally:
        with pub._lock:
            _inflight.discard(key)


def reconcile(account, key, body):
    if not isinstance(body, dict) or set(body) - {'confirmed_not_applied'} or (body and body.get('confirmed_not_applied') is not True):
        raise pub.PublishError('核对参数无效')
    with pub._lock, Session(get_engine()) as session:
        row = pub._get(session, PublishChange, key)
        if row.account_id != account or row.id in _inflight or row.status not in {'unknown', 'committing'}:
            raise pub.PublishError('当前不能核对此修改，请等待或刷新')
        bvid, preview = row.bvid, json.loads(row.preview)
    remote = read(account, 'detail', {'bvid': bvid})
    expected = preview['after']
    # 小节归属不能仅由合集ID证明，合集操作需用户到官网确认，禁止自动认定成功。
    applied = ('settings' in expected and all(remote['settings'].get(k) == v for k, v in expected['settings'].items())
               and [(p['cid'], p['title']) for p in remote['videos']]
               == [(p['cid'], p['title']) for p in expected['videos']])
    with pub._lock, Session(get_engine()) as session:
        row = pub._get(session, PublishChange, key)
        if row.status not in {'unknown', 'committing'} or key in _inflight:
            raise pub.PublishError('修改状态已变化，请刷新')
        if applied:
            _finish(session, row, {'bvid': bvid, 'aid': remote['aid'], 'accepted': True})
        elif body.get('confirmed_not_applied') is True:
            # 只解除保护；新预览会再次读取远端，不能直接重发旧请求。
            row.status = 'failed'; row.error = '用户已在官网核对；重新修改需新预览'
            session.add(row); session.commit()
        session.refresh(row)
        return {**_public(row), 'remote': remote}


def import_template(account, body):
    if not isinstance(body, dict) or set(body) != {'id', 'fields', 'confirmed'} or body['confirmed'] is not True:
        raise pub.PublishError('请选择模板字段并确认导入')
    selected = body['fields']
    _int(body['id'], 1, 2**53-1, '模板编号')
    if (not isinstance(selected, list) or not selected or any(not isinstance(k, str) for k in selected)
            or set(selected) - {'title', 'description', 'tags'}):
        raise pub.PublishError('只导入明确选择的文案字段，分区、版权与官网默认设置保持不变')
    template = next((t for t in read(account, 'templates', {})['items'] if t['id'] == body['id']), None)
    if not template:
        raise pub.PublishError('官网模板已变化，请重新读取')
    values = pub.validate_values({k: template['values'][k] for k in selected})
    key = 'bili-' + pub._hash([account, body['id']])[:32]
    with pub._lock, Session(get_engine()) as session:
        if session.get(ConfigurationScope, 'series:' + key):
            raise pub.PublishError('此官网模板已导入，请在本地模板中编辑；不会覆盖现有设置')
        session.add(ConfigurationScope(key='series:' + key, name=('官网：' + template['name'])[:80],
            values=pub._json({'publish.account_id':account, **{'publish.'+k:v for k,v in values.items()}}), revision=1))
        session.commit()
    from dubforge.configuration import list_templates
    return dict(templates=list_templates())
