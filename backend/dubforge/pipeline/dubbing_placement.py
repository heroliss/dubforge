"""配音铺轨计划：有界局部排布与完整音频的实际冲突判断。"""
import math

from dubforge.errors import DubForgeError


EPSILON_SECONDS = .05
# 成片中相邻中文句子完全首尾相接时，播放器/编码器的量化误差会让听感像
# “重叠”。这个间隔计入局部联动窗口的容量；放不下时会触发 needs_scale，
# 绝不通过裁剪音频来制造间隔。
DEFAULT_INTER_SENTENCE_GAP = .08


def _bounded_starts(rows, durations, lo, hi, shift, gap):
    """固定时长的有界保序最小移动；不可行时返回 None，不裁切音频。"""
    lowers = [max(lo, row[0] - shift) for row in rows]
    uppers = [min(row[0] + shift, hi - duration)
              for row, duration in zip(rows, durations)]
    cursor = lo
    for lower, upper, duration in zip(lowers, uppers, durations):
        start = max(lower, cursor)
        if start > upper:
            return None
        cursor = start + duration + gap
    # 去掉前序音频时长后，非重叠约束即为单调约束。PAVA 合并冲突块，
    # 使整体前移/后移的平方偏差最小；每句仍受源起点附近的位移上限约束。
    prefix = 0.0
    blocks = []
    offsets = []
    for index, (row, duration, lower, upper) in enumerate(zip(rows, durations, lowers, uppers)):
        offsets.append(prefix)
        block = dict(first=index, last=index, total=row[0] - prefix, count=1,
                     lower=lower - prefix, upper=upper - prefix)
        block['value'] = max(block['lower'], min(block['upper'], block['total']))
        blocks.append(block)
        while len(blocks) > 1 and blocks[-2]['value'] > blocks[-1]['value'] + 1e-10:
            right, left = blocks.pop(), blocks.pop()
            block = dict(first=left['first'], last=right['last'],
                         total=left['total'] + right['total'], count=left['count'] + right['count'],
                         lower=max(left['lower'], right['lower']),
                         upper=min(left['upper'], right['upper']))
            block['value'] = max(block['lower'], min(block['upper'], block['total'] / block['count']))
            blocks.append(block)
        prefix += duration + gap
    starts = [0.0] * len(rows)
    for block in blocks:
        for index in range(block['first'], block['last'] + 1):
            starts[index] = block['value'] + offsets[index]
    return starts


def _window_proposal(rows, lo, hi, shift, gap):
    """先原速排布；不足时优先回收超出原槽的时长，再给出其余缩时建议。"""
    actual = [row[3] for row in rows]
    starts = _bounded_starts(rows, actual, lo, hi, shift, gap)
    if starts is not None:
        return starts, actual, True
    normal = [min(duration, max(0.0, row[1] - row[0]))
              for row, duration in zip(rows, actual)]
    # 仅缩短实际长于源字幕槽的部分，短句不因别的句子超长而被压速。
    def long_only(factor):
        return [base + (duration - base) * factor for duration, base in zip(actual, normal)]
    if _bounded_starts(rows, normal, lo, hi, shift, gap) is not None:
        duration_for = long_only
    else:
        # 安全间隔也要占用时间；确实超长的句子可以继续缩短，不能
        # 因为它已缩回原槽就立即转而压缩旁边的短句。
        floors = [0.0 if duration > base + 1e-10 else duration
                  for duration, base in zip(actual, normal)]
        if _bounded_starts(rows, floors, lo, hi, shift, gap) is not None:
            duration_for = lambda factor: [floor + (base - floor) * factor
                                          for floor, base in zip(floors, normal)]
        else:
            duration_for = lambda factor: [duration * factor for duration in normal]
    minimum = duration_for(0.0)
    starts = _bounded_starts(rows, minimum, lo, hi, shift, gap)
    if starts is None:
        # 连安全间隔自身都放不下时，不伪造可用窗口或缩掉间隔。
        return [row[0] for row in rows], [0.0] * len(rows), False
    low, high = 0.0, 1.0
    for _ in range(48):
        middle = (low + high) / 2
        candidate = duration_for(middle)
        if _bounded_starts(rows, candidate, lo, hi, shift, gap) is None:
            high = middle
        else:
            low = middle
    durations = duration_for(low)
    return _bounded_starts(rows, durations, lo, hi, shift, gap), durations, False


