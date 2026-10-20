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


def _review_sidecar(project, artifact):
    """读取修订成片旁的来源配方。

    审查修订成片在早期版本中可以由普通 ``assemble`` 步骤重新生成。
    该步骤会更新 AssemblyResult 的普通版本快照，却不会把历史
    ``review_render`` 元数据一并写回数据库；review-render 本身留下的
    ``*.review.json`` 是这类文件唯一可核验的来源记录。这里只接受带有
    完整来源身份、输入清单和编码器的 sidecar，普通成片不会因为文件名
    看起来像 review 而被当成修订稿。
    """
    try:
        root = crud.project_dir_by_project(project).resolve()
        path = Path(artifact.rel_path)
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != '.mp4':
            return None
        sidecar = path.with_suffix('.review.json')
        raw = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get('schema') not in {'review-render-v1', 'review-render-v2'} \
            or type(raw.get('source_artifact_id')) is not int \
            or not isinstance(raw.get('fingerprint'), str) \
            or not isinstance(raw.get('recipe'), dict):
        return None
    recipe = raw['recipe']
    if (recipe.get('source_artifact_id') != raw['source_artifact_id']
            or recipe.get('schema') != raw['schema']
            or versions.digest(recipe) != raw['fingerprint']
            or recipe.get('encoder') != raw.get('encoder')
            or not isinstance(recipe.get('inputs'), list)
            or not isinstance(recipe.get('encoder'), dict)
            or not isinstance(recipe.get('review_receipt'), dict)):
        return None
    return raw


