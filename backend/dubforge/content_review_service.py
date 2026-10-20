"""项目审查报告、设置保存和发布前核对；依赖领域资料，不依赖HTTP。"""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from dubforge import configuration
from dubforge import content_review_decisions as decisions
from dubforge.content_review import REVIEW_VERSION, TEXT_ONLY_LIMITATION, blocked_comment, normalize_terms
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.content_redaction import ContentRedactionError, audio_ranges, parse_ranges, plan, receipt
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.fs import atomic_write_bytes

KEYS = ('content_review.enabled', 'content_review.mute_audio', 'content_review.audio_mode',
        'content_review.blocked_terms', 'content_review.blocked_ranges')


def report(uid, *, persist=False):
    project = crud.get_project(uid)
    if not project:
        raise ContentRedactionError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    data = configuration.resolve(uid)
    enabled = deps.cfg('content_review.enabled', True)
    terms = normalize_terms(deps.cfg('content_review.blocked_terms', ''))
    store = decisions.load(project)
    deps._content_review_decision_store = store
    result = dict(version=REVIEW_VERSION, rule_version=REVIEW_VERSION, enabled=enabled, mute_audio=deps.cfg('content_review.mute_audio', True),
                  audio_mode=deps.cfg('content_review.audio_mode', 'voice_only'),
                  blocked_terms=terms, blocked_ranges=deps.cfg('content_review.blocked_ranges', '') or '',
                  revision=data['revision'], configuration_fingerprint=data['fingerprint'],
                  inherited=not any(k in data['overrides'] for k in KEYS),
                  origin=' / '.join(dict.fromkeys(data['origins'].get(k, '全局默认') for k in KEYS)),
                  findings=[], original_findings=[], comment_findings=[], warnings=[TEXT_ONLY_LIMITATION],
                  decision_revision=str(store['revision']), ranges=[], effective_ranges=[], effective_audio_ranges=[],
                  checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds') if enabled else None,
                  coverage=dict(original=0, source=0, translated=0, comments=0),
                  summary=dict(lines=0, blocked_lines=0, blocked_comments=0), scope='text_only')
    if not enabled:
        result['warnings'].append('内容筛查已关闭，本次未执行本地文字规则检查。')
    try:
        current = plan(deps)
        result['summary'].update(lines=current['checked_lines'], blocked_lines=len(current['blocked_ids']))
        result['findings'] = current['findings']
        result['original_findings'] = current.get('original_findings', [])
        result['original_checked_lines'] = current.get('original_checked_lines', 0)
        result['coverage'].update(original=current.get('original_checked_text_lines', 0),
            source=current.get('source_checked_lines', 0), translated=current.get('translated_checked_lines', 0))
        result['warnings'].extend(current.get('warnings', []))
        result['ranges'] = current['ranges']
        result['effective_ranges'] = current['ranges']
        result['effective_audio_ranges'] = audio_ranges(deps)
        result['blocked_ids'] = current['blocked_ids']
        result['manual_ranges'] = parse_ranges(result['blocked_ranges'])
        result['receipt'] = receipt(deps)
        result['audio_processing'] = result['receipt']['audio_processing']
        if audio_reason := result['audio_processing'].get('fallback_reason'):
            result['warnings'].append(audio_reason)
        if enabled and not current['checked_lines']:
            result['warnings'].append('还没有可核对的整理稿；当前结果不能代表视频已经检查完成。')
        elif enabled and current.get('translated_checked_lines', 0) < current['checked_lines']:
            result['warnings'].append('当前译文尚未完整生成或无法按句对齐；本次结果只覆盖已有的可核对文字。')
    except DubForgeError as exc:
        result['warnings'].append(exc.message)
        result['blocker'] = exc.to_dict()
    if enabled and not result['mute_audio']:
        result['warnings'].append('已关闭音频静音：隐藏字幕不会改变原声或配音，请核对声音中的对应内容。')
    if enabled:
        from dubforge import comment_studio
        comment_view = comment_studio.view(uid)
        rows = comment_view.get('rows', [])
        originals = {row['id']: row for row in rows}
        seen = set()
        # 已保存上屏文字优先显示，仍同时核对采集原文；同一评论只统计一次。
        for row in [*(comment_view.get('overlay') or {}).get('items', []), *rows]:
            if row['id'] in seen:
                continue
            seen.add(row['id'])
            original = originals.get(row['id']) or {}
            candidate = {**original, **row, 'text_zh': original.get('text_zh', row.get('text_zh', ''))}
            finding = decisions.comment_finding(candidate, (enabled, terms), originals=(original,), store=store)
            if finding:
                result['comment_findings'].append(finding)
        result['coverage']['comments'] = len(seen)
        result['summary']['blocked_comments'] = sum(f['active'] for f in result['comment_findings'])
    findings = result['findings'] + result['original_findings'] + result['comment_findings']
    result['stale_decision_count'] = decisions.stale_count(store, findings) if enabled else 0
    result['unverified_decision_count'] = len(store['entries']) if not enabled else 0
    result['summary'].update(matched_findings=len(findings),
        blocked_original_lines=sum(f['active'] for f in result['original_findings']),
        pending_findings=sum(f['decision'] == 'pending' for f in findings),
        allowed_findings=sum(f['decision'] == 'allow' for f in findings),
        confirmed_findings=sum(f['decision'] == 'block' for f in findings))
    result['coverage']['complete_text'] = bool(enabled and result['coverage']['original'] and result['summary']['lines']
        and result['coverage']['source'] == result['summary']['lines']
        and result['coverage']['translated'] == result['summary']['lines'] and not result.get('blocker'))
    if persist:
        from dubforge import runtime_edit
        with runtime_edit.editing('project:' + uid, allow_owned=True, paths={'content_review'}):
            atomic_write_bytes(crud.project_dir_by_project(project) / 'content_review.json',
                               json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'))
    return result


def save_decision(uid, body):
    """编辑锁内重读证据与版本；旧页面不能覆盖另一页刚完成的复核。"""
    if (not isinstance(body, dict) or body.get('decision') not in {'allow', 'block', 'reset'}
            or not isinstance(body.get('key'), str) or not body['key']
            or not isinstance(body.get('evidence_fingerprint'), str)
            or not isinstance(body.get('decision_revision'), str)
            or not isinstance(body.get('reason', ''), str) or len(body.get('reason', '')) > 2000):
        raise decisions.ContentReviewDecisionError('逐条复核参数无效，请刷新结果后重试')
    from dubforge import runtime_edit
    with runtime_edit.editing('project:' + uid, paths={'content_review'}):
        current = report(uid)
        found = next((row for row in current['findings'] + current['original_findings'] + current['comment_findings']
                      if row['key'] == body['key']), None)
        if (body.get('revision') != current['revision']
                or body.get('configuration_fingerprint') != current['configuration_fingerprint']
                or body['decision_revision'] != current['decision_revision']
                or not found or found['evidence_fingerprint'] != body['evidence_fingerprint']):
            raise decisions.ContentReviewDecisionError('文字、规则或人工复核记录已变化，请刷新后重新核对',
                                                      code='CONTENT_REVIEW_DECISION_CONFLICT')
        project = crud.get_project(uid)
        store = decisions.load(project)
        if body['decision'] == 'reset':
            store['entries'].pop(body['key'], None)
        else:
            store['entries'][body['key']] = dict(decision=body['decision'],
                evidence_fingerprint=found['evidence_fingerprint'], reason=body.get('reason', '').strip(),
                decided_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        store['revision'] += 1
        decisions.persist(project, store)
        fresh = report(uid, persist=True)
        changed = set()
        if fresh['ranges'] != current['ranges']:
            changed.update(('subtitles', 'assemble'))
        saved_overlay = comment_studio_overlay(project)
        selected = {item['id'] for item in (saved_overlay or {}).get('items', [])}
        if found['key'].startswith('comment:') and found['id'] in selected and found['active'] != (body['decision'] != 'allow'):
            changed.add('assemble')
        for key in changed:
            step = crud.get_step(project.id, key)
            if step and step.status == 'done':
                step.status = 'stale'
                crud.save_step(step)
        if changed:
            crud.write_manifest(uid)
        return fresh


def comment_studio_overlay(project):
    from dubforge import comment_studio
    return comment_studio._read(project).get('overlay')


def save(uid, body):
    if not isinstance(body, dict):
        raise ContentRedactionError('内容审查设置格式无效')
    current = configuration.resolve(uid)
    if body.get('revision') != current['revision'] or body.get('configuration_fingerprint') != current['fingerprint']:
        raise ContentRedactionError('项目设置已变化，请刷新审查面板后重新保存', code='CONTENT_REVIEW_CONFLICT')
    if body.get('reset') is True:
        change = dict(reset=list(KEYS))
    else:
        if type(body.get('enabled')) is not bool or type(body.get('mute_audio')) is not bool:
            raise ContentRedactionError('请明确选择内容筛查和音频静音开关')
        terms = normalize_terms(body.get('blocked_terms', ''))
        ranges = body.get('blocked_ranges', '')
        parse_ranges(ranges)
        # 老页面保存时沿用当前模式，避免缺少新字段把用户已有选择改回默认。
        audio_mode = body.get('audio_mode', current['values'].get('content_review.audio_mode', 'voice_only'))
        if audio_mode not in ('voice_only', 'all_audio'):
            raise ContentRedactionError('请选择有效的声音屏蔽方式')
        change = dict(updates=dict(zip(KEYS, [body['enabled'], body['mute_audio'], audio_mode,
                                            '\n'.join(terms), ranges], strict=True)))
    configuration.save('project:' + uid, dict(revision=current['revision'], fingerprint=current['fingerprint'], **change))
    return report(uid, persist=True)


def _partial_history(deps, snapshot, *, depth=0, seen=frozenset()):
    """按成功快照的不可变引用还原局部基片，不借用当前工作稿或源视频。"""
    value = dict(snapshot) if isinstance(snapshot, dict) else snapshot
    if not isinstance(value, dict) or not value.get('partial') or depth > 16:
        return value
    embedded = value.get('partial_plan')
    if isinstance(embedded, dict):
        embedded = value['partial_plan'] = dict(embedded)
        # 旧成功记录曾直接内嵌基片；仍以已绑定文件哈希的历史记录为证据。
        if isinstance(embedded.get('base_version'), dict):
            embedded['base_version'] = _partial_history(deps, embedded['base_version'], depth=depth+1, seen=seen)
        return value
    ref = value.get('partial')
    identifier = ref.get('plan_id') if isinstance(ref, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier) or identifier in seen:
        return value
    try:
        from dubforge.pipeline.assembly_partial import _digest, _checked_hash
        root = deps.root.resolve()
        path = root / 'output' / '.partial-plans' / (identifier + '.json')
        if not path.resolve().is_relative_to(root):
            return value
        cache = getattr(deps, '_partial_review_plans', None)
        if cache is None:
            cache = deps._partial_review_plans = {}
        if identifier not in cache:
            cache[identifier] = json.loads(path.read_text(encoding='utf-8'))
        saved = cache[identifier]
        if (not isinstance(saved, dict) or saved.get('schema') != 1 or saved.get('id') != identifier
                or saved.get('project_uid') != deps.project.uid
                or saved.get('fingerprint') != _digest({k: v for k, v in saved.items() if k != 'fingerprint'})
                or not isinstance(saved.get('base_version'), dict)
                or not isinstance(saved.get('base_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', saved['base_sha256'])):
            return value
        ranges = saved.get('ranges')
        if (not isinstance(ranges, list) or not ranges or any(not isinstance(row, dict)
                or any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key]) for key in ('start', 'end'))
                or not 0 <= row['start'] < row['end'] for row in ranges)):
            return value
        baseline = root / 'output' / '.partial-baselines' / (saved['base_sha256'] + '.mp4')
        if (not baseline.resolve().is_relative_to(root)
                or Path(saved['base_path']).resolve() != baseline.resolve() or not baseline.is_file()):
            return value
        stat = baseline.stat()
        if (stat.st_size != saved.get('base_size')
                or _checked_hash(str(baseline), stat.st_size, stat.st_mtime_ns) != saved['base_sha256']):
            return value
        received = value.get('content_review_receipt') or {}
        if (not isinstance(received, dict) or received.get('base_sha256') != saved['base_sha256']
                or received.get('ranges') != ranges):
            return value
        value['partial_plan'] = {**saved, 'base_version': _partial_history(
            deps, saved['base_version'], depth=depth+1, seen=seen | {identifier})}
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        # 缺失、跨项目、损坏或无法证明的基片保持未还原，后续沿用关闭式门禁。
        pass
    return value


