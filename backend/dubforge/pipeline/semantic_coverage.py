"""整理稿内容复核：允许全文上下文纠错，仅阻断明显大幅删稿或整段搬移。"""
import difflib
import re
import unicodedata
from collections import Counter
from decimal import Decimal


_MAX_DIFF_TOKENS = 2_000
_LOOKAHEAD_TOKENS = 32
_MAX_AUDIT_NOTES = 8
_MIN_MAJOR_OMISSION_TOKENS = 12
_MAJOR_OMISSION_RATIO = .5
_MOVED_PHRASE_TOKENS = 8
_NUMBER_ATOM = r"\d+(?:[.,]\d+)*"
_NUMBERS = re.compile(rf"(?<!\w)[+−-]?{_NUMBER_ATOM}(?:[-−–—]{_NUMBER_ATOM})*"
                      rf"|{_NUMBER_ATOM}(?:[-−–—]{_NUMBER_ATOM})*")
_DECIMAL = re.compile(r"[+-]?\d+(?:\.\d+)?")
_THOUSANDS = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_NUMBER_WORDS = re.compile(
    r"(?<![\w'’])(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|trillion)(?![\w'’])")
_FILLERS = {'um', 'uh', 'erm', 'er'}
_ARTICLES = {'a', 'an', 'the'}
_SUBJECT_PRONOUNS = {'i', 'you', 'we', 'he', 'she', 'it', 'they'}
_LETTER_CONTEXT = {'grade', 'class', 'type', 'letter', 'option', 'vitamin', 'level', 'section'}
_EMPHASIS = {'very', 'really', 'so', 'too', 'quite', 'pretty', 'more', 'most',
             'much', 'many', 'only', 'just', 'even', 'again'}
_PROTECTED_REPEAT_WORDS = {'duran', 'bye'}
_CJK_NEGATIVE_PHRASES = re.compile('没有|不能|不会|不是|不喜欢')
_CJK_NEGATIVE_CHARACTERS = {'不', '没', '未', '无', '别'}
_CJK_NON_NEGATIVE_NAMES = {'不丹', '无锡'}
_NEGATIVES = {'not', 'no', 'never', 'neither', 'nor', 'nothing', 'nobody', 'nowhere',
              'without', 'dont', 'doesnt', 'didnt', 'cant', 'cannot', 'couldnt',
              'wont', 'wouldnt', 'shouldnt', 'mustnt', 'neednt', 'isnt', 'arent',
              'wasnt', 'werent', 'hasnt', 'havent', 'hadnt', 'shant', 'aint'}
_NEGATIVE_AUXILIARIES = {
    'do': 'dont', 'does': 'doesnt', 'did': 'didnt', 'can': 'cant', 'could': 'couldnt',
    'will': 'wont', 'would': 'wouldnt', 'should': 'shouldnt', 'must': 'mustnt',
    'need': 'neednt', 'is': 'isnt', 'are': 'arent', 'was': 'wasnt', 'were': 'werent',
    'has': 'hasnt', 'have': 'havent', 'had': 'hadnt',
    'shall': 'shant',
}
_FUNCTION_WORDS = (_ARTICLES | _SUBJECT_PRONOUNS | _FILLERS | _EMPHASIS |
                   set(_NEGATIVE_AUXILIARIES) |
                   {'and', 'or', 'but', 'then', 'of', 'in', 'on', 'at', 'to', 'from',
                    'for', 'with', 'by', 'about', 'through', 'into', 'this', 'that',
                    'these', 'those', 'my', 'your', 'our', 'their', 'its'})
_CURRENCY_UNITS = {
    'dollar': 'dollar', 'dollars': 'dollar', 'yuan': 'yuan', 'yen': 'yen',
    'euro': 'euro', 'euros': 'euro', 'cent': 'cent', 'cents': 'cent',
    'usd': 'usd', 'cny': 'cny', 'rmb': 'rmb', 'jpy': 'jpy', 'eur': 'eur', 'gbp': 'gbp',
}
_CURRENCY_CODES = {'usd', 'cny', 'rmb', 'jpy', 'eur', 'gbp'}
_MONETARY_CONTEXT = {'pay', 'paid', 'paying', 'cost', 'costs', 'price', 'prices',
                     'fee', 'fees', 'charge', 'charges', 'charged', 'currency', 'money'}
