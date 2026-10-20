"""目标数量评论的准备草稿；评分、翻译复用工作室，不代替用户确认。"""
import threading
import time
import uuid

from dubforge import comment_studio as cs, video_info
from dubforge.errors import DubForgeError


def prepare(uid, body, *, cancel_check=None, extra_settings=(), canvas=None, capacity=None,
            log=None, phase=None):
    """保护人工选择，按评分增减自动项，仅补缺失结果。"""
    if not isinstance(body, dict) or type(body.get('target')) is not int or not 1 <= body['target'] <= 100:
        raise cs.CommentError('目标上屏数量应为 1–100 条')
    if capacity is not None and (type(capacity) is not int or capacity < 0):
        raise cs.CommentError('评论时间轴容量无效')
    target = body['target']
    prompt = body.get('prompt', '')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
        raise cs.CommentError('请填写评分要求（最多 2000 字）')
    failure_policy = body.get('failure_policy')
    if failure_policy is not None and failure_policy not in ('skip', 'stop'):
        raise cs.CommentError('请选择有效的评论失败处理方式')
    write_log, report_phase = log or (lambda _: None), phase or (lambda _: None)
    event = threading.Event()
    with video_info.operation(uid, 'prepare_comments') as project:
        with video_info.gate:
            cs._check_accepting()
            state = cs.view(uid)
            failure_policy = failure_policy or state['configuration']['values']['comments.failure_policy']
            if body.get('source_version') and body['source_version'] != state['source_version']:
                raise cs.CommentError('评论来源已变化，请重新读取后准备', code='COMMENTS_SOURCE_CHANGED')
            if body.get('revision') and body['revision'] != state['overlay_revision']:
                raise cs.CommentError('上屏方案已变化，请重新读取后准备', code='COMMENTS_PLAN_CHANGED')
            settings = cs.validate_settings(body.get('settings', state['defaults']))
            displays = [cs.validate_settings(value) for value in extra_settings] or [settings]
            source_rows = {row['id']: row for row in state['rows']}
            scope = body.get('ids', list(source_rows))
            if (not isinstance(scope, list) or len(scope) > 500
                    or any(not isinstance(ident, str) or ident not in source_rows for ident in scope)
                    or len(set(scope)) != len(scope)):
                raise cs.CommentError('准备评论的来源范围无效', code='COMMENTS_SCOPE_INVALID')
            overlay = state['overlay'] or {}
            order_mode = cs.validate_order_mode(body.get('order_mode', overlay.get('order_mode', 'chronological')))
            raw_items = body.get('items', overlay.get('items', []))
            exclusions = body.get('excluded_ids', overlay.get('excluded_ids', []))
            if not isinstance(raw_items, list) or len(raw_items) > 100:
                raise cs.CommentError('最多保留 100 条上屏评论')
            if (not isinstance(exclusions, list) or len(exclusions) > 500 or any(
                    not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or not isinstance(row.get('source_hash'), str) for row in exclusions)
                    or len({row['id'] for row in exclusions}) != len(exclusions)):
                raise cs.CommentError('手动排除评论的来源标识无效', code='COMMENTS_SCOPE_INVALID')
            exclusions = [{'id': row['id'], 'source_hash': row['source_hash']} for row in exclusions]
            available = {(row['id'], row['source_hash']): row for row in state['rows']}
            saved_items = {(row['id'], row['source_hash']): row for row in overlay.get('items', [])}
            for row in saved_items.values():
                available.setdefault((row['id'], row['source_hash']), row)
            protected, previous_items, seen = [], {}, set()
            for item in raw_items:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('source_hash'), str):
                    raise cs.CommentError('评论标识无效')
                original = available.get((item['id'], item['source_hash']))
                if original is None or item['id'] in seen:
                    raise cs.CommentError('评论原文已变化或重复，请重新核对', code='COMMENTS_SOURCE_CHANGED')
                seen.add(item['id'])
                origin = cs.selection_origin(item, saved_items.get((item['id'], item['source_hash'])))
                text, source = item.get('display_text', ''), item.get('source_text', original['text'])
                if not isinstance(text, str) or not isinstance(source, str) or len(text) > 15000 or len(source) > 12000:
                    raise cs.CommentError('评论文字格式或长度无效', code='COMMENTS_DISPLAY_TEXT')
                entry = {key: original.get(key) for key in ('id', 'source_hash', 'text', 'author', 'url')}
                entry.update(display_text=text.strip(), source_text=source.strip(), selection_origin=origin,
                             timestamp=cs._comment_timestamp(original.get('timestamp')))
                previous_items[(entry['id'], entry['source_hash'])] = entry
                if origin == 'automatic':
                    continue
                protected.append(entry)
            protected_ids = {row['id'] for row in protected}
            manual_count = sum(row['selection_origin'] == 'manual' for row in protected)
            exclusions = [row for row in exclusions if row['id'] not in protected_ids]
            excluded = {row['id'] for row in exclusions if row['source_hash'] == source_rows.get(row['id'], {}).get('source_hash')}
            review_settings = cs._review_settings(uid, state['configuration']['values'])
            # 按同源缓存快照统计整个准备过程，不能累加子批次的 cached/processed。
            initial_translations = {(row['id'], row['source_hash']): row['text_zh'].strip()
                                    for row in state['rows'] if row.get('text_zh', '').strip()}
            doc = cs._read(project)
            initial_ratings = {(row['id'], row['source_hash']) for row in cs._cached_ratings(
                state['rows'], doc.get('rating_cache') or doc.get('filter', {}), prompt,
                cs._context(project)).values() if row['id'] in scope}
            job = dict(id=uuid.uuid4().hex, action='prepare', status='running', stage='preparing',
                       message='正在检查已保存的评分、译文和保留选择', target=target, selected=len(protected),
                       overlay_signature=cs._hash(doc.get('overlay')),
                       started_at=cs._now(), updated_at=cs._now(), cancelling=False, error='', code='')
            cs._prepare_jobs[uid], cs._prepare_cancels[uid] = job, event
        # 沿用旧接口名以兼容自动流程门禁；条目携带真实来源，汇总分别统计。
        warnings, invalid_manual, excluded_auto = [], [], {}
        needs_translation = any(current['language_mode'] != 'original' for current in displays)
        repair_budget, rejection_streak, stop_requests = 24, 0, False
        deadline = time.monotonic() + 2 * cs.PROCESSING_TIMEOUT_SECONDS
        translation_deadline = None

        def check():
            cs._check_accepting()
            if cancel_check:
                cancel_check()
            if event.is_set():
                raise cs.CommentError('评论准备已停止，已完成评分和译文保留；人工草稿未被覆盖', code='COMMENTS_CANCELLED')
            if time.monotonic() > deadline:
                raise cs.CommentError('评论准备超时，已完成评分和译文保留；人工草稿未被覆盖', code='COMMENTS_TIMEOUT')

        def report(stage, message, selected=None):
            with video_info.gate:
                job.update(stage=stage, message=message, updated_at=cs._now())
                if selected is not None:
                    job['selected'] = selected
            write_log(message)
            report_phase(message)
            check()

        def requestable(action, identifiers):
            # 普通准备不代表显式重试；已拦截、失败及止损待处理项均保留人工处理。
            with video_info.gate:
                doc = cs._read(project)
                fingerprint = cs._processing_key(action, prompt, cs._context(project), cs._profiles())
                issues = cs._current_issues(doc, list(source_rows.values()), action, fingerprint)
                valid = cs._cached_ratings(list(source_rows.values()), doc.get('rating_cache') or doc.get('filter', {}),
                    prompt, cs._context(project)) if action == 'filter' else {}
            held = [ident for ident in identifiers if ident not in valid
                    and issues.get(ident, {}).get('status') in {'blocked', 'failed', 'deferred'}]
            if held and failure_policy == 'stop':
                blocked = any(issues[ident]['status'] == 'blocked' for ident in held)
                raise cs.CommentError('评论仍有已记录的问题；严格模式已停止准备，未重复提交，请查看原因后定向处理',
                    code='COMMENTS_BLOCKED' if blocked else 'COMMENTS_PENDING',
                    detail={'ids': held})
            return [ident for ident in identifiers if ident not in held]

        def refresh():
            nonlocal state, source_rows
            state = cs.view(uid)
            source_rows = {row['id']: row for row in state['rows']}

        def translate(identifiers, selected):
            nonlocal state, source_rows, repair_budget, rejection_streak, stop_requests, translation_deadline
            if not needs_translation or stop_requests:
                return
            pending = requestable('translate', [ident for ident in identifiers if not source_rows[ident]['text_zh']])
            if not pending:
                return
            if translation_deadline is None:
                translation_deadline = time.monotonic() + cs.PROCESSING_TIMEOUT_SECONDS
            report('translating', f'评论翻译：已选 {selected}/{target} 条，本批 {len(pending)} 条候选', selected)
            state = cs.run(uid, 'translate', ids=pending, failure_policy=failure_policy,
                cancel_check=check, recovery_budget=repair_budget, budget_deadline=translation_deadline,
                rejection_streak=rejection_streak, _project_scope=project)
            source_rows = {row['id']: row for row in state['rows']}
            child = state['job']
            repair_budget = max(0, repair_budget - child.get('isolation_requests', 0))
            rejection_streak = child.get('rejection_streak', 0)
            stop_requests = rejection_streak >= 3 or bool(child.get('deferred'))
            if child.get('warning'):
                warnings.append('翻译：' + child['warning'])

        def reasons(item):
            current = source_rows.get(item['id'])
            found = cs.review_reasons({**item, 'text_zh': current.get('text_zh', '') if current else ''},
                                      review_settings, originals=(current,))
            found += [issue for display in displays for issue in cs.blocking_text_issues(item, display)]
            if not found and canvas:
                from dubforge.pipeline import comment_overlay
                for display in displays:
                    try:
                        comment_overlay.ass_document({'settings': display, 'items': [item]}, canvas)
                    except cs.CommentError as exc:
                        if exc.code != 'COMMENTS_LAYOUT_OVERFLOW':
                            raise
                        found.append('当前画幅和样式下无法完整显示')
            return list(dict.fromkeys(found))

        try:
            check()
            # 页面和自动流程都按真实媒体检查容量；没有媒体的资料编辑不猜视频长度。
            if canvas is None or capacity is None:
                from dubforge.domain import crud
                from dubforge.pipeline.video_source import resolve_video
                from dubforge.pipeline import comment_overlay
                from dubforge.publishing.service import _probe
                video = resolve_video(project, crud.project_dir_by_project(project))
                if video is not None and video.is_file():
                    if canvas is None:
                        canvas = comment_overlay.video_canvas(video)
                    if capacity is None:
                        duration = _probe(video)
                        capacity = max(0, int((duration - settings['start'] - settings['duration']) // settings['interval']) + 1)
            # 仅保护人工选择；只补空译文，不改写已有文字。
            effective_target = min(target, capacity) if capacity is not None else target
            automatic_needed = max(0, effective_target - len(protected))
            score_scope = [ident for ident in scope if ident not in protected_ids and ident not in excluded]
            if automatic_needed:
                permitted = requestable('filter', score_scope)
                if permitted:
                    report('scoring', f'评论评分：本次 {len(permitted)} 条，复用有效评分', len(protected))
                    state = cs.run(uid, 'filter', ids=permitted, prompt=prompt, limit=target,
                        failure_policy=failure_policy, cancel_check=check, budget_deadline=deadline,
                        _project_scope=project)
                    source_rows = {row['id']: row for row in state['rows']}
                    if state['job'].get('warning'):
                        warnings.append('评分：' + state['job']['warning'])
            pending_protected = [row['id'] for row in protected if not row['display_text'] and row['id'] in source_rows
                              and row['source_hash'] == source_rows[row['id']]['source_hash']]
            for offset in range(0, len(pending_protected), 10):
                translate(pending_protected[offset:offset + 10], len(protected))
            for row in protected:
                current = source_rows.get(row['id'])
                changed = current is not None and row['source_hash'] != current['source_hash']
                if not changed and not row['display_text'] and current:
                    row['display_text'] = current['text_zh']
                found = reasons(row)
                if changed:
                    found.insert(0, '来源原文已变化，保留现有文字，请重新核对')
                if found:
                    invalid_manual.append(dict(id=row['id'], selection_origin=row['selection_origin'], reasons=found))
            long_manual = sum(any(cs.overlay_text_issues(row, display) for display in displays)
                              and not any(cs.blocking_text_issues(row, display) for display in displays)
                              for row in protected)
            if long_manual:
                warnings.append(f'{long_manual} 条手动保留评论超过建议字数，仍会完整上屏；长文字将缩小字号，可能需要延长停留时间。')
            automatic = []
            if automatic_needed:
                with video_info.gate:
                    doc = cs._read(project)
                    ratings = cs._cached_ratings(list(source_rows.values()), doc.get('rating_cache') or doc.get('filter', {}), prompt, cs._context(project))
                candidates = sorted((row for ident, row in ratings.items() if ident in score_scope and row['eligible']),
                                    key=lambda row: (-row['score'], scope.index(row['id'])))
                cursor = 0
                while len(automatic) < automatic_needed and cursor < len(candidates):
                    check()
                    group = []
                    while cursor < len(candidates) and len(group) < min(10, automatic_needed - len(automatic)):
                        candidate = candidates[cursor]
                        cursor += 1
                        row = source_rows[candidate['id']]
                        # 先排除确定不能上屏的原文，避免花费翻译请求。
                        found = cs.review_reasons(row, review_settings)
                        for display in displays:
                            if display['language_mode'] != 'translated':
                                found += cs.overlay_text_issues({'source_text': row['text']}, {**display, 'language_mode': 'original'})
                        if found:
                            excluded_auto[row['id']] = found
                        else:
                            group.append(row['id'])
                    to_translate = [ident for ident in group if not (
                        (previous := previous_items.get((ident, source_rows[ident]['source_hash'])))
                        and previous['display_text'] and not reasons(previous))]
                    translate(to_translate, len(protected) + len(automatic))
                    for ident in group:
                        row = source_rows[ident]
                        entry = {key: row.get(key) for key in ('id', 'source_hash', 'text', 'author', 'url')}
                        entry.update(display_text=row['text_zh'], source_text=row['text'], selection_origin='automatic',
                                     timestamp=cs._comment_timestamp(row.get('timestamp')))
                        previous = previous_items.get((ident, row['source_hash']))
                        if previous and (previous['display_text'] or not needs_translation) and not reasons(previous):
                            entry = {**previous, 'selection_origin': 'automatic'}
                        found = reasons(entry)
                        if found:
                            excluded_auto[ident] = found
                        else:
                            automatic.append(entry)
                    report('selecting', f'评论筛选：已准备 {len(protected) + len(automatic)}/{target} 条', len(protected) + len(automatic))
            items = protected + automatic
            if order_mode == 'chronological':
                items = cs.chronological_items(items, source_rows)
            else:
                # 人工调整出现顺序与人工保留选人是两种独立意图；仅保留仍入选成员的原顺序。
                previous_positions = {row['id']: index for index, row in enumerate(raw_items)}
                kept = sorted((row for row in items if row['id'] in previous_positions),
                              key=lambda row: previous_positions[row['id']])
                added = cs.chronological_items([row for row in items if row['id'] not in previous_positions], source_rows)
                items = kept + added
            refresh()
            completed_translations = {(row['id'], row['source_hash']) for row in state['rows']
                                      if row.get('text_zh', '').strip()}
            with video_info.gate:
                doc = cs._read(project)
                completed_ratings = {(row['id'], row['source_hash']) for row in cs._cached_ratings(
                    state['rows'], doc.get('rating_cache') or doc.get('filter', {}), prompt,
                    cs._context(project)).values() if row['id'] in scope}
            ready = len(items) - len(invalid_manual)
            shortfall = max(0, target - ready)
            summary = dict(target=target, selected=len(items), manual=manual_count,
                protected=len(protected), automatic=len(automatic), ready=ready,
                invalid_manual=sum(row['selection_origin'] == 'manual' for row in invalid_manual),
                invalid_protected=len(invalid_manual), shortfall=shortfall,
                manual_over_target=max(0, manual_count - target), protected_over_target=max(0, len(protected) - target),
                capacity=capacity, capacity_limited=capacity is not None and capacity < target,
                capacity_exceeded=capacity is not None and len(items) > capacity, excluded_automatic=len(excluded_auto),
                layout_checked=canvas is not None,
                new_translations=len(completed_translations - initial_translations.keys()),
                reused_translations=sum(1 for row in items if needs_translation
                    and row['display_text'] and initial_translations.get((row['id'], row['source_hash'])) == row['display_text']),
                new_ratings=len(completed_ratings - initial_ratings), reused_ratings=len(initial_ratings))
            if excluded_auto:
                warnings.append(f'本次已排除{len(excluded_auto)}条命中内容检查或不满足语言、文字长度和排版要求的候选，按评分递补；完整评论与译文仍保留。')
            if len(protected) > target:
                warnings.append(f'手动保留 {manual_count} 条，超过目标 {target} 条；未自动移除，请调整保留状态。')
            if invalid_manual:
                warnings.append(f'{len(invalid_manual)} 条保留评论仍需调整文字、翻译或核对来源；已保留，尚不能确认上屏。')
            if shortfall:
                warnings.append(f'本次已准备 {ready}/{target} 条可用评论；其余候选不足、待处理或不满足当前显示条件。')
            if stop_requests:
                warnings.append('翻译已按止损规则停止；未完成项待人工处理，不会在普通准备时反复提交。')
            with video_info.gate:
                job.update(status='partial' if shortfall or invalid_manual or summary['capacity_exceeded'] else 'done',
                           stage='done', selected=len(items), message='评论草稿已准备，请核对后统一确认保存',
                           updated_at=cs._now(), finished_at=cs._now(), summary=summary)
            state['prepare_job'] = cs.comment_diagnostics.snapshot(job)
            state['preparation'] = dict(draft=dict(items=items, settings=settings, excluded_ids=exclusions,
                                                 order_mode=order_mode), summary=summary,
                invalid_manual=invalid_manual, excluded_automatic=[dict(id=ident, reasons=found) for ident, found in excluded_auto.items()],
                warnings=list(dict.fromkeys(warnings)))
            return state
        except Exception as exc:
            with video_info.gate:
                code = getattr(exc, 'code', 'COMMENTS_INTERNAL')
                job.update(status='cancelled' if code in {'COMMENTS_CANCELLED', 'WORKFLOW_STOPPED'} else
                           'interrupted' if code in {'COMMENTS_INTERRUPTED', 'STEP_INTERRUPTED'} else 'failed',
                           error=getattr(exc, 'message', '评论准备失败，已完成评分和译文保留'), code=code,
                           updated_at=cs._now(), finished_at=cs._now())
            if isinstance(exc, DubForgeError):
                raise
            raise cs.CommentError('评论准备失败，已完成评分和译文保留；人工草稿未被覆盖', code='COMMENTS_INTERNAL') from exc
        finally:
            with cs._drained:
                cs._prepare_cancels.pop(uid, None)
                cs._drained.notify_all()