def _artifact_snapshot(artifact, saved, deps=None):
    """只接受精确绑定此文件哈希的生成快照，不拿当前评论方案猜历史画面。"""
    snapshot, had_comments = None, False
    for record in saved.values():
        if record.artifact_id != artifact.id:
            continue
        try:
            candidate = json.loads(record.snapshot)
        except (ValueError, TypeError):
            continue
        if not isinstance(candidate, dict):
            continue
        had_comments |= bool(candidate.get('include_comments') or candidate.get('comment_overlay'))
        if record.artifact_hash == artifact.sha256:
            snapshot = candidate
    return (_partial_history(deps, snapshot) if deps is not None and snapshot is not None else snapshot), had_comments


def _comment_issue(artifact, snapshot, terms, had_comments=False, *, store=None):
    from dubforge import comment_studio
    from dubforge.pipeline.assembly_outputs import OUTPUT_VARIANTS, final_variant
    variant = final_variant(artifact.name) if artifact.step_key == 'assemble' else None
    if variant is None and artifact.step_key == 'assemble':
        # 已升级为独立版本的旧组合沿用这个实际文件名，不使用任意自定义版本名猜评论。
        legacy_name = artifact.name.removeprefix('final_version_legacy-').removesuffix('.mp4')
        if artifact.name == f'final_version_legacy-{legacy_name}.mp4' and legacy_name in OUTPUT_VARIANTS:
            variant = legacy_name
    legacy_comments = bool(variant and variant.endswith('_comments'))
    overlay = snapshot.get('comment_overlay') if snapshot else None
    if overlay is not None:
        if not isinstance(overlay, dict) or not isinstance(overlay.get('items'), list):
            return '此评论成片缺少可核对的生成快照，请重新生成后再上传'
        for item in overlay['items']:
            if not isinstance(item, dict):
                return '此评论成片缺少可核对的生成快照，请重新生成后再上传'
            if comment_studio.review_reasons(item, (True, terms, store or dict(entries={}))):
                return '此成片已烧录的评论命中当前屏蔽规则，请重新生成后再上传；修改当前评论方案不会改变旧文件'
    elif (snapshot.get('include_comments', legacy_comments) if snapshot is not None else legacy_comments or had_comments):
        return '此评论成片缺少可核对的生成快照，请重新生成后再上传'
    return ''


