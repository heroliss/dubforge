"""系列模板的应用回收区；先保存完整快照，再移除数据库索引。"""
import datetime
import json
import logging
import re
import uuid

from sqlmodel import Session, select

from dubforge import config, configuration, runtime_edit
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes

logger = logging.getLogger(__name__)
FORMAT_VERSION = 1
MAX_BACKUP_BYTES = 2 * 1024 * 1024
SNAPSHOT_FIELDS = ('format_version', 'kind', 'backup_id', 'key', 'name',
                   'overrides', 'archived', 'revision', 'deleted_at')


class TemplateTrashError(DubForgeError):
    code = 'TEMPLATE_TRASH_INVALID'
    suggestion = '请刷新模板列表后重试；找回文件位于数据目录的 trash/templates 中。'


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _directory():
    root = config.data_root().resolve()
    folder = (root / 'trash' / 'templates').resolve()
    if not folder.is_relative_to(root):
        raise TemplateTrashError('模板回收区路径超出数据目录')
    return folder


def _backup_path(backup_id):
    if not isinstance(backup_id, str) or not re.fullmatch(r'[0-9a-f]{32}', backup_id):
        raise TemplateTrashError('模板备份标识无效')
    folder = _directory()
    path = folder / (backup_id + '.json')
    if path.resolve().parent != folder:
        raise TemplateTrashError('模板备份路径无效')
    return path


def _require_confirmation(body):
    if not isinstance(body, dict) or body.get('confirmed') is not True:
        raise TemplateTrashError('请确认后再删除或找回模板', code='TEMPLATE_CONFIRMATION_REQUIRED')


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        return datetime.datetime.fromisoformat(value).tzinfo is not None
    except (ValueError, OverflowError):
        return False


def _validate_backup(data, backup_id):
    if (not isinstance(data, dict) or type(data.get('format_version')) is not int
            or data['format_version'] != FORMAT_VERSION or data.get('kind') != 'series-template'
            or data.get('backup_id') != backup_id
            or not isinstance(data.get('key'), str) or not data['key'].startswith('series:')
            or not 7 < len(data['key']) <= 200
            or not isinstance(data.get('name'), str) or not data['name'].strip()
            or len(data['name']) > 80 or not isinstance(data.get('overrides'), dict)
            or data.get('archived') is not True or type(data.get('revision')) is not int
            or data['revision'] < 0 or not _timestamp(data.get('deleted_at'))):
        raise TemplateTrashError('模板备份格式无效，未执行找回')
    try:
        digest = configuration.digest({key: data[key] for key in SNAPSHOT_FIELDS})
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TemplateTrashError('模板备份包含无效设置，未执行找回') from exc
    if data.get('content_digest') != digest:
        raise TemplateTrashError('模板备份完整性检查失败，未执行找回')
    if 'restored_at' in data and (not _timestamp(data['restored_at'])
            or type(data.get('restored_revision')) is not int
            or data['restored_revision'] <= data['revision']):
        raise TemplateTrashError('模板备份的找回记录无效')
    return data


def _read_backup(backup_id):
    path = _backup_path(backup_id)
    try:
        if path.stat().st_size > MAX_BACKUP_BYTES:
            raise TemplateTrashError('模板备份文件过大，未执行找回')
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise TemplateTrashError('模板备份不存在', code='TEMPLATE_TRASH_NOT_FOUND') from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise TemplateTrashError('模板备份无法读取或已损坏，未执行找回') from exc
    return _validate_backup(data, backup_id)


def _write_backup(path, data):
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_BACKUP_BYTES:
        raise TemplateTrashError('模板设置过大，无法安全保存备份')
    atomic_write_bytes(path, payload)


def _summary(data, *, restorable=True, reason=''):
    return dict(backup_id=data['backup_id'], key=data['key'], id=data['key'][7:],
                name=data['name'], revision=data['revision'], archived=True,
                deleted_at=data['deleted_at'], restorable=restorable, reason=reason,
                backup_file=data['backup_id'] + '.json', backup_path=str(_backup_path(data['backup_id'])))


def _damaged_summary(path, error):
    """坏文件只提供定位信息，不能把文件名当作可恢复标识。"""
    backup_id = path.stem if re.fullmatch(r'[0-9a-f]{32}', path.stem) else None
    return dict(backup_id=backup_id, backup_file=path.name, backup_path=str(path),
                key='', id='', name='无法读取的模板备份', revision=0,
                archived=True, deleted_at='', restorable=False, error=error.message)


