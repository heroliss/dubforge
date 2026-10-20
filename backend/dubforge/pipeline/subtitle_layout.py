"""不改字幕原稿的烧录排版：先缩字，再按显示宽度明确换行。"""
import math
import unicodedata


_OPEN = '（《「『【〈〔［｛'
_CLOSE = '，。！？；：、）》」』】〉〕］｝…'


def _clusters(text):
    """不拆组合重音、肤色、常见 ZWJ 表情和双字符国旗。"""
    result = []
    for char in text:
        attached = (unicodedata.category(char) in {'Mn', 'Mc', 'Me'} or char == '\u200d'
                    or '\U0001f3fb' <= char <= '\U0001f3ff')
        flag_pair = (result and len(result[-1]) == 1
                     and '\U0001f1e6' <= result[-1] <= '\U0001f1ff'
                     and '\U0001f1e6' <= char <= '\U0001f1ff')
        if result and (attached or result[-1].endswith('\u200d') or flag_pair):
            result[-1] += char
        else:
            result.append(char)
    return result


def _cluster_units(cluster):
    """使用偏保守的 em 宽度；混合英文、中文和宽字形不按字符数等宽处理。"""
    widths = []
    for char in cluster:
        if unicodedata.category(char) in {'Mn', 'Mc', 'Me', 'Cf'}:
            width = 0
        elif char == '\t':
            width = 2.8
        elif unicodedata.east_asian_width(char) in 'WF' or ord(char) >= 0x1f1e6:
            width = 1.05
        elif char.isspace():
            width = .4
        elif char in 'MWmw@%&':
            width = 1.05
        elif char in 'ilIjtfr.,:;!\'`|':
            width = .48
        elif char.isupper():
            width = .85
        else:
            width = .72
        widths.append(width)
    if '\u200d' in cluster or (cluster and ord(cluster[0]) >= 0x1f1e6):
        return max(widths, default=0)
    return sum(widths)


def text_units(text):
    """返回一行的估计 em 宽度；调用方为描边、底色和字体回退另留安全边距。"""
    return sum(_cluster_units(cluster) for cluster in _clusters(text))


def _tokens(text):
    # 英文单词作为整体；中日韩允许字间断行，标点优先粘在合法的一侧。
    tokens, word = [], ''
    for cluster in _clusters(text):
        if (cluster.isspace() or unicodedata.east_asian_width(cluster[0]) in 'WF'
                or ord(cluster[0]) >= 0x1f1e6):
            if word:
                tokens.append(word)
                word = ''
            tokens.append(cluster)
        else:
            word += cluster
    if word:
        tokens.append(word)
    merged = []
    for token in tokens:
        if merged and (token in _CLOSE or merged[-1] in _OPEN):
            merged[-1] += token
        else:
            merged.append(token)
    return merged


def _wrap_paragraph(text, units):
    """优先词边界；超长链接或无空格长词才逐字簇断行，不截断正文。"""
    lines, line, width, pending = [], '', 0.0, ''
    for token in _tokens(text):
        if token.isspace():
            pending += token
            continue
        token_width = text_units(token)
        gap = pending if line else ''
        if line and width + text_units(gap) + token_width > units + 1e-9:
            lines.append(line)
            line, width, gap = '', 0.0, ''
        if token_width <= units + 1e-9:
            line += gap + token
            width += text_units(gap) + token_width
        else:
            # 词本身超过整行；避免让西文长词或 URL 依赖渲染器的断词支持。
            for cluster in _clusters(token):
                cluster_width = _cluster_units(cluster)
                if line and width + cluster_width > units + 1e-9:
                    lines.append(line)
                    line, width = '', 0.0
                line += cluster
                width += cluster_width
        pending = ''
    if line or not lines:
        lines.append(line)
    return lines


def layout_text(text, base_size, min_size, available_width, auto_fit=True, *, shrink_width=None):
    """返回 ``(字号, 排版文字)``；自然换行保留，各语言由调用方分别排版。

    上限为用户字号，下限不会反过来放大旧项目的小字号。关闭自适应只关闭
    缩字，始终保留明确换行。软边界决定缩小目标，硬边界决定是否换行；
    到达字号下限后仍可利用两者之间的缓冲区。这里只生成渲染副本。
    """
    base_size = float(base_size)
    minimum = min(base_size, float(min_size))
    available_width = max(float(available_width), 1.0)
    shrink_width = available_width if shrink_width is None else max(1.0, min(float(shrink_width), available_width))
    paragraphs = str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    longest = max((text_units(part) for part in paragraphs), default=0)
    size = base_size
    if auto_fit and longest * base_size > shrink_width + 1e-9:
        fitting = shrink_width / longest
        # 向下量化避免浮点误差把刚好放下的一行再次推过边界。
        size = max(minimum, min(base_size, math.floor(fitting * 10) / 10))
    units = available_width / size
    wrapped = []
    for paragraph in paragraphs:
        # 原来能容纳的段落完整保留，避免把人工空格格式也重排。
        wrapped.extend([paragraph] if text_units(paragraph) <= units + 1e-9
                       else _wrap_paragraph(paragraph, units))
    return size, '\n'.join(wrapped)