def _artifact_issue(deps, artifact, current, saved):
    snapshot, had_comments = _artifact_snapshot(artifact, saved, deps)
    terms = normalize_terms(deps.cfg('content_review.blocked_terms', ''))
    received = (snapshot or {}).get('content_review_receipt', {})
    if (snapshot or {}).get('partial'):
        from dubforge.pipeline.partial_review import comment_issue, covers_current_ranges
        if not isinstance(received, dict) or received.get('scope') != 'partial':
            return '局部成片缺少范围审查记录，请重新生成此修订版本。'
        if issue := comment_issue(artifact, snapshot, terms, decisions.load(deps.project), _comment_issue):
            return issue
        if current['ranges'] and not covers_current_ranges(received, receipt(deps)):
            return '局部成片仍有命中时段未被此次替换或基片原处理覆盖；请补充修订范围，需处理声音时也勾选替换声音。'
        return ''
    if issue := _comment_issue(artifact, snapshot, terms, had_comments, store=decisions.load(deps.project)):
        return issue
    if current['ranges'] and (snapshot or {}).get('content_review_receipt', {}) != receipt(deps):
        return '选中成片包含需处理的文字时段，尚未按当前审查设置重新生成；请返回合成成片生成后再选用'
    return ''


def assert_publishable(project, artifact):
    """核对语音时段与实际烧录评论；旧上传不因当前页面勾选自动变安全。"""
    deps = Dependencies(project)
    if not deps.cfg('content_review.enabled', True):
        return
    current = plan(deps)
    from dubforge.pipeline.assembly_versions import results
    if issue := _artifact_issue(deps, artifact, current, results(project.uid)):
        raise ContentRedactionError(issue, code='CONTENT_REVIEW_OUTPUT_STALE')