def _sidecar_current(deps, project, artifact, record, definition, info, *, verify_hash=False):
    """核验普通 assemble 重生成的审查修订输出。

    普通步骤登记的 fingerprint 证明当前 review 版本已按当前文字、审查
    规则和输入重新生成；sidecar 再证明它仍源自原始成片且所有冻结输入
    没有被替换。两层校验都通过才允许绕过旧输出文件哈希。
    """
    try:
        if not versions.is_current(deps, definition, verify_hash=verify_hash):
            return False
        if info['recipe'].get('definition') != {key: definition[key] for key in
                ('dubbing', 'burn_subtitles', 'include_comments')}:
            return False
        source_id = info['source_artifact_id']
        source = next((a for a in deps.arts.values() if a.id == source_id), None)
        if not source or source.step_key != 'assemble' or source.id == artifact.id:
            return False
        source_record = next((r for r in versions.results(project.uid).values()
                              if r.artifact_id == source.id), None)
        if not source_record or source_record.artifact_hash != source.sha256:
            return False
        inputs = info['recipe']['inputs']
        if not inputs or any(not isinstance(item, dict) for item in inputs):
            return False
        # A review render must remain tied to both the current assembled source
        # and the original downloaded video.  Checking only the source output
        # lets a stale or substituted video pass after a normal assemble rerun.
        video_id = info['recipe'].get('video_artifact_id')
        if type(video_id) is not int or video_id == source.id:
            return False
        video = next((a for a in deps.arts.values() if a.id == video_id), None)
        if not video or video.step_key != 'download':
            return False
        keys = [(item.get('step'), item.get('name')) for item in inputs]
        if len(set(keys)) != len(keys) or len({item.get('id') for item in inputs}) != len(inputs):
            return False
        guards = {key: item for key, item in zip(keys, inputs)}
        source_guard = guards.get(('assemble', source.name))
        if not source_guard or source_guard.get('id') != source.id \
                or source_guard.get('sha256') != source.sha256:
            return False
        video_guard = guards.get(('download', video.name))
        if (not video_guard or video_guard.get('id') != video.id
                or video_guard.get('sha256') != video.sha256
                or video_guard.get('size') != video.size):
            return False
        for item in inputs:
            if not isinstance(item, dict) or type(item.get('id')) is not int:
                return False
            current = next((a for a in deps.arts.values() if a.id == item['id']), None)
            if (not current or current.sha256 != item.get('sha256')
                    or current.size != item.get('size') or current.step_key != item.get('step')
                    or current.name != item.get('name')):
                return False
            review_render._file(deps, current, verify_hash=verify_hash)
        return True
    except (DubForgeError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


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
        sidecar = None if isinstance(info, dict) else _review_sidecar(project, art)
        if sidecar:
            info = sidecar
        if not isinstance(info, dict):
            continue
        definition = definitions.get(record.version_id)
        encoder = info.get('encoder')
        try:
            if art.sha256 != record.artifact_hash:
                state = dict(current=False, reasons=[dict(code='REVIEW_OUTPUT_CHANGED', message='修订文件的登记身份已变化')])
            elif not isinstance(encoder, dict):
                state = dict(current=False, reasons=[dict(code='REVIEW_HISTORY_INVALID', message='修订缺少可核验的编码记录')])
            elif not definition:
                state = dict(current=False, reasons=[dict(code='REVIEW_VERSION_MISSING', message='此修订对应的成片版本已移除')])
            elif sidecar:
                from dubforge import comment_studio
                overlay = comment_studio.effective_overlay(project.uid, comment_studio._read(project).get('overlay'))
                current = _sidecar_current(deps, project, art, record,
                                            versions.resolve(deps, definition, comment_plan=overlay), info,
                                            verify_hash=verify_hash)
                state = dict(current=current, reasons=[] if current else
                             [dict(code='REVIEW_HISTORY_INVALID', message='修订来源或当前成片版本无法核验')])
            else:
                state = review_render.revision_state(deps,
                    versions.resolve(deps, definition), record=record, verify_hash=verify_hash)
        except (DubForgeError, ValueError, TypeError, KeyError, AttributeError):
            state = dict(current=False, reasons=[dict(code='REVIEW_HISTORY_INVALID', message='修订记录或当前版本设置无法核验')])
        current, reasons = state['current'], state['reasons']
        result[art.id] = dict(is_review_revision=True,
            review_source_artifact_id=info.get('source_artifact_id'), review_revision_current=current,
            review_revision_created_at=info.get('created_at', ''),
            review_encoder=encoder.get('name', '') if isinstance(encoder, dict) else '',
            review_note=info.get('note', '基于已有文字与音轨的审查修订'),
            review_revision_reasons=reasons,
            review_revision_reason='' if current else '；'.join(r['message'] for r in reasons[:3]) + '；请重新生成成片')
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
        source_info = None
        record = by_artifact.get(ident)
        if record:
            try:
                source_info = json.loads(record.snapshot).get('review_render')
            except (ValueError, TypeError):
                pass
        if not isinstance(source_info, dict):
            source_info = _review_sidecar(project, art) if art else None
        source_identity, source_sha = ident, expected
        # 原稿本身可能已经是一次审查修订稿。普通 assemble 重生成该版本
        # 会替换同一个 Artifact 的文件哈希；此时以 sidecar 中冻结的底层
        # 原成片作为来源身份，允许输出哈希更新，但不放宽普通原稿校验。
        if source_info:
            source_identity = source_info.get('source_artifact_id')
            guards = source_info.get('recipe', {}).get('inputs', [])
            source_sha = next((item.get('sha256') for item in guards
                               if isinstance(item, dict) and item.get('id') == source_identity), None)
            source_art = rows.get(source_identity)
            source_state = revision_metadata(project, [art], verify_hash=True).get(ident, {}) if art else {}
            if (not source_state.get('review_revision_current') or not source_art
                    or source_art.sha256 != source_sha):
                problems.append(dict(index=index, title=title, reason='原审查修订分P的来源或当前文件无法核验'))
                continue
        if (type(ident) is not int or ident in seen or not isinstance(title, str) or not title.strip()
                or not art or (art.sha256 != expected and not source_info)
                or (source_info and (type(source_identity) is not int or source_sha is None))):
            problems.append(dict(index=index, title=title, reason='原分P身份或冻结哈希已变化，无法精确匹配修订来源'))
            continue
        seen.add(ident)
        matches = []
        for candidate in candidates:
            if not candidate.get('is_review_revision') or candidate.get('review_source_artifact_id') != source_identity:
                continue
            if not candidate.get('review_revision_current') or not candidate.get('exists') or candidate.get('review_ready') is False:
                continue
            record = by_artifact.get(candidate['id'])
            revised = rows.get(candidate['id'])
            info = {}
            if record:
                try:
                    info = json.loads(record.snapshot).get('review_render', {})
                except (ValueError, TypeError):
                    info = {}
            if not isinstance(info, dict) or not info:
                info = _review_sidecar(project, revised) if revised else None
            info = info or {}
            inputs = info.get('recipe', {}).get('inputs', [])
            if not info.get('schema', '').startswith('review-render-'):
                continue
            if not any(isinstance(item, dict) and item.get('id') == source_identity
                       and item.get('sha256') == source_sha for item in inputs):
                continue
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
        source_art = rows.get(source_identity)
        parts.append(dict(index=index, title=title, source_artifact_id=source_identity,
            source_name=source_art.name if source_art else art.name,
            source_sha256=source_sha, artifact_id=candidate['id'], name=candidate['name'],
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
                 '保留原BV替换视频请使用原发布记录中的“更新原稿件”；此处仅准备另发新稿。',
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
