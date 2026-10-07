"""平台内容先存只读候选，再明确导入；不覆盖制作产物或评论工作区。"""
import json
import math
import re

from dubforge import config, video_info
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub
from dubforge.utils.fs import atomic_write_bytes


def stage(account, kind, data):
    raw = pub._json({'account_id': account, 'kind': kind, 'data': data}).encode()
    if len(raw) > 900000:
        raise pub.PublishError('平台资料过大，请缩小采集范围')
    key = pub._hash([account, kind, data])
    atomic_write_bytes(config.data_root() / 'publishing/imports' / f'{key}.json', raw)
    return {**data, 'import_key': key}


def subtitle_lines(data):
    lines = data.get('lines')
    if not isinstance(lines, list) or not 1 <= len(lines) <= 15000:
        raise pub.PublishError('字幕为空或超过15000句')
    previous = -1
    for line in lines:
        if (not isinstance(line, dict) or any(type(line.get(k)) not in (int, float)
            or not math.isfinite(line[k]) for k in ('start', 'end'))
            or not 0 <= line['start'] < line['end'] <= 36000 or line['start'] < previous
            or not isinstance(line.get('text'), str) or not line['text'].strip() or len(line['text']) > 3000):
            raise pub.PublishError('字幕内容或时间轴无效；原字幕未改变')
        previous = line['start']
    return lines


def apply(account, body):
    if set(body) != {'import_key', 'project_uid', 'confirmed'} or body['confirmed'] is not True:
        raise pub.PublishError('请确认来源与目标项目后导入')
    pub._credential(account)
    key = body['import_key']
    if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
        raise pub.PublishError('导入候选无效，请重新读取')
    path = config.data_root() / 'publishing/imports' / f'{key}.json'
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise pub.PublishError('导入候选已丢失，请重新读取') from None
    if doc.get('account_id') != account or pub._hash([account, doc.get('kind'), doc.get('data')]) != key:
        raise pub.PublishError('导入候选与账号不匹配')
    from dubforge import runtime_edit
    # 与启动步骤同锁顺序，避免正在读取导入字幕时切换来源。
    uid = body['project_uid']
    if not isinstance(uid, str) or not uid:
        raise pub.PublishError('请选择有效的目标项目')
    with runtime_edit.editing('project:' + uid), video_info.gate:
        project = pub._project(body['project_uid'])
        root, data = crud.project_dir_by_project(project), doc['data']
        if doc['kind'] == 'comments':
            destination = root / 'imports/bilibili_comments.json'
            old = video_info._read_json(destination) or {'items': []}
            rows = {r['id']: r for r in old['items']}
            for row in data['items']:
                rows[row['id']] = row
            if len(rows) > 2000:
                raise pub.PublishError('项目导入评论已超过2000条，请分项目管理')
            atomic_write_bytes(destination, pub._json({'items': list(rows.values())}).encode())
            return {'kind': 'comments', 'count': len(data['items']), 'total': len(rows), 'project_uid': project.uid}
        if doc['kind'] != 'subtitle':
            raise pub.PublishError('不支持的导入类型')
        lines = subtitle_lines(data)
        destination = root / 'imports/bilibili' / f'{key}.json'
        atomic_write_bytes(destination, pub._json(data).encode())
        atomic_write_bytes(root / 'imports/bilibili_subtitle.json', pub._json({'id': key}).encode())
        return {'kind': 'subtitle', 'count': len(lines), 'project_uid': project.uid,
                'message': '字幕来源已保存；请在语音转文字中选择“已导入的平台字幕”并运行，不会自动覆盖制作产物'}


def selected(root):
    try:
        index = json.loads((root / 'imports/bilibili_subtitle.json').read_text(encoding='utf-8'))
        if not re.fullmatch('[0-9a-f]{64}', index['id']):
            raise ValueError()
        data = json.loads((root / 'imports/bilibili' / f"{index['id']}.json").read_text(encoding='utf-8'))
        subtitle_lines(data)
        return data
    except (OSError, ValueError, KeyError, TypeError):
        raise DubForgeError('尚无有效的平台字幕来源，请先在发布中心的B站稿件工作区导入', code='SUBTITLE_IMPORT_MISSING') from None