_MEASUREMENT_UNITS = {
    'meter', 'meters', 'metre', 'metres', 'kilometer', 'kilometers', 'kilometre',
    'kilometres', 'centimeter', 'centimeters', 'centimetre', 'centimetres',
    'millimeter', 'millimeters', 'millimetre', 'millimetres', 'mile', 'miles',
    'gram', 'grams', 'kilogram', 'kilograms', 'liter', 'liters', 'litre', 'litres',
    'celsius', 'fahrenheit', 'kg', 'km', 'cm', 'mm', 'ml',
}
_CONTEXTUAL_UNITS = {'second', 'seconds', 'minute', 'minutes', 'hour', 'hours',
                     'day', 'days', 'night', 'nights', 'foot', 'feet', 'inch', 'inches',
                     'pound', 'pounds', 'degree', 'degrees'}
# 只规范同一单位的英美拼写，不换算数值、量级或币种。
_UNIT_SPELLING = {'metre': 'meter', 'kilometre': 'kilometer', 'centimetre': 'centimeter',
                  'millimetre': 'millimeter', 'litre': 'liter'}


def _cjk(char: str) -> bool:
    return ("\u2e80" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff"
            or "\uac00" <= char <= "\ud7af")


def _tokens(text: str, *, preserve_case: bool = False) -> list[str]:
    """大小写、全角与标点归一；拉丁等词不可拆，CJK按字对比。"""
    text = unicodedata.normalize('NFKC', text)
    if not preserve_case:
        text = text.casefold()
    result, word = [], []

    def flush():
        if word:
            result.append(''.join(word))
            word.clear()

    for i, char in enumerate(text):
        if _cjk(char):
            flush()
            result.append(char)
        elif unicodedata.category(char) == 'Sc' or char in '%‰°':
            flush()
            result.append(char)  # 币种、百分比、度数有实际意义，不能当标点丢弃。
        elif unicodedata.category(char)[0] in 'LMN':
            word.append(char)
        elif (char in "'’" and word and i + 1 < len(text)
              and unicodedata.category(text[i + 1])[0] in 'LMN'):
            continue  # don't / don’t / dont 只差撇号，不视为漏词。
        else:
            flush()
    flush()
    return result


def _number_entries(text: str) -> list[tuple[int, tuple[str, object]]]:
    """只规范明确十进制与三位千分组，不猜单逗号小数、范围或日期。"""
    text = unicodedata.normalize('NFKC', text).casefold()
    entries = []
    for match in _NUMBERS.finditer(text):
        raw = match.group().replace('−', '-')
        if _DECIMAL.fullmatch(raw) or _THOUSANDS.fullmatch(raw):
            value = ('decimal', Decimal(raw.replace(',', '')))
        else:
            value = ('literal', match.group())
        entries.append((match.start(), value))
    # 归一只用于复核记录，不把原ASR数字视为不可纠正的真值。
    entries.extend((match.start(), ('word', match.group())) for match in _NUMBER_WORDS.finditer(text))
    return sorted(entries, key=lambda item: item[0])


def _number_sequence(text: str) -> list[tuple[str, object]]:
    return [value for _, value in _number_entries(text)]


def _unit_entries(tokens: list[str]) -> list[tuple[int, str]]:
    """定位可能的单位变化用于复核，不依据单位词典判定ASR纠错错误。"""
    units = []
    for index, token in enumerate(tokens):
        symbol = len(token) == 1 and (unicodedata.category(token) == 'Sc' or token in '%‰°')
        numeric_context = any(value.isnumeric() or _NUMBER_WORDS.fullmatch(value)
                              for value in tokens[max(0, index - 4):index])
        # Cent/Yuan/Yen也可能是人名或品牌，单独命中词典不是金额证据。
        monetary_context = any(value in _MONETARY_CONTEXT
                               for value in tokens[max(0, index - 4):index])
        currency = token in _CURRENCY_UNITS and (
            numeric_context or token in _CURRENCY_CODES
            or token in {'dollars', 'euros', 'cents'} and monetary_context)
        if (symbol or currency
                or token in _MEASUREMENT_UNITS | _CONTEXTUAL_UNITS and numeric_context):
            singular = token[:-1] if token.endswith('s') and token != 'celsius' else token
            units.append((index, _CURRENCY_UNITS.get(token, _UNIT_SPELLING.get(singular, singular))))
    return units


