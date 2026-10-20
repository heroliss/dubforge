"""评论专用字体测宽；字体可验证时匹配 ASS 字号，其余保留安全估算。"""
from collections import OrderedDict
from contextlib import contextmanager
from functools import lru_cache
import sys
import struct
from threading import RLock
import unicodedata

from dubforge.pipeline.subtitle_layout import _clusters, _tokens, text_units


# ASS 的字号按字体单元格高度解释；用较大的正高度测量，避免每个浮点字号
# 都创建字体，也减少小字号的整数取整误差。句子和语言样式之间共享有界数值缓存。
_REFERENCE_HEIGHT = 1024
_SAFETY = 1.025
_CACHE_LIMIT = 8192
_width_cache = OrderedDict()
_cache_lock = RLock()


def _simple_text(text):
    # GDI 的简单文本测量不能代替 HarfBuzz 的复杂文字塑形；连字、组合符、
    # emoji 与字体回退均使用原有保守估算，避免测得缺字方框的窄宽度。
    return all(unicodedata.category(char) not in {'Mn', 'Mc', 'Me', 'Cf'}
        and (char.isspace() or 0x20 <= ord(char) <= 0x024F
             or 0x2000 <= ord(char) <= 0x206F
             or 0x3000 <= ord(char) <= 0x303F or 0x3400 <= ord(char) <= 0x9FFF
             or 0xFF00 <= ord(char) <= 0xFFEF) for char in text)


def _utf16_length(text):
    """Windows 文本 API 接收 UTF-16 单元数，不能用 Python 码点数替代。"""
    return len(text.encode('utf-16-le')) // 2