def _window_entries(rows, starts, durations, ready, window_id, left, right):
    """原速结果与缩时建议共用元数据，不把建议容量冒充实际音频时长。"""
    result = []
    for row, start, duration in zip(rows, starts, durations):
        scale = min(1.0, max(0.0, duration / row[3]))
        item = dict(row[2])
        item.update(allocated_start=start, allocated_end=start + duration,
                    actual_end=start + row[3], audio_duration=row[3],
                    available_start=start, available_end=start + duration,
                    available_seconds=duration,
                    left_extension=max(0.0, row[0] - start),
                    right_extension=max(0.0, start + duration - row[1]),
                    needs_scale=scale < 1.0 - 1e-7, scale_factor=scale,
                    shift_seconds=start - row[0],
                    window_id=window_id, window_start=left, window_end=right,
                    window_lines=len(rows), window_ready=ready,
                    island_start=left, island_end=right, island_seconds=right - left)
        result.append(item)
    return result


def allocate_voice_windows(lines, *, island_gap=0.8, max_shift_seconds=2.0,
                           window_lines=9, video_duration=None,
                           inter_sentence_gap=DEFAULT_INTER_SENTENCE_GAP):
    """先在硬障碍之间有界原速排布，不可行时按 window_lines 局部分窗建议缩时。

    不修改源字幕、实际音频时长或文件；allocated_start/end 是建议窗口，
    actual_end 才是完整音频在该起点的真实尾部。只有 window_ready 为真时，
    该窗口的全部原速音频才能安全采用此排布。需要重配时，上层使用建议
    容量调用模型后重新计算，不能把建议窗口当成音频已经缩短。

    original、silent、未知时长与人工位置为硬障碍。先对完整连续段做线性
    原速排布，避免机械分窗阻断可用空白；每句起点最多偏离源起点
    max_shift_seconds，不因联动句数增加而放宽。原速仍不可行时，才以源
    空隙中点划分局部缩时分析窗口；window_lines 只限制这一步的窗口大小。
    已知视频时长时可借用片尾空白；未知时不猜测末句之后的视频空间。
    island_gap 仅保留旧调用兼容，不再用 ASR 的停顿阈值限制可用空白。
    """
    if type(island_gap) not in (int, float) or not math.isfinite(island_gap) or island_gap < 0:
        raise DubForgeError('连续语音岛停顿阈值无效', code='DUB_TIMELINE_INVALID')
    if (type(max_shift_seconds) not in (int, float) or not math.isfinite(max_shift_seconds)
            or max_shift_seconds < 0):
        raise DubForgeError('配音最大滑动秒数无效', code='DUB_TIMELINE_INVALID')
    if type(window_lines) is not int or not 3 <= window_lines <= 15:
        raise DubForgeError('配音缩时分析窗口需为 3～15 句', code='DUB_TIMELINE_INVALID')
    if (type(inter_sentence_gap) not in (int, float)
            or not math.isfinite(inter_sentence_gap) or inter_sentence_gap < 0):
        raise DubForgeError('句间安全间隔无效', code='DUB_TIMELINE_INVALID')
    total = None
    if video_duration is not None:
        total = _seconds(video_duration)
        if total <= 0:
            raise DubForgeError('视频时长无效', code='DUB_TIMELINE_INVALID')
    ordered = []
    for line in lines:
        start, end = _seconds(line['start']), _seconds(line['end'])
        source = line.get('source', 'dub')
        if end < start or source not in {'dub', 'original', 'silent'}:
            raise DubForgeError('配音时间轴或声源无效，请重新准备字幕或配音', code='DUB_TIMELINE_INVALID')
        duration = line.get('seconds', line.get('audio_duration'))
        if source == 'original':
            duration = end - start
        elif source == 'silent':
            duration = None
        if duration is not None:
            duration = _seconds(duration)
            if duration <= 0:
                duration = None
        if line.get('fixed_start') is not None:
            _seconds(line['fixed_start'])
            if line.get('fixed_end') is not None:
                fixed_end = _seconds(line['fixed_end'])
                if fixed_end <= line['fixed_start']:
                    raise DubForgeError('人工播放窗口结束必须晚于开始', code='DUB_TIMELINE_INVALID')
        ordered.append((start, end, dict(line), duration))
    ordered.sort(key=lambda item: (item[0], item[2].get('id', 0)))
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1] - EPSILON_SECONDS:
            raise DubForgeError(
                f"第 {previous[2].get('id', '?')} 句与第 {current[2].get('id', '?')} 句原始时间重叠，无法安全排布配音",
                code='DUB_TIMELINE_INVALID')
    result, index, window_id = [], 0, 0
    while index < len(ordered):
        row = ordered[index]
        if (row[2].get('source', 'dub') != 'dub' or row[3] is None
                or row[2].get('fixed_start') is not None):
            item = dict(row[2])
            fixed = row[2].get('fixed_start')
            start = float(fixed) if fixed is not None and row[3] is not None else row[0]
            actual_end = (start + row[3] if row[3] is not None else
                          start if row[2].get('source') == 'silent' else None)
            end = (row[2].get('fixed_end') if row[2].get('fixed_end') is not None else actual_end)
            if fixed is None or end is None:
                end = row[1]
            capacity_end = min(end, total) if fixed is not None and total is not None else end
            if fixed is not None:
                capacity_end = max(start, capacity_end)
            available = max(0.0, capacity_end - start)
            needs_scale = bool(fixed is not None and row[3] is not None
                               and available < row[3] - .0005)
            item.update(allocated_start=start, allocated_end=capacity_end if fixed is not None else end,
                        actual_end=actual_end, audio_duration=row[3],
                        available_start=start, available_end=capacity_end, available_seconds=available,
                        left_extension=max(0.0, row[0] - start),
                        right_extension=max(0.0, end - row[1]), needs_scale=needs_scale,
                        scale_factor=min(1.0, available / row[3]) if fixed is not None and row[3] else None,
                        shift_seconds=start - row[0],
                        window_id=None, window_ready=not needs_scale)
            result.append(item)
            index += 1
            continue
        first = index
        while (index < len(ordered) and ordered[index][2].get('source', 'dub') == 'dub'
               and ordered[index][3] is not None and ordered[index][2].get('fixed_start') is None):
            index += 1
        segment_end = index
        previous = ordered[first - 1] if first else None
        following = ordered[index] if index < len(ordered) else None
        segment_left = previous[1] if previous else 0.0
        if previous and previous[2].get('fixed_start') is not None:
            segment_left = max(previous[2].get('fixed_end') or 0,
                               previous[2]['fixed_start'] + (previous[3] or 0)) + inter_sentence_gap
        elif previous and previous[2].get('source') == 'original' and previous[3] is not None:
            segment_left += inter_sentence_gap
        segment_right = following[0] if following else (
            total if total is not None else ordered[index - 1][1])
        if following and following[2].get('fixed_start') is not None:
            segment_right = following[2]['fixed_start'] - inter_sentence_gap
        elif following and following[2].get('source') == 'original' and following[3] is not None:
            segment_right -= inter_sentence_gap
        if total is not None:
            segment_right = min(segment_right, total)
        active = ordered[first:segment_end]
        durations = [row[3] for row in active]
        starts = _bounded_starts(active, durations, segment_left, segment_right,
                                 float(max_shift_seconds), float(inter_sentence_gap))
        if starts is not None:
            # 原速可行时不制造窗口接缝；报告真实联动段，不能仍声称仅协调了
            # 配置窗口内的句子。计算始终依据源时间，不累积上轮排布的位移。
            window_id += 1
            result.extend(_window_entries(active, starts, durations, True, window_id,
                                          segment_left, segment_right))
            continue
        left = segment_left
        offset = first
        while offset < segment_end:
            stop = min(offset + window_lines, segment_end)
            if stop < segment_end:
                # 最大窗口以内优先在已有大空隙断开；同样大时选择更远的
                # 边界，避免紧密台词被机械切成许多小块。
                stop = max(range(offset + 3, stop + 1),
                           key=lambda cut: (ordered[cut][0] - ordered[cut - 1][1], cut))
            boundary = ((ordered[stop - 1][1] + ordered[stop][0]) / 2
                        if stop < segment_end else segment_right)
            # 跨窗口的两句同样需要安全间隔，不能在窗口接缝处首尾相接。
            right = boundary - inter_sentence_gap / 2 if stop < segment_end else boundary
            right = max(left, right)
            active = ordered[offset:stop]
            starts, durations, ready = _window_proposal(
                active, left, right, float(max_shift_seconds), float(inter_sentence_gap))
            window_id += 1
            result.extend(_window_entries(active, starts, durations, ready, window_id,
                                          left, right))
            left = boundary + inter_sentence_gap / 2
            offset = stop
    return result