def _unit_sequence(tokens: list[str]) -> list[str]:
    return [value for _, value in _unit_entries(tokens)]


def _adjacent_repetition(source: list[str], first: int, last: int) -> bool:
    """仅豁免与保留词紧邻的同词重复；CJK叠词和数字不推测为识别重复。"""
    deleted = source[first:last]
    if not deleted or any(token != deleted[0] for token in deleted):
        return False
    token = deleted[0]
    if (not token.isalpha() or token in _EMPHASIS | _NEGATIVES | _PROTECTED_REPEAT_WORDS
            or any(_cjk(char) for char in token)):
        return False
    return ((first > 0 and source[first - 1] == token)
            or (last < len(source) and source[last] == token))


def _negative_entries(tokens: list[str]) -> list[tuple[int, str]]:
    """兼容撇号/缩约展开，定位需要依据全文上下文复核的否定变化。"""
    result = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (token in _NEGATIVE_AUXILIARIES and i + 1 < len(tokens)
                and tokens[i + 1] == 'not'):
            result.append((i, _NEGATIVE_AUXILIARIES[token]))
            i += 2
        else:
            if token in _NEGATIVES:
                result.append((i, 'cant' if token == 'cannot' else token))
            i += 1
    return result


def _negative_sequence(tokens: list[str]) -> list[str]:
    return [value for _, value in _negative_entries(tokens)]


def _standalone_cjk_negative(tokens: list[str], first: int, last: int) -> bool:
    """标记单字否定变化供复核，避免将地名补字推断为否定变化。"""
    if last != first + 1 or tokens[first] not in _CJK_NEGATIVE_CHARACTERS:
        return False
    token = tokens[first]
    next_token = tokens[last] if last < len(tokens) else ''
    return token + next_token not in _CJK_NON_NEGATIVE_NAMES


def _cleanup_reason(source: list[str], original: list[str], first: int, last: int) -> str | None:
    """仅接受能由局部词序证明的窄清理模式，不使用通用停用词列表。"""
    deleted = source[first:last]
    if _adjacent_repetition(source, first, last):
        token = deleted[0]
        # 大写实体的叠词和语气强调不能自动当作识别重复，例如Duran Duran。
        repeated = [i for i in (first - 1, first, last)
                    if 0 <= i < len(source) and source[i] == token]
        if (token in _FUNCTION_WORDS or not all(
                original[i][:1].isupper() for i in repeated)):
            return '清理紧邻的同词识别重复'
    if (deleted and all(token in _FILLERS for token in deleted)
            and all(not token.isupper() for token in original[first:last])):
        return '清理英语口头填充词'
    if (last == first + 1 and deleted[0] in _ARTICLES and last < len(source)
            and source[last] in _SUBJECT_PRONOUNS | _ARTICLES
            and (first == 0 or source[first - 1] not in _LETTER_CONTEXT)
            and not (deleted[0] == 'the' and source[last] == 'you')):
        # “the you I know”中的you可作名词，不能按主语代词自动清理the。
        return '清理紧接代词或另一冠词的悬空冠词'
    if (last == len(source) and last == first + 1 and deleted == ['you']
            and source[-7:] == ['see', 'you', 'in', 'the', 'next', 'one', 'you']):
        return '清理告别固定句末悬挂的重复you'
    return None


def _check_inserted_duplicate(source_counts: Counter, output_counts: Counter,
                              output: list[str], first: int, last: int) -> None:
    """固定三词索引识别无来源的重复抄写，不让长稿进入无界文本搜索。"""
    for i in range(first, last - 2):
        phrase = tuple(output[i:i + 3])
        if (sum(token not in _FUNCTION_WORDS for token in phrase) >= 2
                and source_counts[phrase] and output_counts[phrase] > source_counts[phrase]):
            raise ValueError('整理结果新增了重复的原文片段（' + ' '.join(phrase) +
                             '）；请保留原有表达和强调，不要重复抄写邻近台词')


