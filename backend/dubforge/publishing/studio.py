"""B站创作管理：读接口有界；修改先预览、持久化再发送，失败不盲重发。"""
import copy
import hashlib
import json
import math
import re
import time

from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub, transport
from dubforge.publishing.models import Publication, PublishChange, now
from dubforge.configuration_models import ConfigurationScope

_inflight = set()
EDIT_FIELDS = {'title', 'description', 'tags', 'dynamic', 'declaration_id',
               'tid', 'human_type2', 'copyright', 'source', 'cover'}


def validate_edit_values(values, *, before=None, account=''):
    """编辑使用修改后的组合校验；封面只接受内部上传后的平台地址。"""
    from urllib.parse import urlsplit, urlunsplit
    if not isinstance(values, dict) or set(values) - EDIT_FIELDS:
        raise pub.PublishError('包含当前无法同步的稿件设置，请在能力说明中核对')
    result = pub.validate_values({k: v for k, v in values.items() if k != 'cover'})
    if 'cover' in values:
        value = values['cover']
        if not isinstance(value, str):
            raise pub.PublishError('封面地址无效')
        try:
            parsed = urlsplit('https:' + value if value.startswith('//') else value)
            host, port = parsed.hostname or '', parsed.port
        except ValueError:
            raise pub.PublishError('封面地址格式无效') from None
        if (len(value) > 2000 or parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or port
                or not any(host == suffix or host.endswith('.' + suffix)
                           for suffix in ('hdslb.com', 'biliimg.com', 'bilibili.com'))):
            raise pub.PublishError('封面只能使用已上传的B站图片')
        result['cover'] = urlunsplit(('https', parsed.netloc, parsed.path, parsed.query, ''))
    if ('title' in result and not result['title']) or ('tags' in result and not result['tags']):
        raise pub.PublishError('标题和标签不能为空')
    merged = {**(before or {}), **result}
    if {'copyright', 'source'} & result.keys() and merged.get('copyright') == 2 and not merged.get('source'):
        raise pub.PublishError('转载稿件必须填写来源')
    if {'copyright', 'declaration_id'} & result.keys() and merged.get('copyright') == 2 \
            and merged.get('declaration_id') not in {None, -1}:
        raise pub.PublishError('转载类型不能保留当前创作声明，请先核对版权类型和声明组合')
    if account and {'tid', 'human_type2', 'declaration_id'} & result.keys():
        metadata = pub.metadata(account)
        for field, group in (('tid', 'categories'), ('human_type2', 'human_categories')):
            if field in result and (not result[field] or result[field] not in pub._category_ids(metadata.get(group, []))):
                raise pub.PublishError('目标分区不可用，请重新选择项目发布分区')
        if 'declaration_id' in result and (result['declaration_id'] is None
                or result['declaration_id'] not in [d['value'] for d in metadata.get('declarations', [])]):
            raise pub.PublishError('创作声明已不可用，请选择有效选项；清空请到官网核对')
    return result