def assert_snapshot(snapshot):
    project = crud.get_project(snapshot.get('project_uid'))
    if not project:
        raise ContentRedactionError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    if not deps.cfg('content_review.enabled', True):
        return
    terms = normalize_terms(deps.cfg('content_review.blocked_terms', ''))
    settings = snapshot.get('settings', {})
    for name, label in (('title', '标题'), ('description', '简介'), ('dynamic', '动态'), ('tags', '标签')):
        value = settings.get(name, '')
        if isinstance(value, list):
            value = ' '.join(value)
        if blocked_comment(value, '', terms):
            raise ContentRedactionError(f'投稿{label}命中屏蔽规则，请核对并修改后再投稿', code='CONTENT_REVIEW_METADATA')
    for part in snapshot.get('parts', []):
        if blocked_comment(part.get('title', ''), '', terms):
            raise ContentRedactionError('分P标题命中屏蔽规则，请核对后再投稿', code='CONTENT_REVIEW_METADATA')
        from dubforge.domain.models import Artifact
        with crud.session() as session:
            artifact = session.get(Artifact, part['artifact_id'])
        if not artifact or artifact.sha256 != part['sha256']:
            raise ContentRedactionError('已确认的成片索引变化，请复制新草稿并重新选择文件', code='CONTENT_REVIEW_OUTPUT_STALE')
        assert_publishable(project, artifact)