def _joined_span(source: list[str], output: list[str], i: int, j: int) -> tuple[int, int] | None:
    """最多32词证明粘词/拆词完全保真，不用字符级模糊diff猜测。"""
    left, right = source[i], output[j]
    si, sj = i + 1, j + 1
    while True:
        if left == right:
            return si, sj
        if left.startswith(right) and sj < min(len(output), j + _LOOKAHEAD_TOKENS):
            right += output[sj]
            sj += 1
        elif right.startswith(left) and si < min(len(source), i + _LOOKAHEAD_TOKENS):
            left += source[si]
            si += 1
        else:
            return None


def _bounded_diff(source: list[str], output: list[str]):
    """长稿固定前向32词定位；未找到锚点仅按一对词局部纠错向前推进。"""
    i = j = 0
    while i < len(source) and j < len(output):
        if source[i] == output[j]:
            i, j = i + 1, j + 1
            continue
        joined = _joined_span(source, output, i, j)
        if joined is not None:
            i, j = joined
            continue
        positions = {}
        for offset, token in enumerate(output[j:j + _LOOKAHEAD_TOKENS]):
            positions.setdefault(token, offset)
        anchors = [(offset + positions[token], offset, positions[token])
                   for offset, token in enumerate(source[i:i + _LOOKAHEAD_TOKENS])
                   if token in positions]
        if not anchors:
            yield 'replace', i, i + 1, j, j + 1
            i, j = i + 1, j + 1
            continue
        _, skipped_source, skipped_output = min(anchors)
        yield ('delete' if skipped_output == 0 else 'insert' if skipped_source == 0 else 'replace',
               i, i + skipped_source, j, j + skipped_output)
        i, j = i + skipped_source, j + skipped_output
    if i < len(source):
        yield 'delete', i, len(source), j, j
    elif j < len(output):
        yield 'insert', i, i, j, len(output)


def _excerpt(values) -> str:
    return (' '.join(str(value[1]) if isinstance(value, tuple) else str(value)
                     for value in values) or '∅')[:100]


def _append_note(notes: list[str], note: str, *, priority=False) -> None:
    if len(notes) < _MAX_AUDIT_NOTES:
        notes.append(note)
    elif priority:
        notes[-1] = note


def _audit_sequence(notes, label, before, after, source, output, *, character_positions=False,
                    raw_tokens=False):
    if [value for _, value in before] == [value for _, value in after]:
        return False
    index = 0
    while index < min(len(before), len(after)) and before[index][1] == after[index][1]:
        index += 1
    if index < len(before):
        position, owner = before[index][0], source
    elif index < len(after):
        position, owner = after[index][0], output
    else:
        position, owner = 0, source
    if character_positions:
        position = len(_tokens(unicodedata.normalize('NFKC', owner)[:position]))
    position = min(position, max(0, len(_tokens(source)) - 1))
    if raw_tokens:
        source_words, output_words = _tokens(source, preserve_case=True), _tokens(output, preserve_case=True)
        left = _excerpt([source_words[pos] for pos, _ in before[index:index + 3]])
        right = _excerpt([output_words[pos] for pos, _ in after[index:index + 3]])
    else:
        left = _excerpt([value for _, value in before[index:index + 3]])
        right = _excerpt([value for _, value in after[index:index + 3]])
    _append_note(notes, f'原文约第{position + 1}个词附近：{label}调整 {left} → {right}；'
                 '允许依据全文上下文纠正识别，请复核；此记录不代表语义正确')
    return True