def normalize_part_titles(titles):
    """预览、冻结请求和worker使用同一份规范化名称。"""
    titles = {} if titles is None else titles
    if not isinstance(titles, dict):
        raise pub.PublishError('分P名称格式无效')
    result = {}
    for cid, title in titles.items():
        if (not isinstance(cid, str) or not re.fullmatch(r'[1-9]\d*', cid)
                or not isinstance(title, str) or not title.strip()
                or len(title.strip().encode('utf-16-le')) // 2 > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in title)):
            raise pub.PublishError('分P名称须为1至80个字符，且不能包含控制字符')
        result[cid] = title.strip()
    return result


def _read_preservation(detail):
    """只接受受控保留摘要；旧worker没有此字段时沿用旧计划契约。"""
    if 'preservation' not in detail:
        return None
    value = detail['preservation']
    if (not isinstance(value, dict) or set(value) != {'version', 'no_disturbance', 'parts'}
            or type(value.get('version')) is not int or value['version'] != 1
            or value.get('no_disturbance') is not None and (
                type(value['no_disturbance']) is not int or value['no_disturbance'] not in {0, 1})
            or not isinstance(value.get('parts'), list)
            or len(value['parts']) != len(detail.get('videos', []))
            or any(not isinstance(part, dict) or set(part) != {'cid', 'description_hash'}
                or type(part.get('cid')) is not int or part['cid'] <= 0
                or not isinstance(part.get('description_hash'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', part['description_hash']) for part in value['parts'])
            or [part['cid'] for part in value['parts']] != [part['cid'] for part in detail.get('videos', [])]):
        raise pub.PublishError('平台保留设置的核验资料不完整，未解除未知写入保护',
                               code='PUBLISH_RESPONSE_UNSUPPORTED')
    return copy.deepcopy(value)


def _expected_preservation(before, videos, replacements=()):
    original = _read_preservation(before)
    if original is None:
        return None
    descriptions = {part['cid']: part['description_hash'] for part in original['parts']}
    sources = {part['cid']: part['old_cid'] for part in replacements}
    empty = hashlib.sha256(b'').hexdigest()
    return dict(version=1, no_disturbance=original['no_disturbance'], parts=[dict(cid=part['cid'],
        description_hash=descriptions.get(sources.get(part['cid'], part['cid']), empty)) for part in videos])


def _preservation_matches(expected, remote):
    if expected is None:
        return True
    try:
        actual = _read_preservation(remote)
    except pub.PublishError:
        return False
    return (actual is not None and actual['parts'] == expected['parts']
            and (expected['no_disturbance'] is None or actual['no_disturbance'] == expected['no_disturbance']))


def part_layout(videos, replacements=(), *, titles=None, order=None):
    """完整清单只允许显式改名、排序、追加或替换，未选分P永远保留。"""
    titles = normalize_part_titles(titles)
    by_cid = {p['cid']: {k: p[k] for k in ('cid', 'filename', 'title')} for p in videos}
    if not isinstance(titles, dict) or set(titles) - {str(cid) for cid in by_cid}:
        raise pub.PublishError('分P名称只能修改本次已核对的原分P')
    checked_titles = {}
    for cid, title in titles.items():
        checked_titles[int(cid)] = _text(title, 80, '分P名称')
        if not checked_titles[int(cid)] or any(ord(c) < 32 or ord(c) == 127 for c in title):
            raise pub.PublishError('分P名称不能为空或包含控制字符')
    identities = list(by_cid)
    for part in replacements:
        old = part['old_cid']
        if old:
            if old not in by_cid:
                raise pub.PublishError('原分P已变化，请重新核对')
            by_cid[old] = {k: part[k] for k in ('cid', 'filename', 'title')}
        else:
            if part['cid'] in by_cid:
                raise pub.PublishError('追加分P的上传编号重复')
            identities.append(part['cid'])
            by_cid[part['cid']] = {k: part[k] for k in ('cid', 'filename', 'title')}
    for cid, title in checked_titles.items():
        by_cid[cid]['title'] = title
    if order is not None and (not isinstance(order, list) or any(type(i) is not int for i in order)
            or len(order) != len(identities) or set(order) != set(identities)):
        raise pub.PublishError('分P排序必须包含全部保留和新增分P各一次，不能漏选或删除')
    if not 1 <= len(identities) <= 100:
        raise pub.PublishError('更新后须保留1至100个分P')
    return [by_cid[cid] for cid in (order if order is not None else identities)]
_WRITE_STAGE_LABELS = {
    'preflight': '同步前检查',
    'ownership': '读取稿件归属',
    'version': '远端版本校验',
    'validate': '编辑参数检查',
    'request': '发送编辑请求前检查',
    'rules': '读取章节规则',
    'read': '读取现有章节',
    'verify': '写后回读核对',
}


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


def _chapter_target(body, *, require_aid=True):
    """章节 API 的身份键；aid/cid 必须显式绑定到稿件和分P。"""
    bvid = _bvid(body.get('bvid'))
    aid = body.get('aid')
    cid = body.get('cid')
    if require_aid:
        aid = _int(aid, 1, 2**53 - 1, '稿件编号')
    elif aid is not None:
        aid = _int(aid, 1, 2**53 - 1, '稿件编号')
    cid = _int(cid, 1, 2**53 - 1, '分P编号')
    return dict(bvid=bvid, **({'aid': aid} if aid is not None else {}), cid=cid)


def _chapter_rows(value, *, duration=None, rules=None):
    """把页面章节规范化成 worker 的 cards，并在发预览前拒绝明显错误。"""
    if not isinstance(value, list) or len(value) > 100:
        raise pub.PublishError('章节列表无效，不能超过100条', code='PUBLISH_CHAPTER_INVALID')
    limit = rules.get('limit') if isinstance(rules, dict) else None
    if type(limit) is int and limit >= 0 and len(value) > limit:
        raise pub.PublishError(f'章节数量超过B站当前限制（{limit}条）', code='PUBLISH_CHAPTER_LIMIT_EXCEEDED')
    rows = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) - {'start_time', 'end_time', 'title', 'from', 'to', 'content'}:
            raise pub.PublishError(f'第{index}条章节格式无效', code='PUBLISH_CHAPTER_INVALID')
        start = item.get('start_time', item.get('from'))
        end = item.get('end_time', item.get('to'))
        title = item.get('title', item.get('content'))
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) \
                or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end):
            raise pub.PublishError(f'第{index}条章节时间无效', code='PUBLISH_CHAPTER_INVALID')
        if start < 0 or end <= start or end > 86400:
            raise pub.PublishError(f'第{index}条章节时间范围无效', code='PUBLISH_CHAPTER_TIME')
        if duration is not None and end > duration + 1:
            raise pub.PublishError(f'第{index}条章节超出视频时长', code='PUBLISH_CHAPTER_TIME')
        if not isinstance(title, str) or not title.strip() \
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
            raise pub.PublishError(f'第{index}条章节标题无效（最多16个字符）', code='PUBLISH_CHAPTER_INVALID')
        from dubforge.publishing.chapters import title_length
        if title_length(title.strip()) > 16:
            raise pub.PublishError(f'第{index}条章节标题超过16个字符', code='PUBLISH_CHAPTER_TITLE_TOO_LONG')
        # 官方编辑器用 Math.round 转为整数秒，不能使用 Python 的银行家舍入。
        start_second, end_second = math.floor(float(start) + .5), math.floor(float(end) + .5)
        if end_second <= start_second:
            raise pub.PublishError(f'第{index}条章节取整后时长为零', code='PUBLISH_CHAPTER_TIME')
        rows.append(dict(start_time=start_second, end_time=end_second, title=title.strip()))
    rows.sort(key=lambda row: (row['start_time'], row['end_time'], row['title']))
    for index in range(1, len(rows)):
        if rows[index]['start_time'] < rows[index - 1]['end_time']:
            raise pub.PublishError(f'第{index + 1}条章节与上一条重叠', code='PUBLISH_CHAPTER_OVERLAP')
    if isinstance(rules, dict):
        stay = rules.get('stay')
        space = rules.get('space')
        if isinstance(stay, (int, float)) and stay >= 0:
            for row in rows:
                if (row['end_time'] - row['start_time'] < (1 if row['start_time'] == 0 else stay)):
                    raise pub.PublishError('章节持续时间未达到B站当前规则', code='PUBLISH_CHAPTER_INVALID')
        if isinstance(space, (int, float)) and space >= 0:
            for index in range(1, len(rows)):
                if rows[index]['start_time'] - rows[index - 1]['end_time'] < space:
                    raise pub.PublishError('章节间隔未达到B站当前规则', code='PUBLISH_CHAPTER_INTERVAL')
    return rows


def _chapter_cards(value):
    """只接收适配器已经规范化的卡片；结构变化仍返回中文领域错误。"""
    if not isinstance(value, list) or any(not isinstance(row, dict) or not {'from', 'to', 'content'} <= set(row) for row in value):
        raise pub.PublishError('B站章节响应结构不完整，请重新读取', code='PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED')
    return _chapter_rows([dict(start_time=row['from'], end_time=row['to'], title=row['content']) for row in value])


def _remote(account, action, body=None, *, cancel=None):
    return transport.call('studio_' + action, {**(body or {}), 'account_id': account},
                          credential=pub._credential(account), **({'cancel': cancel} if cancel is not None else {}))


def read(account, action, body, *, cancel=None):
    """显式只读操作，不把任意前端字段透传到 SDK。"""
    schemas = {'tags': {'title', 'description', 'tid', 'tags'},
               'archives': {'page', 'keyword', 'status'}, 'detail': {'bvid'},
               'templates': set(), 'topics': {'page', 'keyword', 'title', 'tid'},
               'comments': {'bvid', 'limit'}, 'subtitles': {'bvid', 'cid'},
               'chapters': {'bvid', 'cid'},
               'chapter_rules': {'bvid', 'aid', 'cid'}, 'chapter_read': {'bvid', 'aid', 'cid'},
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
    if action in {'detail', 'comments', 'subtitles', 'subtitle', 'chapters'}:
        p['bvid'] = _bvid(body.get('bvid'))
    if action in {'chapter_rules', 'chapter_read'}:
        p.update(_chapter_target(body))
    if action == 'comments':
        p['limit'] = _int(body.get('limit', 50), 1, 200, '评论数量')
    if action in {'subtitles', 'subtitle'}:
        p['cid'] = _int(body.get('cid', 0), 0 if action == 'subtitles' else 1, 2**53-1, '分P编号')
    if action == 'chapters':
        p['cid'] = _int(body.get('cid', 0), 0, 2**53-1, '分P编号')
    if action == 'subtitle':
        p['subtitle_id'] = _text(body.get('subtitle_id'), 50, '字幕编号')
        if not re.fullmatch(r'\d+', p['subtitle_id']):
            raise pub.PublishError('请选择有效字幕')
    data = _remote(account, action, p, **({'cancel': cancel} if cancel is not None else {}))
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
    from dubforge.publishing import updates
    updates.assert_available(session, account=account, bvid=bvid, excluding=updates.current_owner())
    rows = session.exec(select(PublishChange).where(PublishChange.status.in_(['committing', 'unknown']))).all()
    if any(r.id != excluding and (r.account_id == account and r.bvid == bvid
                                  or publication_id and r.publication_id == publication_id) for r in rows):
        raise pub.PublishError('此稿件有尚未确认的修改，请先核对结果', code='PUBLISH_WRITE_UNKNOWN')


def publication_available(session, key, *, allow_archived_check=False):
    from dubforge.publishing import lifecycle
    lifecycle.assert_active(pub._get(session, Publication, key), allow_archived_read=allow_archived_check)
    if session.exec(select(PublishChange).where(PublishChange.publication_id == key,
                    PublishChange.status.in_(['committing', 'unknown']))).first():
        raise pub.PublishError('此上传清单正在追加或结果待核对，不能再次投稿或编辑', code='PUBLISH_WRITE_UNKNOWN')


def _uploaded(session, key, account):
    row = pub._get(session, Publication, key)
    from dubforge.publishing import lifecycle
    lifecycle.assert_active(row)
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


def prepare_replacement(account, bvid, replacements, values=None, expected_version='', *, part_titles=None, order=None):
    """内部换片适配：上传与产物审查由更新任务完成，这里冻结完整远端分P清单。"""
    bvid = _bvid(bvid)
    if not isinstance(expected_version, str) or len(expected_version) > 128:
        raise pub.PublishError('远端版本凭证无效')
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= 100:
        raise pub.PublishError('请至少选择一个需要替换的分P')
    normalized = []
    for part in replacements:
        if not isinstance(part, dict) or set(part) != {'old_cid', 'cid', 'filename', 'title'}:
            raise pub.PublishError('替换分P的上传回执不完整')
        old_cid = _int(part['old_cid'], 0, 2**53 - 1, '原分P编号')
        cid = _int(part['cid'], 1, 2**53 - 1, '新分P编号')
        filename = _text(part['filename'], 300, '上传标识')
        title = _text(part['title'], 80, '分P标题')
        if not filename or not title or filename != part['filename'] \
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in part['filename'] + part['title']):
            raise pub.PublishError('替换分P的上传标识或标题无效')
        normalized.append(dict(old_cid=old_cid, cid=cid, filename=filename, title=title))
    existing_ids = [p['old_cid'] for p in normalized if p['old_cid']]
    if len(set(existing_ids)) != len(existing_ids) \
            or len({p['cid'] for p in normalized}) != len(normalized) \
            or len({p['filename'] for p in normalized}) != len(normalized):
        raise pub.PublishError('替换分P的目标或上传回执重复')
    with pub._lock, Session(get_engine()) as session:
        _ensure_free(session, account, bvid)
    before = read(account, 'detail', {'bvid': bvid})
    if before.get('mid') != account or before.get('bvid') != bvid or not before.get('version'):
        raise pub.PublishError('无法确认稿件归属，未生成换片计划')
    aid = _int(before.get('aid'), 1, 2**53 - 1, '稿件编号')
    if expected_version and before['version'] != expected_version:
        raise pub.PublishError('B站稿件已变化，请重新检查更新方案', code='PUBLISH_REMOTE_CHANGED')
    if before.get('edit_supported') is False:
        raise pub.PublishError(before.get('edit_limitation') or '特殊稿件请在官网修改', code='PUBLISH_EDIT_UNSUPPORTED')
    old_videos = before.get('videos')
    if not isinstance(old_videos, list) or not 1 <= len(old_videos) <= 100 \
            or any(not isinstance(p, dict) or type(p.get('cid')) is not int or not p['cid']
                   or not p.get('filename') or not isinstance(p.get('title'), str) for p in old_videos) \
            or len({p['cid'] for p in old_videos}) != len(old_videos):
        raise pub.PublishError('B站分P清单不完整，不能安全替换', code='PUBLISH_RESPONSE_UNSUPPORTED')
    old_cids, old_files = {p['cid'] for p in old_videos}, {p['filename'] for p in old_videos}
    if any((p['old_cid'] and p['old_cid'] not in old_cids) or p['cid'] in old_cids
           or p['filename'] in old_files for p in normalized):
        raise pub.PublishError('原分P不存在或新视频已属于此稿件，请重新检查', code='PUBLISH_REMOTE_CHANGED')
    values = {} if values is None else values
    values = validate_edit_values(values, before=before['settings'], account=account)
    part_titles = normalize_part_titles(part_titles)
    videos = part_layout(old_videos, normalized, titles=part_titles, order=order)
    after = {'settings': {**before['settings'], **values}, 'videos': videos}
    payload = dict(account_id=account, bvid=bvid, kind='replace', bound_aid=aid,
                   version=before['version'], values=values, videos=videos, replacements=normalized)
    if part_titles:
        payload['part_titles'] = part_titles
    if order is not None:
        payload['order'] = order
    preservation = _expected_preservation(before, videos, normalized)
    if preservation is not None:
        payload['expected_preservation'] = preservation
    # 上传文件标识只保存到内部冻结请求，公共差异只显示标题和分P编号。
    before = {**before, 'videos': [{k: v for k, v in p.items() if k != 'filename'} for p in old_videos]}
    after = {**after, 'videos': [{k: v for k, v in p.items() if k != 'filename'} for p in videos]}
    row = PublishChange(account_id=account, bvid=bvid, kind='replace', publication_id='',
                        payload=pub._json(payload), preview=pub._json({'before': before, 'after': after,
                            'warnings': ['替换视频将保留原BV，平台仍会重新转码和审核。']}))
    with pub._lock, Session(get_engine()) as session:
        _ensure_free(session, account, bvid)
        session.add(row); session.commit(); session.refresh(row)
        return _public(row)


def prepare(account, body, *, expected_version=''):
    if not isinstance(body, dict) or set(body) - {'bvid', 'kind', 'values', 'order', 'part_titles', 'publication_id'}:
        raise pub.PublishError('修改参数无效')
    bvid, kind = _bvid(body.get('bvid')), body.get('kind')
    if not isinstance(kind, str) or kind not in {'edit', 'append', 'collection', 'chapters'}:
        raise pub.PublishError('不支持的稿件修改动作')
    publication_id = _text(body.get('publication_id', ''), 64, '上传清单')
    if kind != 'append' and publication_id:
        raise pub.PublishError('只有追加分P可以选择上传清单')
    with pub._lock, Session(get_engine()) as session:
        _ensure_free(session, account, bvid, publication_id)
    before = read(account, 'detail', {'bvid': bvid})
    if expected_version and before.get('version') != expected_version:
        raise pub.PublishError('B站稿件在预检后发生变化，请重新核对', code='PUBLISH_REMOTE_CHANGED')
    if before.get('mid') != account or before.get('bvid') != bvid or not before.get('version'):
        raise pub.PublishError('无法确认稿件归属，未生成修改计划')
    if kind not in {'collection', 'chapters'} and before.get('edit_supported') is False:
        raise pub.PublishError(before.get('edit_limitation') or '特殊稿件请在官网修改')
    values = body.get('values', {})
    order = body.get('order')
    part_titles = normalize_part_titles(body.get('part_titles'))
    videos = []
    warnings = []
    if kind == 'chapters':
        if publication_id or order is not None or not isinstance(values, dict) \
                or set(values) != {'aid', 'cid', 'items', 'permanent'} or type(values['permanent']) is not bool:
            raise pub.PublishError('章节修改需要稿件编号、分P编号、章节列表和播放器展示开关', code='PUBLISH_CHAPTER_INVALID')
        aid = _int(values['aid'], 1, 2**53 - 1, '稿件编号')
        cid = _int(values['cid'], 1, 2**53 - 1, '分P编号')
        if before.get('aid') != aid or not any(part.get('cid') == cid for part in before.get('videos', [])):
            raise pub.PublishError('章节目标稿件或分P与B站回执不一致', code='PUBLISH_CHAPTER_INVALID')
        # 先在领域边界做无网络的形状校验；平台动态规则由 chapter_prepare 再确认。
        items = _chapter_rows(values['items'])
        prepared = _remote(account, 'chapter_prepare', dict(bvid=bvid, aid=aid, cid=cid,
                         items=items, permanent=values['permanent']))
        if not isinstance(prepared, dict) or prepared.get('bvid') != bvid \
                or prepared.get('aid') != aid or prepared.get('cid') != cid \
                or not isinstance(prepared.get('snapshot'), dict) \
                or not isinstance(prepared.get('rules'), dict) \
                or not isinstance(prepared.get('fingerprint'), str) \
                or prepared['snapshot'].get('target') != dict(bvid=bvid, aid=aid, cid=cid) \
                or prepared['snapshot'].get('fingerprint') != prepared['fingerprint']:
            raise pub.PublishError('B站章节预览响应不完整，未生成修改计划', code='PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED')
        current = _chapter_cards(prepared.get('current', []))
        proposed = _chapter_cards(prepared.get('cards', []))
        proposed = _chapter_rows(proposed, rules=prepared['rules'])
        # B站网页会把最后一章的结束时间规范为视频结尾；起点和标题必须完全一致，
        # 末章终点允许采用平台返回的规范值，避免把一次可验证的提交误判为响应结构错误。
        same_shape = (len(proposed) == len(items) and all(
            left['start_time'] == right['start_time'] and left['title'] == right['title']
            and (index == len(items) - 1 or left['end_time'] == right['end_time'])
            for index, (left, right) in enumerate(zip(proposed, items))))
        if not same_shape or prepared.get('permanent') is not values['permanent']:
            raise pub.PublishError('B站章节预览与本次编辑不一致，未生成修改计划', code='PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED')
        before = {'bvid': bvid, 'aid': aid, 'cid': cid, 'items': current,
                  'permanent': bool(prepared.get('current_permanent')), 'rules': prepared.get('rules', {})}
        after = {'bvid': bvid, 'aid': aid, 'cid': cid, 'items': proposed,
                 'permanent': bool(prepared.get('permanent')), 'rules': prepared.get('rules', {})}
        if before['items'] == after['items'] and before['permanent'] == after['permanent']:
            raise pub.PublishError('没有需要提交的章节修改', code='PUBLISH_CHAPTER_NOOP')
        payload = dict(account_id=account, bvid=bvid, kind=kind, aid=aid, cid=cid,
                       prepared=prepared.get('snapshot'), cards=prepared.get('cards', []),
                       permanent=bool(prepared.get('permanent')))
        preview = {'before': before, 'after': after, 'warnings': [], 'source': 'bilibili.chapter.preview'}
    elif kind == 'collection':
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
            raise pub.PublishError('包含不支持的稿件设置，请按当前可用的更新范围选择；可见范围等需到官网处理')
        values = validate_edit_values(values, before=before['settings'], account=account)
        if kind == 'append':
            if values or order is not None or part_titles:
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
        cids = [p['cid'] for p in before['videos']]
        if len(cids) + len(videos) > 100:
            raise pub.PublishError('本版本最多管理100个分P，请在官网处理更大的稿件')
        if order is not None and (not isinstance(order, list) or any(type(v) is not int for v in order)
                                 or sorted(order) != sorted(cids) or len(set(order)) != len(cids)):
            raise pub.PublishError('分P排序必须包含全部现有分P各一次，不能删除或替换')
        reordered = part_layout(before['videos'], titles=part_titles, order=order)
        after = {'settings': {**before['settings'], **values}, 'videos': [*reordered, *videos]}
        if not videos and after == {'settings': before['settings'], 'videos': before['videos']}:
            raise pub.PublishError('没有需要提交的修改')
    if kind != 'chapters':
        payload = dict(account_id=account, bvid=bvid, kind=kind, version=before['version'], values=values,
                       order=order, videos=videos)
        if part_titles:
            payload['part_titles'] = part_titles
        if kind == 'edit' and (part_titles or order is not None):
            payload['videos'] = copy.deepcopy(after['videos'])
        if kind == 'append':
            payload['publication_revision'] = revision
        if kind in {'edit', 'append'}:
            preservation = _expected_preservation(before, after['videos'])
            if preservation is not None:
                payload['expected_preservation'] = preservation
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
    change_kind = ''
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
        change_kind = row.kind
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
        result = _remote(account, 'chapter_write' if change_kind == 'chapters' else 'write', {**payload, 'confirmed': True})
        if result.get('bvid') != payload['bvid'] or result.get('accepted') is not True:
            raise pub.PublishError('修改响应不完整', code='PUBLISH_WRITE_UNKNOWN')
        if change_kind == 'chapters' and (result.get('aid') != payload['aid'] or result.get('cid') != payload['cid']
                                     or result.get('verified') is not True):
            raise pub.PublishError('章节回读结果尚未确认，请先核对B站', code='PUBLISH_CHAPTER_UNKNOWN')
        if change_kind == 'replace' and (result.get('aid') != payload['bound_aid'] or result.get('verified') is not True):
            raise pub.PublishError('视频替换结果尚未回读确认，请先核对B站', code='PUBLISH_WRITE_UNKNOWN')
        if payload.get('expected_preservation') is not None and result.get('verified') is not True:
            raise pub.PublishError('原稿保留设置尚未回读确认，请先核对B站', code='PUBLISH_WRITE_UNKNOWN')
        with pub._lock, Session(get_engine()) as session:
            row = pub._get(session, PublishChange, key)
            _finish(session, row, result); session.refresh(row)
            return _public(row)
    except Exception as exc:
        definite = not write_started or getattr(exc, 'code', '') in {
            'PUBLISH_WRITE_NOT_SENT', 'PUBLISH_WRITE_REJECTED', 'PUBLISH_REMOTE_CHANGED',
            'PUBLISH_EDIT_UNSUPPORTED', 'PUBLISH_REMOTE_OWNERSHIP', 'PUBLISH_RESPONSE_UNSUPPORTED',
            'PUBLISH_CHAPTER_INVALID', 'PUBLISH_CHAPTER_RULE_UNAVAILABLE', 'PUBLISH_CHAPTER_RULE_REJECTED',
            'PUBLISH_CHAPTER_LIMIT_EXCEEDED', 'PUBLISH_CHAPTER_TITLE_TOO_LONG', 'PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED',
            'PUBLISH_CHAPTER_REMOTE_CHANGED', 'PUBLISH_CHAPTER_NOT_SENT', 'PUBLISH_CHAPTER_REJECTED',
            'PUBLISH_CHAPTER_TIME', 'PUBLISH_CHAPTER_OVERLAP', 'PUBLISH_CHAPTER_INTERVAL',
        }
        message = exc.message if isinstance(exc, DubForgeError) else '修改结果尚未确认，请先核对B站'
        detail = getattr(exc, 'detail', None)
        stage = detail.get('stage') if isinstance(detail, dict) else None
        if stage in _WRITE_STAGE_LABELS:
            message = f'{message}（失败阶段：{_WRITE_STAGE_LABELS[stage]}）'
        with pub._lock, Session(get_engine()) as session:
            row = pub._get(session, PublishChange, key)
            row.status, row.error = ('failed' if definite else 'unknown'), message
            session.add(row); session.commit()
        chapter_error_codes = {
            'PUBLISH_CHAPTER_INVALID', 'PUBLISH_CHAPTER_RULE_UNAVAILABLE', 'PUBLISH_CHAPTER_RULE_REJECTED',
            'PUBLISH_CHAPTER_LIMIT_EXCEEDED', 'PUBLISH_CHAPTER_TITLE_TOO_LONG', 'PUBLISH_CHAPTER_RESPONSE_UNSUPPORTED',
            'PUBLISH_CHAPTER_REMOTE_CHANGED', 'PUBLISH_CHAPTER_NOT_SENT', 'PUBLISH_CHAPTER_REJECTED',
            'PUBLISH_CHAPTER_UNKNOWN', 'PUBLISH_CHAPTER_INTERVAL', 'PUBLISH_CHAPTER_TIME', 'PUBLISH_CHAPTER_OVERLAP',
        }
        if isinstance(exc, DubForgeError) and ((not write_started) or
                (change_kind == 'chapters' and getattr(exc, 'code', '') in chapter_error_codes)):
            raise
        code = ('PUBLISH_CHAPTER_FAILED' if definite else 'PUBLISH_CHAPTER_UNKNOWN') if change_kind == 'chapters' \
            else ('PUBLISH_WRITE_FAILED' if definite else 'PUBLISH_WRITE_UNKNOWN')
        raise pub.PublishError(message, code=code) from None
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
        bvid, preview, kind = row.bvid, json.loads(row.preview), row.kind
        payload = json.loads(row.payload)
    if kind == 'chapters':
        remote = read(account, 'chapter_read', {'bvid': bvid, 'aid': payload['aid'], 'cid': payload['cid']})
        expected = preview['after']
        applied = (remote.get('bvid') == bvid and remote.get('aid') == payload['aid']
                   and remote.get('cid') == payload['cid'] and _chapter_cards(remote.get('cards', [])) == expected['items']
                   and bool(remote.get('permanent')) == expected['permanent'])
    elif kind == 'collection':
        remote = read(account, 'detail', {'bvid': bvid})
        expected = preview['after']['collection']
        before = preview['before']
        bound = (remote.get('bvid') == bvid and remote.get('mid') == account
                 and type(remote.get('aid')) is int and remote['aid'] == before.get('aid'))
        if expected is None:
            # 缺少字段不能证明移出成功；必须是适配器明确读到不属于任何合集。
            applied = (bound and 'collection' in remote and remote['collection'] is None
                       and remote.get('in_season') is False)
        else:
            applied = False
            if bound and (remote.get('collection') or {}).get('id') == expected['id']:
                # 完整小节读取自身验证账号归属与section身份，不能仅凭合集ID推定小节。
                section = pub.read_collection_order(account, expected['id'], expected['section_id'])
                applied = (section['section']['id'] == expected['section_id']
                           and section['section']['season_id'] == expected['id']
                           and any(p['aid'] == remote['aid'] and (not p['bvid'] or p['bvid'] == bvid)
                                   for p in section['episodes']))
    else:
        remote = read(account, 'detail', {'bvid': bvid})
        expected = preview['after']
        applied = ('settings' in expected and all(remote['settings'].get(k) == v for k, v in expected['settings'].items())
                   and [(p['cid'], p['title']) for p in remote['videos']]
                   == [(p['cid'], p['title']) for p in expected['videos']])
        if kind == 'replace':
            applied = (applied and remote.get('bvid') == bvid and remote.get('mid') == account
                       and remote.get('aid') == payload['bound_aid']
                       and [(p.get('cid'), p.get('filename'), p.get('title')) for p in remote.get('videos', [])]
                       == [(p['cid'], p['filename'], p['title']) for p in payload['videos']])
        applied = applied and _preservation_matches(payload.get('expected_preservation'), remote)
    with pub._lock, Session(get_engine()) as session:
        row = pub._get(session, PublishChange, key)
        if row.status not in {'unknown', 'committing'} or key in _inflight:
            raise pub.PublishError('修改状态已变化，请刷新')
        if applied:
            _finish(session, row, {'bvid': bvid, 'aid': remote['aid'], 'accepted': True,
                    **(dict(cid=payload['cid'], verified=True, cards=remote.get('cards', []),
                            permanent=remote.get('permanent')) if kind == 'chapters'
                       else dict(verified=True) if kind in {'replace', 'collection'}
                            or payload.get('expected_preservation') is not None else {})})
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
