"""发布文案中的来源事实由程序附加，不依赖模型生成。"""
from datetime import date
import re
from urllib.parse import urlsplit


# 文案证据中的链接只接受可公开访问的 HTTP(S) 地址。该提取器不执行请求，
# 也不把带用户名/密码的 URL 送给模型或追加到简介。
_URL_RE = re.compile(r"(?i)(?<![\w@])(?:https?://|www\.)[^\s<>\"'`（）]+")
_SPONSOR_RE = re.compile(
    r"(?i)(赞助|赞助商|商务合作|捐赠|打赏|支持本频道|优惠码|折扣码|推广链接|affiliate|sponsor|"
    r"promo(?:tion)?\s*code|discount\s*code|patreon|ko[- ]?fi|buymeacoffee|paypal)"
)
_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)]}〉》」』】>（）"
_SOCIAL_HOSTS = {'twitter.com', 'x.com', 'instagram.com', 'facebook.com', 'tiktok.com',
                 'weibo.com', 'zhihu.com', 'twitch.tv', 'discord.com', 'linkedin.com', 'bsky.app'}
_RESOURCE_HOSTS = {'github.com', 'gitlab.com', 'bitbucket.org', 'notion.so', 'arxiv.org',
                    'huggingface.co', 'npmjs.com', 'pypi.org', 'readthedocs.io', 'docs.google.com',
                    'drive.google.com'}
_RESOURCE_WORDS = re.compile(r'(?i)(文档|源码|代码|项目|下载|资料|教程|官网|documentation|source|code|project|download|docs)')


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


def _safe_url(value):
    """规范化单个资料链接；无效 URL 返回空字符串。"""
    if not isinstance(value, str):
        return ''
    value = value.strip().rstrip(_TRAILING_PUNCTUATION)
    if value.lower().startswith('www.'):
        value = 'https://' + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ''
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        return ''
    # 避免将换行、尖括号等 Markdown 边界带进最终简介。
    if any(ch in value for ch in '\r\n<>"\'`'):
        return ''
    return value


def link_category(url, label='', *, sponsor=False):
    """按公开域名与行标签做保守分类；不访问远端，也不把分类当安全凭据。"""
    if sponsor:
        return 'sponsor'
    try:
        host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    except ValueError:
        host = ''
    if host in _SOCIAL_HOSTS or any(host.endswith('.' + base) for base in _SOCIAL_HOSTS):
        return 'social'
    if host in _RESOURCE_HOSTS or any(host.endswith('.' + base) for base in _RESOURCE_HOSTS):
        return 'resource'
    return 'resource' if _RESOURCE_WORDS.search(label or '') else 'unknown'


def extract_description_links(description, *, exclude_url='', include_sponsor=False, limit=30):
    """从原视频简介提取文档/GitHub 等链接，返回可审计的小记录。

    只做本地文本扫描，不请求链接。按所在行的上下文标记潜在赞助链接，
    默认排除这类链接；调用方可在用户明确勾选后传入 ``include_sponsor=True``。
    ``exclude_url`` 通常是原视频 URL，避免重复追加来源地址。
    """
    if not isinstance(description, str) or not description.strip():
        return []
    excluded = _safe_url(exclude_url)
    seen = set()
    result = []
    for line in description.splitlines():
        sponsor = bool(_SPONSOR_RE.search(line))
        for raw in _URL_RE.findall(line):
            url = _safe_url(raw)
            if not url or url == excluded or url in seen:
                continue
            seen.add(url)
            if sponsor and not include_sponsor:
                continue
            context = _URL_RE.sub('', line)
            context = re.sub(r'[：:]?\s*[（(][^（）()]*[）)]\s*$', '', context)
            context = context.strip(' \t-*•:：()[]{}（）')
            if not context:
                context = urlsplit(url).netloc
            result.append(dict(url=url, label=context[:100], sponsor=sponsor,
                               category=link_category(url, context, sponsor=sponsor)))
            if len(result) >= max(1, min(int(limit), 100)):
                return result
    return result


def filter_sponsor_text(description, *, include_sponsor=False):
    """按行过滤明显的赞助/推广宣传语，防止模型误把它写入简介。"""
    if include_sponsor or not isinstance(description, str):
        return description if isinstance(description, str) else ''
    return '\n'.join(line for line in description.splitlines() if not _SPONSOR_RE.search(line))


def sanitize_generated_description(description, links=(), *, include_sponsor=False):
    """对模型回传简介做程序级边界检查，不允许自定义提示词新增网址。"""
    allowed = {_safe_url(item.get('url')) for item in links or [] if isinstance(item, dict)}

    def keep(match):
        raw, safe = match.group(0), _safe_url(match.group(0))
        return raw if safe and safe in allowed else ''

    body = _URL_RE.sub(keep, description if isinstance(description, str) else '')
    return filter_sponsor_text(body, include_sponsor=include_sponsor)


def append_source_links(description, links, *, heading='相关链接'):
    """把已提取的链接追加到简介，保持幂等并遵守后续长度裁剪。"""
    body = description.strip() if isinstance(description, str) else ''
    existing = set(_safe_url(v) for v in _URL_RE.findall(body))
    rows = []
    for item in links or []:
        if not isinstance(item, dict):
            continue
        url = _safe_url(item.get('url'))
        if not url or url in existing:
            continue
        existing.add(url)
        label = item.get('label') if isinstance(item.get('label'), str) else ''
        rows.append(f'- {label}: {url}' if label else f'- {url}')
    if not rows:
        return body
    block = '\n\n' + heading + '：\n' + '\n'.join(rows)
    return body + block


def attribution(project, info, *, include_author=True, include_published_date=True,
                include_source_url=True):
    """返回可附加到简介的来源事实。

    这些字段来自项目登记或平台资料，不由模型生成。三个开关彼此独立，
    供文案预览界面让用户决定公开哪些归属信息；省略开关时保持历史行为。
    """
    source = info.get('source') or {}
    author = source.get('uploader') or source.get('channel') or ''
    author = clip(' '.join(author.split()), 150) if isinstance(author, str) else ''
    published = published_date(info)
    url = clip(source_url(project), 500)
    return '\n'.join(filter(None, [f'原作者：{author}' if include_author and author else '',
                                   f'原视频发布时间：{published}' if include_published_date and published else '',
                                   f'原视频：{url}' if include_source_url and url else '']))


def append_attribution(description, credit):
    """仅用于自动文案：预留署名长度；重复生成不叠加相同的来源行。"""
    # 模型偶尔仍会输出来源行；移除它生成的行，统一以真实资料为准。
    body = '\n'.join(line for line in description.splitlines()
                     if not re.match(r'^\s*(原作者|原视频发布时间|原视频)\s*[：:]', line)).strip()
    suffix = '\n\n' + credit if credit else ''
    body = clip(body, 2000 - len(suffix.encode('utf-16-le')) // 2).rstrip()
    return (body + suffix).strip()
