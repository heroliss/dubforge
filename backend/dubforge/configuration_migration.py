"""一次性搬运设置值；运行代码不再读旧配置表。"""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

VERSION = 2


def freeze_project_baselines(engine):
    """仅补旧项目缺失基线；保留当前有效值，不推断产物当年的制作设置。"""
    from sqlmodel import Session, select
    from dubforge import configuration, config
    from dubforge.configuration_models import ConfigurationScope
    from dubforge.domain.models import Project
    from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
    changed = []
    with configuration.gate, Session(engine) as session:
        settings = config.load_config()
        allowed = set(configuration.defaults(settings)) - RUNTIME_PARAM_PATHS
        for uid in session.exec(select(Project.uid)).all():
            row = session.get(ConfigurationScope, 'project:' + uid)
            if row is None:
                row = ConfigurationScope(key='project:' + uid, name='本项目')
            previous = configuration.read_baseline(row.baseline, settings=settings)
            if previous is not None:
                raw = json.loads(row.baseline)
                missing = allowed - set(raw['values'])
                if not missing:
                    continue
                current = configuration.capture_baseline(session, row.parent, settings=settings)
                data = dict(schema=1, values={**{k: current['values'][k] for k in missing}, **raw['values']},
                            origins={**{k: current['origins'][k] for k in missing}, **raw['origins']},
                            sources={**{k: current['sources'][k] for k in missing}, **raw['sources']},
                            configured=sorted(set(raw['configured']) | (set(current['configured']) & missing)))
            else:
                data = configuration.capture_baseline(session, row.parent, settings=settings)
            configuration._validate(json.loads(row.values), {**configuration.defaults(settings), **data['values']},
                                    historical=True, settings=settings)
            row.baseline = configuration._json(data)
            session.add(row)
            changed.append(uid)
        session.commit()
    return changed


