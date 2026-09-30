"""发布文案中的来源事实由程序附加，不依赖模型生成。"""
from datetime import date
import re
from urllib.parse import urlsplit


def published_date(info):
    """首播/正式发布日期优先；只有日期时不虚构时分或时区。"""
    source = info.get('source') or {}
    for key in ('release_date', 'upload_date'):
        value = source.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            continue
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            continue
    return ''


def clip(value, units):
    # 与网页 maxlength 的 UTF-16 计数一致，不能切坏 emoji。
    return value.encode('utf-16-le')[:max(0, units) * 2].decode('utf-16-le', errors='ignore')


def source_url(project):
    url = project.source_url if project.source_kind != 'local' else ''
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password:
            return url
    except ValueError:
        pass
    return ''


def attribution(project, info):
    source = info.get('source') or {}
    author = source.get('uploader') or source.get('channel') or ''
    author = clip(' '.join(author.split()), 150) if isinstance(author, str) else ''
    published = published_date(info)
    url = clip(source_url(project), 500)
    return '\n'.join(filter(None, [f'原作者：{author}' if author else '',
                                   f'原视频发布时间：{published}' if published else '',
                                   f'原视频：{url}' if url else '']))


def append_attribution(description, credit):
    """仅用于自动文案：预留署名长度；重复生成不叠加相同的来源行。"""
    # 模型偶尔仍会输出来源行；移除它生成的行，统一以真实资料为准。
    body = '\n'.join(line for line in description.splitlines()
                     if not re.match(r'^\s*(原作者|原视频发布时间|原视频)\s*[：:]', line)).strip()
    suffix = '\n\n' + credit if credit else ''
    body = clip(body, 2000 - len(suffix.encode('utf-16-le')) // 2).rstrip()
    return (body + suffix).strip()