def _repeated_deleted_phrase(tokens: list[str], first: int, last: int) -> bool:
    """只证明字面邻接重复，不把次数或语气推定为语义无关。"""
    deleted = tokens[first:last]
    size = len(deleted)
    if not deleted:
        return False
    if (first >= size and tokens[first - size:first] == deleted
            or tokens[last:last + size] == deleted):
        return True
    # 多次口吃最多查32词周期；每个删除块只作固定次数扫描。
    for period in range(1, min(_LOOKAHEAD_TOKENS, size) + 1):
        if size % period:
            continue
        phrase = deleted[:period]
        if (all(deleted[index] == phrase[index % period] for index in range(size))
                and (first >= period and tokens[first - period:first] == phrase
                     or tokens[last:last + period] == phrase)):
            return True
    return False


def _correction_atom(token: str) -> bool:
    return (any(character.isnumeric() for character in token)
            or _NUMBER_WORDS.fullmatch(token) is not None
            or token in _NEGATIVES | _CJK_NEGATIVE_CHARACTERS | set(_CURRENCY_UNITS)
            | _MEASUREMENT_UNITS | _CONTEXTUAL_UNITS
            or len(token) == 1 and (unicodedata.category(token) == 'Sc' or token in '%‰°'))


def _ordinary_tokens(tokens):
    return [token for token in tokens if not _correction_atom(token)]


def _span_equivalent(left, right, *, numbers_same, negation_same):
    if ''.join(left) == ''.join(right):
        return True
    def unit_spelling(token):
        singular = token[:-1] if token.endswith('s') and token != 'celsius' else token
        if token in _MEASUREMENT_UNITS | _CONTEXTUAL_UNITS | set(_CURRENCY_UNITS):
            return _CURRENCY_UNITS.get(token, _UNIT_SPELLING.get(singular, singular))
        return token
    if [unit_spelling(token) for token in left] == [unit_spelling(token) for token in right]:
        return True
    if numbers_same and left and right and all(token.isnumeric() for token in left + right):
        return True
    if (negation_same and left and right
            and all(token in _NEGATIVES | set(_NEGATIVE_AUXILIARIES) for token in left + right)
            and _negative_sequence(left) == _negative_sequence(right)):
        return True
    return False


def _check_major_movement(expected, actual, left_spans, right_spans):
    """短语法调整不拦；只检查较长独特片段搬移或明显整体词序置换。"""
    before, after = _ordinary_tokens(expected), _ordinary_tokens(actual)
    ordinary_left = [_ordinary_tokens(span) for span in left_spans]
    ordinary_right = [_ordinary_tokens(span) for span in right_spans]
    width = _MOVED_PHRASE_TOKENS
    full = Counter(tuple(before[index:index + width]) for index in range(len(before) - width + 1))
    removed = {tuple(span[index:index + width]) for span in ordinary_left
               for index in range(len(span) - width + 1)}
    added = {tuple(span[index:index + width]) for span in ordinary_right
             for index in range(len(span) - width + 1)}
    if any(full[phrase] == 1 and sum(token not in _FUNCTION_WORDS for token in phrase) >= 3
           for phrase in removed & added):
        raise ValueError('整理结果明显搬移了较长的原文片段，请保留原台词顺序和句子归属')
    if (len(before) >= 12 and before != after and Counter(before) == Counter(after)
            and max((len(span) for span in ordinary_left), default=0) >= 12):
        raise ValueError('整理结果明显重排了较长原文的整体词序，请保留原台词顺序')


