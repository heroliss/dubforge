"""项目审查报告、设置保存和发布前核对；依赖领域资料，不依赖HTTP。"""
import json
from datetime import datetime, timezone

from dubforge import configuration
from dubforge.content_review import REVIEW_VERSION, TEXT_ONLY_LIMITATION, blocked_comment, normalize_terms
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.content_redaction import ContentRedactionError, parse_ranges, plan, receipt
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.fs import atomic_write_bytes

KEYS = ('content_review.enabled', 'content_review.mute_audio', 'content_review.blocked_terms', 'content_review.blocked_ranges')


def report(uid, *, persist=False):
    project = crud.get_project(uid)
    if not project:
        raise ContentRedactionError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    data = configuration.resolve(uid)
    enabled = deps.cfg('content_review.enabled', True)
    terms = normalize_terms(deps.cfg('content_review.blocked_terms', ''))
    result = dict(version=REVIEW_VERSION, enabled=enabled, mute_audio=deps.cfg('content_review.mute_audio', True),
                  blocked_terms=terms, blocked_ranges=deps.cfg('content_review.blocked_ranges', '') or '',
                  revision=data['revision'], configuration_fingerprint=data['fingerprint'],
                  inherited=not any(k in data['overrides'] for k in KEYS),
                  origin=' / '.join(dict.fromkeys(data['origins'].get(k, '全局默认') for k in KEYS)),
                  findings=[], comment_findings=[], warnings=[TEXT_ONLY_LIMITATION],
                  summary=dict(lines=0, blocked_lines=0, blocked_comments=0), scope='text_only')
    try:
        current = plan(deps)
        result['summary'].update(lines=current['checked_lines'], blocked_lines=len(current['blocked_ids']))
        result['findings'] = current['findings']
        result['original_findings'] = current.get('original_findings', [])
        result['original_checked_lines'] = current.get('original_checked_lines', 0)
        result['warnings'].extend(current.get('warnings', []))
        result['ranges'] = current['ranges']
        result['receipt'] = receipt(deps)
        if enabled and not current['checked_lines']:
            result['warnings'].append('还没有可核对的整理稿；当前结果不能代表视频已经检查完成。')
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
            reasons = list(dict.fromkeys([*row.get('review_reasons', []),
                *comment_studio.review_reasons(row, (enabled, terms), originals=(originals.get(row['id']),))]))
            if reasons:
                if row['id'] in seen:
                    continue
                seen.add(row['id'])
                result['comment_findings'].append(dict(id=row['id'], ids=[row['id']],
                    source_text=row.get('source_text') or row.get('text', ''),
                    text=row.get('display_text') or row.get('text_zh', ''), reason='；'.join(reasons), rule='comment'))
        result['summary']['blocked_comments'] = len(result['comment_findings'])
    if persist:
        result['checked_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        atomic_write_bytes(crud.project_dir_by_project(project) / 'content_review.json',
                           json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'))
    return result


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
        change = dict(updates=dict(zip(KEYS, [body['enabled'], body['mute_audio'], '\n'.join(terms), ranges], strict=True)))
    configuration.save('project:' + uid, dict(revision=current['revision'], fingerprint=current['fingerprint'], **change))
    return report(uid, persist=True)


def _artifact_snapshot(artifact, saved):
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
    return snapshot, had_comments


def _comment_issue(artifact, snapshot, terms, had_comments=False):
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
            if comment_studio.review_reasons(item, (True, terms)):
                return '此成片已烧录的评论命中当前屏蔽规则，请重新生成后再上传；修改当前评论方案不会改变旧文件'
    elif (snapshot.get('include_comments', legacy_comments) if snapshot is not None else legacy_comments or had_comments):
        return '此评论成片缺少可核对的生成快照，请重新生成后再上传'
    return ''


def _artifact_issue(deps, artifact, current, saved):
    snapshot, had_comments = _artifact_snapshot(artifact, saved)
    terms = normalize_terms(deps.cfg('content_review.blocked_terms', ''))
    if issue := _comment_issue(artifact, snapshot, terms, had_comments):
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
            result[artifact.id] = dict(review_ready=True, review_status='已按当前规则处理'
                if deps.cfg('content_review.mute_audio', True) else '字幕已处理·声音未静音', review_reason=TEXT_ONLY_LIMITATION)
        else:
            result[artifact.id] = dict(review_ready=None, review_status='未命中本地规则'
                if current['checked_lines'] else '缺少审查文字', review_reason=TEXT_ONLY_LIMITATION)
    return result