def candidate_states(project, artifacts):
    """候选清单只读提示；逐文件有效性不冒充平台审核结论。"""
    deps = Dependencies(project)
    if not deps.cfg('content_review.enabled', True):
        return {a.id: dict(review_ready=None, review_status='审查关闭', review_reason='上传前请核对字幕、声音与画面') for a in artifacts}
    try:
        current = plan(deps)
    except DubForgeError as exc:
        return {a.id: dict(review_ready=False, review_status='无法核对文字', review_reason=exc.message) for a in artifacts}
    from dubforge.pipeline.assembly_versions import results
    saved = results(project.uid)
    result = {}
    for artifact in artifacts:
        if issue := _artifact_issue(deps, artifact, current, saved):
            result[artifact.id] = dict(review_ready=False, review_status='需审查修订', review_reason=issue)
        elif current['ranges']:
            audio = receipt(deps)['audio_processing']
            audio_status = ('语音已屏蔽·保留分离背景' if audio['effective_mode'] == 'voice_only'
                            else '命中时段已全部静音')
            result[artifact.id] = dict(review_ready=True, review_status='已按当前规则处理'
                + '·' + audio_status if deps.cfg('content_review.mute_audio', True)
                else '字幕已处理·声音未静音',
                review_reason=' '.join(filter(None, (audio.get('fallback_reason'), TEXT_ONLY_LIMITATION))))
        else:
            matching = bool(current.get('findings') or current.get('original_findings'))
            snapshot, _ = _artifact_snapshot(artifact, saved, deps)
            overlay = (snapshot or {}).get('comment_overlay') or {}
            matching |= any(decisions.comment_finding(item, (True, normalize_terms(deps.cfg('content_review.blocked_terms', ''))),
                store=decisions.load(project)) for item in overlay.get('items', []))
            result[artifact.id] = dict(review_ready=None, review_status='命中已标记误报' if matching else
                '未命中本地规则' if current['checked_lines'] or current.get('original_checked_lines') else '缺少审查文字',
                review_reason=TEXT_ONLY_LIMITATION)
        snapshot, _ = _artifact_snapshot(artifact, saved, deps)
        if (snapshot or {}).get('partial'):
            item = result[artifact.id]
            item['review_scope'] = 'partial'
            if item['review_ready'] is not False:
                item['review_status'] = ('局部修订·已核对当前命中范围' if item['review_ready']
                                         else '局部修订·范围外沿用基片')
            item['review_reason'] += ' 局部范围采用当前审查，范围外继承基片原记录，未重新审查全片。'
    return result
