"""从完整投稿快照准备审查修订新稿；只读核对与本地草稿，不调用平台。"""
import copy
import json
import re
import uuid
from pathlib import Path

from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions as versions, review_render
from dubforge.pipeline.dependencies import Dependencies
from dubforge.publishing import service as pub
from dubforge.publishing.models import Publication, PublishAccount


def revision_metadata(project, artifacts, *, verify_hash=False):
    """候选文件保留可核查的来源身份；不按文件名推断修订关系。"""
    deps = Dependencies(project)
    saved = versions.results(project.uid)
    try:
        definitions = {v['id']: v for v in versions.catalog(project.uid)['versions']}
    except DubForgeError:
        definitions = {}
    by_id = {a.id: a for a in artifacts}
    result = {a.id: dict(is_review_revision=False) for a in artifacts}
    for record in saved.values():
        art = by_id.get(record.artifact_id)
        if not art:
            continue
        try:
            snapshot = json.loads(record.snapshot)
        except (ValueError, TypeError):
            continue
        info = snapshot.get('review_render') if isinstance(snapshot, dict) else None
        if not isinstance(info, dict):
            continue
        definition = definitions.get(record.version_id)
        encoder = info.get('encoder')
        try:
            current = bool(art.sha256 == record.artifact_hash and isinstance(encoder, dict)
                and definition and review_render.is_current(deps,
                versions.resolve(deps, definition), record=record, verify_hash=verify_hash))
        except (DubForgeError, ValueError, TypeError, KeyError, AttributeError):
            current = False
        result[art.id] = dict(is_review_revision=True,
            review_source_artifact_id=info.get('source_artifact_id'), review_revision_current=current,
            review_revision_created_at=info.get('created_at', ''),
            review_encoder=encoder.get('name', '') if isinstance(encoder, dict) else '',
            review_note=info.get('note', '基于已有文字与音轨的审查修订'),
            review_revision_reason='' if current else '原视频、已有成片、文字、评论或设置已变化；请重新生成审查修订版')
    return result


def assert_revision_current(project, artifact, *, verify_hash=False):
    state = revision_metadata(project, [artifact], verify_hash=verify_hash).get(artifact.id, {})
    if state.get('is_review_revision') and not state['review_revision_current']:
        raise pub.PublishError(state['review_revision_reason'], code='CONTENT_REVIEW_OUTPUT_STALE')


def assert_snapshot_current(snapshot):
    """确认后到真正上传、投稿之间也不得复用已失效的修订文字或评论。"""
    project = pub._project(snapshot['project_uid'])
    with Session(get_engine()) as session:
        artifacts = [session.get(Artifact, part['artifact_id']) for part in snapshot['parts']]
    # 文件本身在上传循环独立验哈希；这里验证修订制作时冻结的完整输入链。
    states = revision_metadata(project, [a for a in artifacts if a], verify_hash=True)
    for art in artifacts:
        state = states.get(art.id, {}) if art else {}
        if state.get('is_review_revision') and not state['review_revision_current']:
            raise pub.PublishError(state['review_revision_reason'], code='CONTENT_REVIEW_OUTPUT_STALE')


def _source(session, key):
    from dubforge.publishing.studio import publication_available
    from dubforge.workflows import check_idle
    publication_available(session, key)
    row = pub._get(session, Publication, key)
    check_idle(row.project_uid)
    if row.status in pub.ACTIVE or row.status == 'unknown' or key in pub._running:
        raise pub.PublishError('原投稿正在执行或结果尚未确认，请先停止操作并核对原BV',
                               code='PUBLISH_REVIEW_SOURCE_BUSY')
    try:
        snapshot, receipt = json.loads(row.snapshot), json.loads(row.receipt)
    except (ValueError, TypeError):
        snapshot, receipt = {}, {}
    if not isinstance(snapshot, dict) or not isinstance(receipt, dict):
        snapshot, receipt = {}, {}
    bvid = receipt.get('bvid')
    if (not isinstance(bvid, str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid)
            or not isinstance(snapshot.get('parts'), list) or not 1 <= len(snapshot['parts']) <= 100
            or any(not isinstance(part, dict) for part in snapshot['parts'])
            or not isinstance(snapshot.get('settings'), dict)):
        raise pub.PublishError('原记录缺少完整投稿快照或BV回执，不能还原分P及已确认资料',
                               code='PUBLISH_REVIEW_SOURCE_INVALID')
    if snapshot.get('update_target') or receipt.get('update_kind') == 'append':
        raise pub.PublishError('此记录只包含追加分P的局部上传清单，不能作为完整原稿创建修订新稿',
                               code='PUBLISH_REVIEW_SOURCE_INVALID')
    return row, snapshot, bvid