def delete(key, body):
    """绑定项目必须先显式处理，删除模板不会偷偷改变项目继承结果。"""
    _require_confirmation(body)
    if not isinstance(key, str) or not key.startswith('series:'):
        raise TemplateTrashError('只能删除已归档的系列模板')
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate, configuration.gate, Session(get_engine()) as session:
        row = configuration._row(session, key)
        if type(body.get('revision')) is not int or body['revision'] != row.revision:
            raise configuration.ConfigurationError('模板已变化，请刷新后重试', code='CONFIGURATION_CONFLICT')
        if not row.archived:
            raise TemplateTrashError('请先归档模板，再删除', code='TEMPLATE_NOT_ARCHIVED')
        runtime_edit.assert_editable(key)
        bindings = session.exec(select(ConfigurationScope).where(ConfigurationScope.parent == key)).all()
        if bindings:
            raise TemplateTrashError('模板仍被项目使用，请先解除绑定或删除相关项目（包括已归档项目）',
                code='TEMPLATE_IN_USE', detail={'project_count': len(bindings)})
        backup_id = uuid.uuid4().hex
        try:
            data = dict(format_version=FORMAT_VERSION, kind='series-template', backup_id=backup_id,
                        key=row.key, name=row.name, overrides=json.loads(row.values),
                        archived=True, revision=row.revision, deleted_at=_now())
            data['content_digest'] = configuration.digest(data)
            _validate_backup(data, backup_id)
            _write_backup(_backup_path(backup_id), data)
        except TemplateTrashError:
            raise
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            raise TemplateTrashError('模板备份保存失败，模板未删除', code='TEMPLATE_BACKUP_FAILED') from exc
        try:
            session.delete(row)
            session.commit()
        except Exception as exc:
            session.rollback()
            raise TemplateTrashError('模板删除失败，原模板和备份均已保留',
                code='TEMPLATE_DELETE_FAILED', detail={'backup_id': backup_id}) from exc
        return dict(deleted=True, recovery='application-trash', **_summary(data))


def list_deleted():
    """坏文件单独显示定位和错误；保留原文件，不阻挡其他备份找回。"""
    with configuration.gate, Session(get_engine()) as session:
        try:
            paths = list(_directory().glob('*.json'))
        except OSError as exc:
            raise TemplateTrashError('模板回收区暂时无法读取', code='TEMPLATE_TRASH_READ_FAILED') from exc
        result = []
        for path in paths:
            try:
                data = _read_backup(path.stem)
            except TemplateTrashError as exc:
                logger.warning('模板备份无法读取 %s：%s', path.name, exc.message)
                result.append(_damaged_summary(path, exc))
                continue
            if 'restored_at' in data:
                continue
            existing = session.get(ConfigurationScope, data['key'])
            bound = session.exec(select(ConfigurationScope.key).where(
                ConfigurationScope.parent == data['key'])).first()
            reason = ('相同标识的模板已存在，找回不会覆盖当前模板。' if existing is not None else
                      '仍有项目引用此模板标识，请先解除绑定再找回。' if bound is not None else '')
            result.append(_summary(data, restorable=existing is None and bound is None, reason=reason))
        return sorted(result, key=lambda item: (item['deleted_at'], item['backup_file']), reverse=True)


def restore(backup_id, body):
    """恢复相同模板标识和覆盖值，保留归档状态，不自动恢复任何项目绑定。"""
    _require_confirmation(body)
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate, configuration.gate, Session(get_engine()) as session:
        data = _read_backup(backup_id)
        if 'restored_at' in data:
            raise TemplateTrashError('此模板备份已经找回', code='TEMPLATE_TRASH_ALREADY_RESTORED')
        if session.get(ConfigurationScope, data['key']) is not None:
            raise configuration.ConfigurationError('相同标识的模板已存在，找回不会覆盖它', code='CONFIGURATION_CONFLICT')
        # 不接管遗留的悬空引用，避免恢复模板时悄悄重新绑定项目。
        bindings = session.exec(select(ConfigurationScope.key).where(ConfigurationScope.parent == data['key'])).all()
        if bindings:
            raise TemplateTrashError('仍有项目引用此模板标识，请先解除绑定再找回',
                code='TEMPLATE_IN_USE', detail={'project_count': len(bindings)})
        try:
            configuration._validate(data['overrides'], configuration._resolve(session, 'global')['values'])
        except DubForgeError as exc:
            raise TemplateTrashError('备份设置与当前全局设置不兼容，请核对后再找回', detail=exc.to_dict()) from exc
        row = ConfigurationScope(key=data['key'], name=data['name'], archived=True,
            values=configuration._json(data['overrides']), revision=data['revision'] + 1)
        try:
            session.add(row)
            session.commit()
        except Exception as exc:
            session.rollback()
            raise TemplateTrashError('模板找回失败，备份文件已保留', code='TEMPLATE_RESTORE_FAILED') from exc
        result = dict(restored=True, **_summary(data, restorable=False, reason='此模板已经找回。'))
        result['revision'] = row.revision
        # 数据库先成功，最后的文件标记失败也不能误报为未恢复。
        data.update(restored_at=_now(), restored_revision=row.revision)
        try:
            _write_backup(_backup_path(backup_id), data)
        except (OSError, TypeError, ValueError, UnicodeError, TemplateTrashError):
            logger.exception('模板已找回，但备份标记写入失败：%s', backup_id)
            result['warning'] = '模板已找回；备份文件仍保留在回收区，重复找回不会覆盖当前模板。'
        return result