def validate_source_preservation(parts: list[dict], source: str, *, cleanup_budget: int = 3) -> list[str]:
    """允许全文上下文纠错，返回最多8条含位置和前后值的中文复核记录。

    原ASR不是人名、数字、币种或否定的事实真值；局部变化不因此拒绝整稿。
    cleanup_budget保留调用兼容性，只作清理量复核提示，不再机械阻断。
    仅非重复删減至少12词且达全文一半、明显长片段搬移或额外抄写硬拒。
    格式与时间由调用方单独验证；程序不能证明纠错语义正确或证明全部信息完整。
    """
    text = ' '.join(str(part.get('text', '')) for part in parts)
    expected, actual = _tokens(source), _tokens(text)
    notes: list[str] = []
    if not expected:
        return notes
    if not actual:
        raise ValueError('整理结果未保留原文台词，请返回全文全部内容')
    number_changed = _audit_sequence(notes, '数字', _number_entries(source), _number_entries(text),
                                     source, text, character_positions=True)
    negative_changed = _audit_sequence(notes, '否定', _negative_entries(expected),
                                       _negative_entries(actual), source, text)
    def cjk_entries(tokens):
        chars = ''.join(token if len(token) == 1 and _cjk(token) else '|' for token in tokens)
        return [(match.start(), match.group()) for match in _CJK_NEGATIVE_PHRASES.finditer(chars)]
    _audit_sequence(notes, '中文否定', cjk_entries(expected), cjk_entries(actual), source, text)
    unit_changed = _audit_sequence(notes, '单位/币种', _unit_entries(expected),
                                   _unit_entries(actual), source, text, raw_tokens=True)
    if ''.join(expected) == ''.join(actual):
        return notes
    operations = (difflib.SequenceMatcher(a=expected, b=actual, autojunk=False).get_opcodes()
                  if max(len(expected), len(actual)) <= _MAX_DIFF_TOKENS
                  else _bounded_diff(expected, actual))
    original, candidate = _tokens(source, preserve_case=True), _tokens(text, preserve_case=True)
    source_counts = Counter(tuple(expected[index:index + 3]) for index in range(len(expected) - 2))
    output_counts = Counter(tuple(actual[index:index + 3]) for index in range(len(actual) - 2))
    cleaned_tokens = omitted_tokens = 0
    left_spans, right_spans = [], []
    for tag, i1, i2, j1, j2 in operations:
        if tag == 'equal':
            continue
        left, right = expected[i1:i2], actual[j1:j2]
        if _span_equivalent(left, right, numbers_same=not number_changed, negation_same=not negative_changed):
            continue
        if tag == 'insert' and j2 - j1 >= 3:
            _check_inserted_duplicate(source_counts, output_counts, actual, j1, j2)
        left_spans.append(left)
        right_spans.append(right)
        before, after = _excerpt(original[i1:i2]), _excerpt(candidate[j1:j2])
        reason = '局部文字/语法纠错复核'
        if tag == 'delete':
            cleaned_tokens += i2 - i1
            known = _cleanup_reason(expected, original, i1, i2)
            repeated = _repeated_deleted_phrase(expected, i1, i2)
            if known:
                reason = known
            elif repeated:
                reason = ('少量ASR原词未保留，相邻重复文字或口吃清理复核；'
                          '无法仅凭文本确认是否为强调、专名或识别重复，请复核')
            else:
                omitted_tokens += i2 - i1
                reason = '少量ASR原词未保留，无法仅凭文本确认是识别噪声或实质遗漏，请结合原音核对'
        elif tag == 'replace' and not all(_correction_atom(token) for token in left + right):
            omitted_tokens += max(0, len(left) - len(right))
        standalone_negative = (_standalone_cjk_negative(expected, i1, i2) if left else False) or (
            _standalone_cjk_negative(actual, j1, j2) if right else False)
        if standalone_negative:
            reason = '中文否定/语法纠错复核'
        # 类型级记录已给出前后值时，不重复逐字输出同一次数值/单位修改。
        typed_change = number_changed or negative_changed or unit_changed
        if not (typed_change and left + right and all(_correction_atom(token) for token in left + right)):
            _append_note(notes, f'原文第{i1 + 1}个词起（{before}）：{reason}；'
                         f'{before} → {after}；此记录不代表语义正确')
    _check_major_movement(expected, actual, left_spans, right_spans)
    if omitted_tokens >= _MIN_MAJOR_OMISSION_TOKENS and omitted_tokens >= len(expected) * _MAJOR_OMISSION_RATIO:
        raise ValueError(f'整理结果疑似明显大幅遗漏原文：至少{omitted_tokens}词未保留，'
                         f'占全文约{omitted_tokens / len(expected):.0%}；请返回完整台词，勿将全文概括为摘要')
    if cleaned_tokens > max(0, cleanup_budget):
        _append_note(notes, f'清理量复核：{cleaned_tokens}词 → 已清理；超过参考{max(0, cleanup_budget)}词，'
                     '允许依据全文上下文清理，请复核重要信息；不因此拒绝整稿', priority=True)
    return notes
