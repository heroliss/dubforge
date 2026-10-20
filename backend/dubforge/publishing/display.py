"""发布目标的只读名称快照；不参与投稿参数、远端调用或业务指纹。"""
import datetime
import hashlib
import json

from dubforge import config
from dubforge.utils.fs import atomic_write_bytes


def _title(value):
    return value.strip()[:200] if isinstance(value, str) else ''


def _identity(account_id, revision, credential):
    # 凭据只参与不可逆的本地命名；重新登录后不沿用上次授权的选项快照。
    return hashlib.sha256(json.dumps([account_id, revision, credential]).encode()).hexdigest()


def _path(account_id, revision, credential):
    return config.data_root() / 'publishing' / 'display' / (_identity(account_id, revision, credential) + '.json')


def _names(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    categories = {}
    def walk(rows, prefix='', depth=0):
        if not isinstance(rows, list) or depth > 10:
            return
        for row in rows[:2000]:
            if not isinstance(row, dict):
                continue
            name = _title(row.get('name') or row.get('title'))
            title = ' / '.join(part for part in (prefix, name) if part)
            if type(row.get('id')) is int and title:
                categories[str(row['id'])] = title
            walk(row.get('children'), title, depth + 1)
    walk(metadata.get('human_categories'))
    seasons = {}
    season_rows = metadata.get('seasons')
    for row in (season_rows if isinstance(season_rows, list) else [])[:3000]:
        if not isinstance(row, dict) or type(row.get('id')) is not int:
            continue
        section_rows = row.get('sections')
        seasons[str(row['id'])] = dict(title=_title(row.get('title')), sections={
            str(section['id']): _title(section.get('title'))
            for section in (section_rows if isinstance(section_rows, list) else [])[:1000]
            if isinstance(section, dict) and type(section.get('id')) is int})
    return dict(categories=categories, seasons=seasons)


def remember(account_id, revision, credential, metadata):
    """仅保存已经获取的安全名称；写盘失败不能阻止预检或刷新选项。"""
    value = dict(schema=1, account_id=account_id,
                 fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 **_names(metadata))
    try:
        atomic_write_bytes(_path(account_id, revision, credential),
                           json.dumps(value, ensure_ascii=False).encode())
    except OSError:
        pass
    return value


def resolve(account_id, revision, credential, settings, *, account_name='', metadata=None):
    """不主动联网；缓存名称明确附带来源和读取时间，不能充当实时有效性校验。"""
    source, value = 'none', {}
    if isinstance(metadata, dict):
        source, value = 'platform', dict(**_names(metadata), fetched_at=datetime.datetime.now(
            datetime.timezone.utc).isoformat())
    else:
        try:
            path = _path(account_id, revision, credential)
            if path.stat().st_size <= 4 * 1024 * 1024:
                cached = json.loads(path.read_text(encoding='utf-8'))
                if (cached.get('schema') == 1 and cached.get('account_id') == account_id
                        and isinstance(cached.get('categories'), dict) and isinstance(cached.get('seasons'), dict)):
                    source, value = 'cache', cached
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    categories = value.get('categories') or {}
    seasons = value.get('seasons') or {}
    season = seasons.get(str(settings.get('season_id'))) or {}
    season = season if isinstance(season, dict) else {}
    sections = season.get('sections') or {}
    sections = sections if isinstance(sections, dict) else {}
    return dict(account_id=account_id, account_name=account_name,
                human_type2=settings.get('human_type2'), season_id=settings.get('season_id'),
                section_id=settings.get('section_id'),
                category_title=_title(categories.get(str(settings.get('human_type2')), '')),
                season_title=_title(season.get('title', '')),
                section_title=_title(sections.get(str(settings.get('section_id')), '')),
                source=source, fetched_at=value.get('fetched_at', ''))
