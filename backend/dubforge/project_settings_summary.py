"""项目卡片的只读设置摘要；不扫描产物，不代替运行与模板应用预检。"""
import json

from sqlalchemy import case, func
from sqlmodel import Session, or_, select

from dubforge import configuration
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.publishing.models import Publication, PublishAccount
from dubforge.project_publish_target import history_projects, metadata, selection


def _object(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def project_summaries(projects, *, include_display_names=True):
    """一次批量读取来源和安全名称，卡片不为每项目重新解析完整设置。"""
    projects = list(projects)
    if not projects:
        return {}
    keys = ['project:' + project.uid for project in projects]
    with Session(get_engine()) as session:
        scopes = {row.key: row for row in session.exec(select(ConfigurationScope).where(or_(
            ConfigurationScope.key.in_([*keys, 'global']),
            ConfigurationScope.key.startswith('series:'),
        ))).all()}
        account_query = select(PublishAccount) if include_display_names else select(
            PublishAccount.id, PublishAccount.name, PublishAccount.revision)
        accounts = {row.id: row for row in session.exec(account_query).all()}
        histories = history_projects(session, [project.uid for project in projects])
        fallback = configuration.defaults()
        global_values = _object(scopes['global'].values) if 'global' in scopes else {}
        results, targets = {}, set()
        for project in projects:
            own = scopes.get('project:' + project.uid)
            template_id = own.parent[7:] if own and own.parent.startswith('series:') else ''
            template = scopes.get('series:' + template_id) if template_id else None
            patch = _object(own.values) if own else {}
            baseline = _object(own.baseline) if own else {}
            known = patch is not None and baseline is not None and (not own or not own.parent or own.parent.startswith('series:'))
            if baseline:
                base = baseline.get('values')
                known = known and baseline.get('schema') == 1 and isinstance(base, dict)
                values = {**fallback, **(base if isinstance(base, dict) else {}), **(patch or {})}
            else:
                parent = _object(template.values) if template else {}
                known = known and global_values is not None and parent is not None and (not template_id or template is not None)
                values = {**fallback, **(global_values or {}), **(parent or {}), **(patch or {})}
            known = known and values.get('run.mode') in ('produce', 'draft', 'upload', 'publish')
            selected = selection(patch or {}, has_history=project.uid in histories)
            if not known:
                selected.update(target=None, source='unknown', reason='项目设置待核对')
            target = selected['target'] or {}
            target = target if isinstance(target, dict) else {}
            publication_id = target.get('publication_id', '') if target.get('kind') == 'existing' else ''
            if isinstance(publication_id, str) and publication_id:
                targets.add(publication_id)
            else:
                publication_id = ''
            account_id = values.get('publish.account_id') or ''
            account = accounts.get(account_id) if isinstance(account_id, str) else None
            display = {}
            if include_display_names and known and account:
                from dubforge.publishing import display as names
                display = names.resolve(account.id, account.revision, account.credential,
                    dict(season_id=values.get('publish.season_id'), section_id=values.get('publish.section_id')),
                    account_name=account.name)
            results[project.uid] = dict(known=known, template_id=template_id,
                template_name=template.name if template else '', template_archived=bool(template and template.archived),
                override_count=len(patch or {}), delivery_mode=values.get('run.mode') if known else None,
                account_id=account_id if known and isinstance(account_id, str) else '',
                account_name=account.name if known and account else '',
                season_id=values.get('publish.season_id') if known else None,
                section_id=values.get('publish.section_id') if known else None,
                season_title=display.get('season_title', ''), section_title=display.get('section_title', ''),
                target_kind=target.get('kind', '') if known else '', target_selection=metadata(selected),
                publication_id=publication_id if known else '', bvid='')
            if not include_display_names:
                # 轻量列表只读数据库修订，不读取合集名称缓存或项目文件。
                results[project.uid]['source_revision'] = dict(
                    global_revision=scopes['global'].revision if 'global' in scopes else None,
                    project_revision=own.revision if own else None,
                    template_revision=template.revision if template else None,
                    template_parent=own.parent if own else '',
                    account_revision=account.revision if account else None)
        if targets:
            receipt_column = Publication.receipt if include_display_names else func.json_extract(
                case((func.json_valid(Publication.receipt) == 1, Publication.receipt), else_='{}'), '$.bvid').label('bvid')
            for row in session.exec(select(Publication.id, Publication.project_uid, receipt_column)
                                    .where(Publication.id.in_(targets))).all():
                result = results.get(row.project_uid)
                if result and result['publication_id'] == row.id:
                    result['bvid'] = (_object(row.receipt) or {}).get('bvid', '') if include_display_names else row.bvid or ''
    return results