def _plan(session, key):
    original, snapshot, bvid = _source(session, key)
    project = pub._project(original.project_uid)
    root = crud.project_dir_by_project(project).resolve()
    candidates = pub.candidates(project.uid)['files']
    rows = {a.id: a for a in session.exec(select(Artifact).where(Artifact.project_id == project.id)).all()}
    saved = versions.results(project.uid)
    by_artifact = {r.artifact_id: r for r in saved.values()}
    parts, problems, seen = [], [], set()
    for index, source in enumerate(snapshot['parts'], 1):
        ident, title, expected = source.get('artifact_id'), source.get('title'), source.get('sha256')
        art = rows.get(ident)
        if (type(ident) is not int or ident in seen or not isinstance(title, str) or not title.strip()
                or not art or art.sha256 != expected):
            problems.append(dict(index=index, title=title, reason='原分P身份或冻结哈希已变化，无法精确匹配修订来源'))
            continue
        seen.add(ident)
        matches = []
        for candidate in candidates:
            if not candidate.get('is_review_revision') or candidate.get('review_source_artifact_id') != ident:
                continue
            if not candidate.get('review_revision_current') or not candidate.get('exists') or candidate.get('review_ready') is False:
                continue
            record = by_artifact.get(candidate['id'])
            info = json.loads(record.snapshot).get('review_render', {}) if record else {}
            inputs = info.get('recipe', {}).get('inputs', [])
            if not any(item.get('id') == ident and item.get('sha256') == expected for item in inputs):
                continue
            revised = rows.get(candidate['id'])
            path = pub._eligible_file(revised, project, root) if revised else None
            if (not path or not path.is_file() or path.stat().st_size != revised.size
                    or pub._file_hash(path) != revised.sha256):
                continue
            from dubforge.content_review_service import assert_publishable
            assert_publishable(project, revised)
            assert_revision_current(project, revised, verify_hash=True)
            matches.append(candidate)
        if len(matches) != 1:
            problems.append(dict(index=index, title=title, reason='没有与此原分P对应的有效审查修订文件'
                if not matches else '存在多个仍有效的审查修订文件，请明确保留一个对应版本后重试'))
            continue
        candidate = matches[0]
        parts.append(dict(index=index, title=title, source_artifact_id=ident, source_name=art.name,
            source_sha256=expected, artifact_id=candidate['id'], name=candidate['name'],
            sha256=candidate['sha256'], size=candidate['size'],
            version_id=candidate.get('version_id'), version_name=candidate.get('version_name', candidate['name'])))
    if problems:
        raise pub.PublishError('无法完整匹配原投稿分P，未创建草稿；请先核对审查修订文件',
                               code='PUBLISH_REVIEW_MAPPING', detail={'parts': problems})
    account = pub._get(session, PublishAccount, snapshot.get('account_id'))
    draft = json.loads(original.draft)
    cover_id = draft.get('cover_id') or Path(snapshot.get('cover_path', '')).name
    cover = pub._cover_path(cover_id)
    if not snapshot.get('cover_sha256') or pub._file_hash(cover) != snapshot['cover_sha256']:
        raise pub.PublishError('原投稿封面与冻结快照不一致，请先核对保留的封面', code='PUBLISH_REVIEW_SOURCE_INVALID')
    result = dict(mode='new_submission', project_uid=project.uid,
        source=dict(publication_id=key, revision=original.revision, status=original.status,
                    bvid=bvid, url=f'https://www.bilibili.com/video/{bvid}'),
        account_id=account.id, account_name=account.name, cover_id=cover_id,
        settings=pub.validate_values(snapshot['settings']), parts=parts,
        notices=['这会创建新投稿并获得新BV；原稿件、审核记录和已发布快照保持保留。',
                 '创建草稿只保存到本地；上传与正式投稿仍需执行发布动作。',
                 '本版创作管理可修改文案、排序或追加分P；替换原BV的视频请前往B站创作中心。',
                 '审查修订沿用已有文字与混音，需要另行核对画面，不能保证平台审核通过。'])
    result['fingerprint'] = pub._hash(result)
    return result