@lru_cache(maxsize=1)
def _gdi():
    if sys.platform != 'win32':
        return None
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('gdi32', use_last_error=True)
    signatures = {
        'CreateCompatibleDC': ([w.HDC], w.HDC),
        'CreateFontW': ([ctypes.c_int] * 5 + [w.DWORD] * 8 + [w.LPCWSTR], w.HFONT),
        'SelectObject': ([w.HDC, w.HGDIOBJ], w.HGDIOBJ),
        'GetTextFaceW': ([w.HDC, ctypes.c_int, w.LPWSTR], ctypes.c_int),
        'GetFontData': ([w.HDC, w.DWORD, w.DWORD, w.LPVOID, w.DWORD], w.DWORD),
        'GetGlyphIndicesW': ([w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(w.WORD), w.DWORD], w.DWORD),
        'GetTextExtentPoint32W': ([w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(w.SIZE)], w.BOOL),
        'DeleteObject': ([w.HGDIOBJ], w.BOOL),
        'DeleteDC': ([w.HDC], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _font_matches(api, dc, requested):
    import ctypes
    actual = ctypes.create_unicode_buffer(128)
    if not api.GetTextFaceW(dc, len(actual), actual):
        return False
    if actual.value.casefold() == requested.casefold():
        return True
    # Windows 可能返回“微软雅黑”等本地化家族名；字体自身的 name 表记录
    # 英文/本地化别名。核对这些名称，不能简单把名称差异都当缺字体。
    table = int.from_bytes(b'name', 'little')
    length = api.GetFontData(dc, table, 0, None, 0)
    if length == 0xFFFFFFFF or not 6 <= length <= 1024 * 1024:
        return False
    data = ctypes.create_string_buffer(length)
    if api.GetFontData(dc, table, 0, data, length) != length:
        return False
    raw = data.raw
    _, count, offset = struct.unpack_from('>HHH', raw)
    for index in range(count):
        position = 6 + index * 12
        if position + 12 > len(raw):
            return False
        platform, encoding, _, name, size, start = struct.unpack_from('>HHHHHH', raw, position)
        if name not in {1, 4, 6, 16}:
            continue
        value = raw[offset + start:offset + start + size]
        try:
            alias = value.decode('utf-16-be' if platform in {0, 3} else 'mac_roman')
        except UnicodeError:
            continue
        if alias.casefold() == requested.casefold():
            return True
    return False


@contextmanager
def _font_context(api, font):
    """句子测量批次内共享 DC；不跨线程保存原生句柄，所有出口均释放。"""
    import ctypes
    dc = face = previous = None
    try:
        dc = api.CreateCompatibleDC(None)
        if not dc:
            yield None
            return
        face = api.CreateFontW(_REFERENCE_HEIGHT, 0, 0, 0, 400,
                              0, 0, 0, 1, 0, 0, 4, 0, font)
        if not face:
            yield None
            return
        previous = api.SelectObject(dc, face)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            yield None
            return
        # 不把系统替换字体的宽度冒充用户指定字体的度量。
        yield dc if _font_matches(api, dc, font) else None
    finally:
        if previous:
            api.SelectObject(dc, previous)
        if face:
            api.DeleteObject(face)
        if dc:
            api.DeleteDC(dc)


def _measure_token(api, dc, token):
    import ctypes
    from ctypes import wintypes as w
    count = _utf16_length(token)
    glyphs = (w.WORD * count)()
    found = api.GetGlyphIndicesW(dc, token, count, glyphs, 1)
    if found == 0xFFFFFFFF or any(glyph == 0xFFFF for glyph in glyphs):
        return None
    size = w.SIZE()
    if not api.GetTextExtentPoint32W(dc, token, count, ctypes.byref(size)):
        return None
    return size.cx / _REFERENCE_HEIGHT * _SAFETY


def _native_widths(font, tokens):
    if not font or sys.platform != 'win32':
        return None
    try:
        api = _gdi()
        if api is None:
            return None
        # 只在缓存缺失时创建一个字体/DC 并批量补齐；二分字号期间只读数值。
        # 锁还避免两个预览线程同时填充相同字词；数值缓存不持有线程所属 DC。
        with _cache_lock:
            missing = [token for token in tokens if (font, token) not in _width_cache]
            measured = {}
            if missing:
                with _font_context(api, font) as dc:
                    for token in missing:
                        value = _measure_token(api, dc, token) if dc is not None else None
                        measured[token] = value
                        # 极长链接不长期留在缓存中，仍可在本次句子排版中使用。
                        if len(token) <= 128:
                            _width_cache[font, token] = value
                            while len(_width_cache) > _CACHE_LIMIT:
                                _width_cache.popitem(last=False)
            values = {}
            for token in tokens:
                key = (font, token)
                if key in _width_cache:
                    value = _width_cache[key]
                    _width_cache.move_to_end(key)
                else:
                    value = measured.get(token)
                if value is None:
                    return None
                values[token] = value
            return values
    except (OSError, AttributeError, ValueError, OverflowError, struct.error):
        # 平台能力、字体不可用时只影响排版精确度，不使评论预览和成片失败。
        return None


class PreparedText:
    """分词与字体测量只做一次，换行和字号搜索复用相同宽度。"""

    def __init__(self, text, font=None):
        self.paragraphs = str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n')
        self.tokens = [_tokens(paragraph) for paragraph in self.paragraphs]
        unique = set(token for tokens in self.tokens for token in tokens)
        unique.update(cluster for paragraph in self.paragraphs for cluster in _clusters(paragraph))
        self.widths = (_native_widths(font, unique) if _simple_text(str(text)) else None)
        self.native = self.widths is not None

    def units(self, token):
        if self.widths is None:
            return text_units(token)
        if token in self.widths:
            return self.widths[token]
        # 连续空白由换行器临时合并；各字符宽度已经预先测好。
        return sum(self.widths[cluster] for cluster in _clusters(token))

    def wrap(self, size, available_width):
        limit = max(1, available_width) / size
        lines, widths = [], []
        for original, tokens in zip(self.paragraphs, self.tokens, strict=True):
            original_width = sum(self.units(token) for token in tokens)
            if original_width <= limit + 1e-9:
                lines.append(original)
                widths.append(original_width)
                continue
            line, width, pending = '', 0.0, ''
            for token in tokens:
                if token.isspace():
                    pending += token
                    continue
                token_width = self.units(token)
                gap = pending if line else ''
                gap_width = self.units(gap) if gap else 0
                if line and width + gap_width + token_width > limit + 1e-9:
                    lines.append(line)
                    widths.append(width)
                    line, width, gap, gap_width = '', 0.0, '', 0
                if token_width <= limit + 1e-9:
                    line += gap + token
                    width += gap_width + token_width
                else:
                    # 无空格长词和 URL 仅在必要时逐字簇断开，完整保留正文。
                    for cluster in _clusters(token):
                        cluster_width = self.units(cluster)
                        if line and width + cluster_width > limit + 1e-9:
                            lines.append(line)
                            widths.append(width)
                            line, width = '', 0.0
                        line += cluster
                        width += cluster_width
                pending = ''
            if line or not tokens:
                lines.append(line)
                widths.append(width)
        return lines, max(widths, default=0) * size