def migrate(engine):
    from dubforge.configuration import defaults, _validate, ConfigurationError
    from dubforge.pipeline.assembly_versions import legacy_version
    allowed = set(defaults())
    # 原库只读备份包含所有配置值；事务成功后再移除旧表，不动媒体文件。
    with engine.connect() as probe:
        tables = {r[0] for r in probe.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        done = 'configurationmigration' in tables and probe.exec_driver_sql('SELECT id FROM configurationmigration WHERE id=?', (VERSION,)).first()
        if done:
            return
        has_legacy = bool(tables & {'workflowrecipe', 'assemblycatalog', 'publishtemplate'})
        if not has_legacy and 'project' in tables:
            has_legacy = any('step_params' in json.loads(r[0] or '{}') for r in probe.exec_driver_sql('SELECT params FROM project'))
        if 'publishaccount' in tables:
            has_legacy |= any(r[1] == 'defaults' for r in probe.exec_driver_sql('PRAGMA table_info(publishaccount)'))
    if has_legacy and engine.url.database and engine.url.database != ':memory:':
        database = Path(engine.url.database)
        backup = database.with_name(database.stem + '.before-unified-configuration.sqlite3')
        if not backup.exists():
            temporary = backup.with_suffix('.tmp')
            with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
            os.replace(temporary, backup)
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE IF NOT EXISTS configurationmigration (id INTEGER PRIMARY KEY)')
        if conn.exec_driver_sql('SELECT id FROM configurationmigration WHERE id=?', (VERSION,)).first():
            return
        tables = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        scopes = {}
        def scope(key, name=''):
            return scopes.setdefault(key, dict(name=name, parent='', values={}, archived=False))
        def rows(table):
            return conn.exec_driver_sql(f'SELECT * FROM {table}').mappings().all() if table in tables else []
        def decode(raw):
            return json.loads(raw or '{}')
        accounts = {r['id']: r for r in rows('publishaccount')}
        account_values = {k: {'publish.'+field:value for field,value in decode(r.get('defaults')).items()}
                          for k,r in accounts.items()}
        for row in rows('publishtemplate'):
            target = scope('global' if row['id'] == 'global' else 'series:' + row['id'], row['name'])
            target['values'].update({'publish.' + k: v for k, v in decode(row['values']).items()})
            if row['account_id']:
                target['values']['publish.account_id'] = row['account_id']
            target['archived'] = bool(row.get('archived', False))
        for row in rows('workflowrecipe'):
            old = decode(row['values'])
            target = scope('global' if row['key'] == 'global' else 'project:' + row['key'])
            if old.get('template_id') and row['key'] != 'global':
                target['parent'] = 'series:' + old['template_id']
            for source, path in [('mode','run.mode'), ('account_id','publish.account_id'), ('ai_copy','publish.ai_copy'),
                                 ('cover_source','publish.cover_source'), ('cover_id','publish.cover_id'),
                                 ('part_titles','publish.part_titles'), ('comments_mode','comments.mode'),
                                 ('comment_limit','comments.limit'), ('comment_count','comments.count'), ('comment_prompt','comments.prompt')]:
                if source in old: target['values'][path] = old[source]
            target['values'].update({'publish.' + k: v for k, v in old.get('overrides', {}).items()})
            target['values'].update({'comments.' + k: v for k, v in old.get('comment_settings', {}).items()})
            if old.get('version_ids') is not None:
                target['values']['publish.version_ids'] = old['version_ids']
            elif old.get('variants'):
                target['values']['assembly.versions'] = [legacy_version(v) for v in old['variants']]
                target['values']['publish.version_ids'] = ['legacy-' + v for v in old['variants']]
                target['values']['publish.part_titles'] = {'legacy-' + k: v for k, v in old.get('part_titles', {}).items()}
        for row in rows('assemblycatalog'):
            scope('global' if row['key'] == 'global' else 'project:' + row['key'])['values']['assembly.versions'] = json.loads(row['versions'])
        for row in rows('project'):
            target = scope('project:' + row['uid'])
            params = decode(row['params'])
            target['values'].update(params.pop('step_params', {}))
            # 内容保持在项目文件；迁移现有的人工评论样式到项目覆盖。
            from dubforge import config
            path = config.projects_dir() / (row.get('dir_name') or row['uid']) / 'comment_studio.json'
            if path.is_file():
                overlay = decode(path.read_text(encoding='utf-8')).get('overlay')
                if overlay:
                    target['values'].update({'comments.' + k: v for k, v in overlay.get('settings', {}).items()})
            conn.exec_driver_sql('UPDATE project SET params=? WHERE uid=?', (json.dumps(params, ensure_ascii=False), row['uid']))
        # 全局旧配方选择的模板只做一次值展开；新版全局不能反向继承系列。
        global_recipe = next((decode(r['values']) for r in rows('workflowrecipe') if r['key'] == 'global'), {})
        inherited = scopes.get('series:' + global_recipe.get('template_id', ''))
        if inherited:
            target = scope('global', '全局默认')
            target['values'] = {**inherited['values'], **target['values']}
        # 开发期间可能已有新版范围；以已经保存的新值为准，迁移只补齐旧值。
        existing = {r['key']:r for r in rows('configurationscope')}
        for key, row in existing.items():
            target = scope(key)
            target.update(name=row['name'], parent=row['parent'], archived=bool(row['archived']))
            target['values'].update(decode(row['values']))
        # 账号只管理凭据；旧账号默认保留为普通系列模板，不增加第四层继承。
        for key, doc in list(scopes.items()):
            if key.startswith('series:'):
                account = doc['values'].get('publish.account_id', '')
                doc['values'] = {**account_values.get(account, {}), **doc['values']}
        for key, doc in list(scopes.items()):
            if not key.startswith('project:'):
                continue
            parent = scopes.get(doc['parent'], {}).get('values', {})
            account = {**scopes.get('global', {}).get('values', {}), **parent, **doc['values']}.get('publish.account_id', '')
            legacy_defaults = account_values.get(account, {})
            if legacy_defaults:
                if not doc['parent']:
                    doc['parent'] = 'series:account-' + account
                else:
                    doc['values'] = {**{k:v for k,v in legacy_defaults.items() if k not in parent}, **doc['values']}
        for account, values in account_values.items():
            if values:
                scope('series:account-'+account, accounts[account]['name']+' · 发布设置')['values'] = {
                    **values, 'publish.account_id':account, **decode(existing.get('series:account-'+account, {}).get('values'))}
        # 旧流程与成片清单可能曾分别编辑：保留两边的值，并让分P引用有效身份。
        baseline = defaults()
        for key, doc in sorted(scopes.items(), key=lambda item: (item[0] != 'global', item[0].startswith('project:'))):
            effective = {**baseline, **scopes.get('global', {}).get('values', {}),
                         **scopes.get(doc['parent'], {}).get('values', {}), **doc['values']}
            ids = doc['values'].get('publish.version_ids')
            if ids is None:
                continue
            versions = list(effective['assembly.versions'])
            aliases = {}
            for wanted in ids:
                if any(v['id'] == wanted for v in versions):
                    continue
                if wanted.startswith('legacy-'):
                    candidate = legacy_version(wanted.removeprefix('legacy-'))
                    match = next((v for v in versions if not v.get('overrides') and all(v[k] == candidate[k]
                        for k in ('dubbing','burn_subtitles','include_comments'))), None)
                    if match:
                        aliases[wanted] = match['id']
                    else:
                        versions.append(candidate)
                else:
                    from dubforge.errors import DubForgeError
                    raise DubForgeError('旧配置引用了不存在的成片版本，迁移未执行；原配置已保留', code='CONFIGURATION_MIGRATION_INVALID')
            if versions != effective['assembly.versions']:
                doc['values']['assembly.versions'] = versions
            doc['values']['publish.version_ids'] = list(dict.fromkeys(aliases.get(v,v) for v in ids))
            titles = doc['values'].get('publish.part_titles', {})
            doc['values']['publish.part_titles'] = {aliases.get(k,k):v for k,v in titles.items()}
        for key, doc in scopes.items():
            values = {k: v for k, v in doc['values'].items() if k in allowed}
            if doc['parent'] and doc['parent'] not in scopes:
                raise ConfigurationError('旧配置引用的模板不存在，迁移未执行；原配置已保留', code='CONFIGURATION_MIGRATION_INVALID')
            base = {**baseline, **(scopes.get('global', {}).get('values', {}) if key != 'global' else {}),
                    **scopes.get(doc['parent'], {}).get('values', {})}
            _validate(values, base)
            revision = existing[key]['revision'] + 1 if key in existing else 1
            conn.exec_driver_sql('INSERT INTO configurationscope (key,name,parent,"values",baseline,revision,archived) VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name,parent=excluded.parent,"values"=excluded."values",revision=excluded.revision,archived=excluded.archived',
                                 (key, doc['name'], doc['parent'], json.dumps(values, ensure_ascii=False),
                                  existing.get(key, {}).get('baseline') or '{}', revision, doc['archived']))
        for row in rows('publication'):
            draft = decode(row['draft'])
            template = scopes.get('series:' + draft.get('template_id', ''))
            inherited = {k[8:]:v for k,v in account_values.get(draft.get('account_id'), {}).items()}
            if template:
                from dubforge.publishing.service import DEFAULTS
                inherited.update({k[8:]:v for k,v in template['values'].items() if k.startswith('publish.') and k[8:] in DEFAULTS})
            draft['overrides'] = {**inherited, **draft.get('overrides', {})}
            draft['template_id'] = ''
            conn.exec_driver_sql('UPDATE publication SET draft=? WHERE id=?', (json.dumps(draft, ensure_ascii=False), row['id']))
        # 只清理配置表中的旧值；不涉及媒体、运行历史或发布记录。
        for table in ('workflowrecipe', 'assemblycatalog', 'publishtemplate'):
            if table in tables: conn.exec_driver_sql(f'DROP TABLE {table}')
        if 'publishaccount' in tables and any(r[1] == 'defaults' for r in conn.exec_driver_sql('PRAGMA table_info(publishaccount)')):
            conn.exec_driver_sql('ALTER TABLE publishaccount DROP COLUMN defaults')
        conn.exec_driver_sql('INSERT INTO configurationmigration (id) VALUES (?)', (VERSION,))
    return True