def plan(key):
    with pub._lock, Session(get_engine()) as session:
        return _plan(session, key)


def synchronize_source(draft, parts, project_uid):
    """来源由服务端保留；手动改P后展示真实清单，不能继续冒称原预览映射。"""
    source = draft.get('review_source')
    if not isinstance(source, dict):
        return
    expected = source.get('prepared_parts', [])
    artifacts = list(crud.latest_artifacts(crud.get_project(project_uid).id, ['assemble', 'download']).values())
    metadata = revision_metadata(pub._project(project_uid), artifacts)
    mappings = []
    for index, part in enumerate(parts, 1):
        meta = metadata.get(part['artifact_id'], {})
        previous = expected[index-1] if index <= len(expected) else {}
        mappings.append(dict(index=index, title=part['title'], artifact_id=part['artifact_id'],
            source_artifact_id=meta.get('review_source_artifact_id'),
            matches_original_part=bool(meta.get('is_review_revision')
                and meta.get('review_source_artifact_id') == previous.get('source_artifact_id'))))
    source['parts'] = mappings
    source['selection_changed'] = [(p.get('artifact_id'), p.get('title')) for p in expected] != [
        (p['artifact_id'], p['title']) for p in parts]


def create(key, body):
    if not isinstance(body, dict) or set(body) != {'revision', 'fingerprint', 'request_id'}:
        raise pub.PublishError('请从修订预览创建草稿，并提供版本、指纹和请求编号')
    try:
        request_id = str(uuid.UUID(body['request_id']))
    except (ValueError, TypeError, AttributeError):
        raise pub.PublishError('草稿请求编号无效，请重新打开修订预览') from None
    with pub._lock, Session(get_engine()) as session, pub._publication_edit(session, key):
        original, _, _ = _source(session, key)
        pub._revision(original, body['revision'])
        # 已消费请求先返回原草稿；用户后续人工编辑不被重复点击覆盖。
        existing_rows = session.exec(select(Publication).where(Publication.project_uid == original.project_uid)).all()
        for row in existing_rows:
            source = json.loads(row.draft).get('review_source', {})
            if source.get('source_publication_id') == key and source.get('request_id') == request_id:
                if source.get('plan_fingerprint') != body['fingerprint']:
                    raise pub.PublishError('同一请求对应的修订预览已经变化，请重新核对', code='PUBLISH_CONFLICT')
                return {**pub.view(row.id), 'reused': True}
        preview = _plan(session, key)
        if preview['fingerprint'] != body['fingerprint']:
            raise pub.PublishError('原投稿或修订文件已变化，请重新打开修订预览', code='PUBLISH_CONFLICT')
        ident = uuid.uuid5(uuid.NAMESPACE_URL, f'dubforge:review-draft:{key}:{preview["fingerprint"]}').hex
        existing = session.get(Publication, ident)
        if existing:
            return {**pub.view(ident), 'reused': True}
        draft = dict(account_id=preview['account_id'], template_id='', cover_id=preview['cover_id'],
            parts=[dict(artifact_id=p['artifact_id'], title=p['title']) for p in preview['parts']],
            overrides=copy.deepcopy(preview['settings']), content={},
            review_source=dict(mode='new_submission', source_publication_id=key,
                source_bvid=preview['source']['bvid'], source_revision=preview['source']['revision'],
                source_url=preview['source']['url'], plan_fingerprint=preview['fingerprint'], request_id=request_id,
                prepared_parts=copy.deepcopy(preview['parts'])))
        synchronize_source(draft, draft['parts'], original.project_uid)
        row = Publication(id=ident, project_uid=original.project_uid, draft=pub._json(draft))
        session.add(row); session.commit()
    return {**pub.view(ident), 'reused': False}