# 名称较短的别名，供工作台/成片步骤接入；保留一个语义明确的主 API。
allocate_timing = allocate_voice_windows


def _seconds(value, *, allow_none=False):
    if allow_none and value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise DubForgeError('配音时间轴包含无效时间，请重新准备字幕或配音',
                            code='DUB_TIMELINE_INVALID')
    return float(value)


def build_plan(lines, video_duration=None):
    """纯函数；不移动台词、不裁音频。未知时长不猜测，空位不形成声音冲突。"""
    total = (float(video_duration) if type(video_duration) in (int, float)
             and math.isfinite(video_duration) and video_duration > 0 else None)
    rows = []
    for line in lines:
        start, end = _seconds(line['start']), _seconds(line['end'])
        source = line['source']
        if end < start or source not in {'dub', 'original', 'silent'}:
            raise DubForgeError('配音时间轴或声源无效，请重新准备字幕或配音',
                                code='DUB_TIMELINE_INVALID')
        seconds = (end - start if source == 'original' else 0.0 if source == 'silent'
                   else _seconds(line.get('seconds'), allow_none=True))
        rows.append(dict(id=line['id'], source=source, take=line.get('take'),
                         start=start, source_end=end, seconds=seconds,
                         audio_end=start + seconds if seconds is not None else None,
                         media_key=line.get('media_key')))
    rows.sort(key=lambda row: (row['start'], row['id']))
    signature = [[r[k] for k in ('id', 'start', 'source_end', 'source', 'take', 'seconds', 'media_key')]
                 for r in rows]
    audible = [r for r in rows if r['seconds'] is not None and r['seconds'] > 0]
    issues = []
    for index, row in enumerate(rows):
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        row['target_end'] = next_row['start'] if next_row else row['source_end']
        if row['source'] == 'dub' and row['audio_end'] is not None:
            excess = row['audio_end'] - row['target_end']
            if excess > EPSILON_SECONDS + 1e-9:
                issues.append(dict(kind='target_overrun', id=row['id'],
                                   next_id=next_row['id'] if next_row else None,
                                   available=max(0.0, row['target_end'] - row['start']), excess=excess))
        if total is not None and row['audio_end'] is not None:
            excess = row['audio_end'] - total
            if row['seconds'] > 0 and excess > EPSILON_SECONDS + 1e-9:
                issues.append(dict(kind='video_overrun', id=row['id'], next_id=None,
                                   available=max(0.0, total - row['start']), excess=excess))
    for index, row in enumerate(audible):
        for other_index in range(index + 1, len(audible)):
            other = audible[other_index]
            if other['start'] >= row['audio_end'] - EPSILON_SECONDS:
                break
            overlap = min(row['audio_end'], other['audio_end']) - other['start']
            if overlap > EPSILON_SECONDS + 1e-9:
                issues.append(dict(kind='overlap', id=row['id'], next_id=other['id'],
                                   available=max(0.0, other['start'] - row['start']), excess=overlap))
    return dict(schema=1, video_duration=total, signature=signature, lines=rows, issues=issues)


def region_entries(plan, start, end):
    """明确的试片区域可以裁剪；返回原音频内偏移与区域内铺轨位置。"""
    start, end = _seconds(start), _seconds(end)
    if end <= start:
        raise DubForgeError('试合成区域无效，请重新选择起止时间', code='ASSEMBLE_PREVIEW_RANGE')
    result = []
    for row in plan['lines']:
        if row['audio_end'] is None or not row['seconds']:
            continue
        clip_start, clip_end = max(start, row['start']), min(end, row['audio_end'])
        if clip_end <= clip_start:
            continue
        result.append(dict(**row, offset=clip_start - start,
                           clip_start=clip_start, clip_end=clip_end,
                           audio_offset=clip_start - row['start']))
    return result


def require_video_fit(plan):
    """正式成片禁止静默裁掉视频末尾之外的人声；字幕目标超时只提示。"""
    overruns = [issue for issue in plan['issues'] if issue['kind'] == 'video_overrun']
    if not overruns:
        return
    detail = '；'.join(f"第 {i['id']} 句超出视频结尾 {i['excess']:.2f} 秒" for i in overruns[:8])
    raise DubForgeError(f'配音超出视频结尾，合成已停止以保留完整声音：{detail}',
                        code='DUB_AUDIO_OUTSIDE_VIDEO',
                        suggestion='请在配音页试听这些句子，缩短译文后重配、调整时长因子，或选择原声/空位，再生成成片。')
