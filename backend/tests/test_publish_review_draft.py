"""审查修订新稿：隔离数据与伪媒体，预览和创建绝不调用平台。"""
import json
import copy
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from dubforge import configuration
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions as av, review_render as rr
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from dubforge.publishing import review_draft as review, service
from dubforge.publishing.models import Publication, PublishChange
from test_publishing import setup_publish


@pytest.fixture()
def revisions(setup_publish, monkeypatch):
    uid, original, originals, calls = setup_publish
    frozen = service.plan(original['id'])['snapshot']
    with Session(get_engine()) as session:
        row = session.get(Publication, original['id'])
        row.snapshot = service._json(frozen)
        row.status = 'rejected'
        row.receipt = service._json(dict(bvid='BV1234567890', aid=222, state_desc='审核退回'))
        row.transfers = service._json({'old-only': {'cid': 777}})
        session.add(row); session.commit()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    definitions = av.catalog(uid)['versions']
    revised = []
    for index, source in enumerate(originals):
        definition = dict(id=f'review-{index}', name=f'审查修订{index}', dubbing=index == 0,
                          burn_subtitles=True, include_comments=False, overrides={})
        definitions.append(definition)
        path = root / f'artifacts/assemble/final_version_{definition["id"]}.mp4'
        service.atomic_write_bytes(path, b'isolated-review-media-' + str(index).encode())
        art = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
        revised.append(art)
        snapshot = av.resolve(Dependencies(project), definition)
        snapshot['review_render'] = dict(schema=rr.RENDER_VERSION, source_artifact_id=source.id,
            fingerprint=f'repair-{index}', encoder={'name': 'h264_nvenc'}, created_at='2026-10-05',
            recipe={'inputs': [{'id': source.id, 'sha256': source.sha256}]})
        with Session(get_engine()) as session:
            session.add(AssemblyResult(key=f'{uid}:{definition["id"]}', project_uid=uid,
                version_id=definition['id'], artifact_id=art.id, artifact_hash=art.sha256,
                fingerprint=f'repair-{index}', snapshot=service._json(snapshot)))
            session.commit()
    cfg = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=cfg['revision'], updates={'assembly.versions': definitions}))
    current = {art.id for art in revised}
    monkeypatch.setattr(rr, 'revision_state', lambda deps, version, record=None, verify_hash=False:
        dict(current=bool(record and record.artifact_id in current), reasons=[] if record and record.artifact_id in current else
             [dict(code='REVIEW_EFFECT_CHANGED', message='实际屏蔽的字幕或声音时段已变化')]))
    yield dict(uid=uid, key=original['id'], originals=originals, revised=revised,
               current=current, calls=calls, frozen=frozen)
    assert calls == []


def body_for(preview, **updates):
    return dict(revision=preview['source']['revision'], fingerprint=preview['fingerprint'],
                request_id=str(uuid.uuid4()), **updates)


def count_rows():
    with Session(get_engine()) as session:
        return len(session.exec(select(Publication)).all())


def stored(key):
    with Session(get_engine()) as session:
        return session.get(Publication, key).model_dump()


def edit_record(uid, art, change):
    with Session(get_engine()) as session:
        row = session.exec(select(AssemblyResult).where(AssemblyResult.project_uid == uid,
                                                        AssemblyResult.artifact_id == art.id)).one()
        snapshot = json.loads(row.snapshot)
        change(snapshot)
        row.snapshot = service._json(snapshot)
        session.add(row); session.commit()


def test_plan_exact_provenance_and_frozen_metadata_without_network_or_writes(revisions):
    r = revisions
    before = stored(r['key'])
    preview = review.plan(r['key'])
    assert count_rows() == 1 and stored(r['key']) == before
    assert preview['mode'] == 'new_submission'
    assert preview['source']['bvid'] == 'BV1234567890'
    assert preview['source']['url'] == 'https://www.bilibili.com/video/BV1234567890'
    assert [(p['source_artifact_id'], p['artifact_id'], p['title']) for p in preview['parts']] == [
        (original.id, revised.id, f'版本{i}') for i, (original, revised) in enumerate(zip(r['originals'], r['revised']))]
    assert preview['settings'] == r['frozen']['settings'] and preview['account_id'] == '123'
    assert preview['cover_id'] == json.loads(before['draft'])['cover_id']
    assert '新BV' in preview['notices'][0]
    for candidate in service.candidates(r['uid'])['files']:
        if candidate['id'] in r['current']:
            assert candidate['is_review_revision'] and candidate['review_revision_current']
            assert candidate['review_source_artifact_id'] in {a.id for a in r['originals']}
    capabilities = service.catalog()['studio_capabilities']
    assert capabilities['replace_parts'] is True
    assert capabilities['update_existing'] is True
    fields = capabilities['field_capabilities']
    assert fields['title']['read_scope'] == 'value'
    assert fields['title']['write'] == 'remote_edit'
    assert fields['selected_comments']['write'] == 'submission_only'
    assert fields['subtitles']['write'] == 'read_only'
    assert fields['commercial_promotion']['write'] == 'unsupported'


def test_create_idempotent_preserves_original_empty_draft_and_later_manual_edits(revisions):
    r = revisions
    unrelated = service.create(r['uid'])
    old_original, old_empty = stored(r['key']), stored(unrelated['id'])
    preview = review.plan(r['key'])
    request = body_for(preview)
    created = review.create(r['key'], request)
    assert created['status'] == 'draft' and created['reused'] is False
    assert created['snapshot'] == created['receipt'] == {} and json.loads(stored(created['id'])['transfers']) == {}
    assert created['settings'] == r['frozen']['settings']
    assert created['draft']['template_id'] == ''
    assert created['draft']['overrides'] == r['frozen']['settings']
    assert created['draft']['review_source']['source_bvid'] == 'BV1234567890'
    assert created['draft']['review_source']['selection_changed'] is False
    assert all(p['matches_original_part'] for p in created['draft']['review_source']['parts'])
    edited = service.save(created['id'], dict(created['draft'], revision=created['revision'],
        parts=[dict(artifact_id=r['revised'][1].id, title='手动选择')], review_source={'source_bvid': '伪造'}))
    assert edited['draft']['review_source']['source_bvid'] == 'BV1234567890'
    assert edited['draft']['review_source']['selection_changed'] is True
    assert edited['draft']['review_source']['parts'][0] == dict(index=1, title='手动选择',
        artifact_id=r['revised'][1].id, source_artifact_id=r['originals'][1].id, matches_original_part=False)
    duplicate = review.create(r['key'], request)
    other_click = review.create(r['key'], body_for(preview))
    assert duplicate['id'] == other_click['id'] == created['id']
    assert duplicate['reused'] is other_click['reused'] is True
    assert duplicate['draft'] == edited['draft']
    assert count_rows() == 3
    assert stored(r['key']) == old_original and stored(unrelated['id']) == old_empty


def test_preview_and_create_api_contract_and_cross_site_protection(revisions):
    from app.main import app
    client = TestClient(app)
    key = revisions['key']
    base = f'/api/publishing/publications/{key}/review-draft'
    preview_response = client.post(base + '/plan', json={})
    assert preview_response.status_code == 200
    preview = preview_response.json()
    response = client.post(base, json=body_for(preview))
    assert response.status_code == 200 and response.json()['status'] == 'draft'
    blocked = client.post(base, json=body_for(preview), headers={'sec-fetch-site': 'cross-site'})
    assert blocked.status_code == 400 and blocked.json()['code'] == 'PUBLISH_ORIGIN'


@pytest.mark.parametrize('failure', ['not-current', 'missing', 'bytes', 'wrong-provenance', 'wrong-frozen-hash'])
def test_one_unmatched_part_blocks_entire_draft(revisions, failure):
    r = revisions
    art = r['revised'][1]
    preview = review.plan(r['key'])
    if failure == 'not-current':
        r['current'].remove(art.id)
    elif failure == 'missing':
        (crud.project_dir(r['uid']) / art.rel_path).unlink()
    elif failure == 'bytes':
        service.atomic_write_bytes(crud.project_dir(r['uid']) / art.rel_path, b'tampered')
    elif failure == 'wrong-provenance':
        edit_record(r['uid'], art, lambda s: s['review_render'].update(source_artifact_id=r['originals'][0].id))
    else:
        edit_record(r['uid'], art, lambda s: s['review_render']['recipe']['inputs'][0].update(sha256='other'))
    with pytest.raises(DubForgeError) as error:
        review.create(r['key'], body_for(preview))
    assert error.value.code == 'PUBLISH_REVIEW_MAPPING'
    assert error.value.detail['parts'][0]['index'] == 2
    assert count_rows() == 1


def test_multiple_current_revisions_are_ambiguous_and_old_invalid_revision_is_ignored(revisions):
    r = revisions
    project = crud.get_project(r['uid'])
    root = crud.project_dir(r['uid'])
    old = r['revised'][0]
    path = root / 'artifacts/assemble/final_version_review-other.mp4'
    service.atomic_write_bytes(path, b'other-review-file')
    newer = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
    with Session(get_engine()) as session:
        original_record = session.get(AssemblyResult, f'{r["uid"]}:review-0')
        snapshot = json.loads(original_record.snapshot)
        snapshot['id'] = 'review-other'; snapshot['name'] = '另一审查修订'
        session.add(AssemblyResult(key=f'{r["uid"]}:review-other', project_uid=r['uid'], version_id='review-other',
            artifact_id=newer.id, artifact_hash=newer.sha256, fingerprint='other-repair', snapshot=service._json(snapshot)))
        session.commit()
    cfg = configuration.resolve(r['uid'])
    definition = dict(id='review-other', name='另一审查修订', dubbing=True, burn_subtitles=True,
                      include_comments=False, overrides={})
    configuration.save('project:' + r['uid'], dict(revision=cfg['revision'],
        updates={'assembly.versions': av.catalog(r['uid'])['versions'] + [definition]}))
    r['current'].add(newer.id)
    with pytest.raises(DubForgeError) as error:
        review.plan(r['key'])
    assert error.value.code == 'PUBLISH_REVIEW_MAPPING' and '多个' in error.value.detail['parts'][0]['reason']
    assert count_rows() == 1
    r['current'].remove(old.id)
    assert review.plan(r['key'])['parts'][0]['artifact_id'] == newer.id


@pytest.mark.parametrize('status', ['uploading', 'submitting', 'unknown'])
def test_active_or_unknown_original_is_protected(revisions, status):
    with Session(get_engine()) as session:
        row = session.get(Publication, revisions['key']); row.status = status
        session.add(row); session.commit()
    with pytest.raises(DubForgeError) as error:
        review.plan(revisions['key'])
    assert error.value.code == 'PUBLISH_REVIEW_SOURCE_BUSY' and count_rows() == 1


def test_pending_remote_change_is_protected(revisions):
    with Session(get_engine()) as session:
        session.add(PublishChange(account_id='123', bvid='BV1234567890', kind='append',
            publication_id=revisions['key'], status='unknown'))
        session.commit()
    with pytest.raises(DubForgeError) as error:
        review.plan(revisions['key'])
    assert error.value.code == 'PUBLISH_WRITE_UNKNOWN' and count_rows() == 1


@pytest.mark.parametrize('field', ['revision', 'fingerprint'])
def test_stale_preview_does_not_create_row(revisions, field):
    request = body_for(review.plan(revisions['key']))
    request[field] = 0 if field == 'revision' else 'wrong'
    with pytest.raises(DubForgeError) as error:
        review.create(revisions['key'], request)
    assert error.value.code == 'PUBLISH_CONFLICT' and count_rows() == 1


def test_consumed_request_cannot_be_reused_with_other_fingerprint(revisions):
    request = body_for(review.plan(revisions['key']))
    review.create(revisions['key'], request)
    request['fingerprint'] = 'forged'
    with pytest.raises(DubForgeError) as error:
        review.create(revisions['key'], request)
    assert error.value.code == 'PUBLISH_CONFLICT' and count_rows() == 2


def test_review_draft_keeps_original_confirmed_metadata_when_project_defaults_change(revisions):
    r = revisions
    preview = review.plan(r['key'])
    cfg = configuration.resolve(r['uid'])
    configuration.save('project:' + r['uid'], dict(revision=cfg['revision'],
        updates={'publish.title': '其他系列默认标题', 'publish.tags': ['其他默认标签']}))
    created = review.create(r['key'], body_for(preview))
    assert created['settings'] == r['frozen']['settings']
    assert service.plan(created['id'])['snapshot']['settings'] == r['frozen']['settings']


@pytest.mark.parametrize('issue', ['missing-bv', 'missing-parts', 'bad-part', 'append-only', 'cover-changed'])
def test_incomplete_original_snapshot_or_cover_is_rejected(revisions, issue):
    with Session(get_engine()) as session:
        row = session.get(Publication, revisions['key'])
        snapshot, receipt = json.loads(row.snapshot), json.loads(row.receipt)
        if issue == 'missing-bv': receipt = {}
        elif issue == 'missing-parts': snapshot['parts'] = []
        elif issue == 'bad-part': snapshot['parts'] = ['bad']
        elif issue == 'append-only': snapshot['update_target'] = 'BV1234567890'
        else: snapshot['cover_sha256'] = 'changed'
        row.snapshot, row.receipt = service._json(snapshot), service._json(receipt)
        session.add(row); session.commit()
    with pytest.raises(DubForgeError) as error:
        review.plan(revisions['key'])
    assert error.value.code == 'PUBLISH_REVIEW_SOURCE_INVALID' and count_rows() == 1


def test_client_cannot_forge_review_source_and_regular_draft_cannot_acquire_one(revisions):
    preview = review.plan(revisions['key'])
    with pytest.raises(DubForgeError):
        review.create(revisions['key'], {**body_for(preview), 'review_source': {'source_bvid': 'BVfake'}})
    row = service.create(revisions['uid'])
    saved = service.save(row['id'], dict(row['draft'], revision=row['revision'], review_source={'source_bvid': 'BVfake'}))
    assert 'review_source' not in saved['draft'] and count_rows() == 2


def test_candidate_and_publish_preflight_reject_revision_that_became_stale(revisions):
    r = revisions
    created = review.create(r['key'], body_for(review.plan(r['key'])))
    r['current'].remove(r['revised'][0].id)
    candidate = next(c for c in service.candidates(r['uid'])['files'] if c['id'] == r['revised'][0].id)
    assert candidate['review_ready'] is False and candidate['review_status'] == '审查修订已失效'
    assert candidate['review_revision_reasons'] == [dict(code='REVIEW_EFFECT_CHANGED', message='实际屏蔽的字幕或声音时段已变化')]
    assert '实际屏蔽' in candidate['review_reason']
    with pytest.raises(DubForgeError) as error:
        service.plan(created['id'])
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'


def test_revision_metadata_handles_historical_broken_encoder_and_definition(revisions, monkeypatch):
    r = revisions
    edit_record(r['uid'], r['revised'][0], lambda s: s['review_render'].update(encoder='bad-legacy-value'))
    data = review.revision_metadata(crud.get_project(r['uid']), r['revised'])
    assert data[r['revised'][0].id]['review_encoder'] == ''
    assert data[r['revised'][0].id]['review_revision_current'] is False
    monkeypatch.setattr(av, 'resolve', lambda *args: (_ for _ in ()).throw(av.VersionError('坏定义')))
    data = review.revision_metadata(crud.get_project(r['uid']), r['revised'])
    assert data[r['revised'][0].id]['review_revision_current'] is False


def test_changed_registry_hash_cannot_disguise_review_revision_as_regular_output(revisions):
    from dubforge.domain.models import Artifact
    r = revisions
    with Session(get_engine()) as session:
        art = session.get(Artifact, r['revised'][0].id)
        art.sha256 = 'changed-registry-hash'
        session.add(art); session.commit()
    candidate = next(c for c in service.candidates(r['uid'])['files'] if c['id'] == r['revised'][0].id)
    assert candidate['is_review_revision'] and candidate['review_revision_current'] is False
    assert candidate['review_ready'] is False
    with pytest.raises(DubForgeError) as error:
        review.plan(r['key'])
    assert error.value.code == 'PUBLISH_REVIEW_MAPPING'


def test_snapshot_guard_rechecks_revision_inputs_before_upload_and_before_submit(revisions, monkeypatch):
    r = revisions
    created = review.create(r['key'], body_for(review.plan(r['key'])))
    snapshot = service.plan(created['id'])['snapshot']
    r['current'].clear()
    with pytest.raises(DubForgeError) as error:
        review.assert_snapshot_current(snapshot)
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    # worker 开始前规则变化：不允许读取平台账号能力或发送视频。
    with Session(get_engine()) as session:
        row = session.get(Publication, created['id']); row.snapshot = service._json(snapshot)
        session.add(row); session.commit()
    import threading
    service._execute(created['id'], 'publish', threading.Event())
    assert service.view(created['id'])['status'] == 'failed'
    assert r['calls'] == []


@pytest.fixture()
def reassembled_review_submission(revisions, monkeypatch):
    """模拟真实旧审查投稿：普通 assemble 更新同一产物，保留独立来源 sidecar。"""
    r = revisions
    project, root = crud.get_project(r['uid']), crud.project_dir(r['uid'])
    definitions = av.catalog(r['uid'])['versions']
    video_path = root / 'artifacts/download/source.mp4'
    service.atomic_write_bytes(video_path, b'isolated-original-video')
    video = crud.add_artifact(project.id, 'download', 'video', video_path.name,
                             video_path, str(video_path.relative_to(root)))
    sidecars, recipes, frozen_parts = [], [], []
    for index, (source, revised) in enumerate(zip(r['originals'], r['revised'])):
        definition = next(item for item in definitions if item['id'] == f'review-{index}')
        source_definition = {**definition, 'id': f'source-{index}', 'name': f'原成片{index}'}
        definitions.append(source_definition)
        source_snapshot = av.resolve(Dependencies(project), source_definition)
        encoder = dict(name='h264_nvenc', preset='p4', rate_control='vbr',
                       quality_parameter='cq', quality_value=23)
        recipe = dict(schema=rr.RENDER_VERSION, source_artifact_id=source.id,
            video_artifact_id=video.id,
            inputs=[dict(id=art.id, step=art.step_key, name=art.name, sha256=art.sha256,
                         size=art.size, rel_path=art.rel_path) for art in (video, source)],
            definition={key: definition[key] for key in ('dubbing', 'burn_subtitles', 'include_comments')},
            values=copy.deepcopy(source_snapshot['values']), comment_overlay=None,
            review_receipt=dict(signature='isolated-review', ranges=[]), audio_filter='',
            encoder=encoder)
        info = dict(schema=rr.RENDER_VERSION, source_artifact_id=source.id,
                    fingerprint=av.digest(recipe), recipe=recipe, encoder=encoder,
                    created_at='2026-10-05T10:00:00Z')
        path = root / revised.rel_path
        sidecar = path.with_suffix('.review.json')
        service.atomic_write_bytes(sidecar, service._json(info).encode())
        sidecars.append(sidecar); recipes.append(info)
        frozen_parts.append(dict(artifact_id=revised.id, title=f'修订分P{index}',
                                 sha256=revised.sha256, size=revised.size, path=str(path)))
        # 正式登记原成片，sidecar 的底层来源必须有成功快照而非仅一个文件。
        with Session(get_engine()) as session:
            session.add(AssemblyResult(key=f'{r["uid"]}:source-{index}', project_uid=r['uid'],
                version_id=source_definition['id'], artifact_id=source.id, artifact_hash=source.sha256,
                fingerprint=f'original-{index}', snapshot=service._json(source_snapshot)))
            session.commit()
        # 普通 assemble 原子替换同一最终名，Artifact ID 沿用，hash/size 正常更新。
        service.atomic_write_bytes(path, b'new-reviewed-production-output-' + str(index).encode())
        updated = crud.add_artifact(project.id, 'assemble', 'video', revised.name, path, revised.rel_path,
                                    replace_existing=True)
        assert updated.id == revised.id and updated.sha256 != revised.sha256
        r['revised'][index] = updated
        snapshot = av.resolve(Dependencies(project), definition)
        with Session(get_engine()) as session:
            record = session.get(AssemblyResult, f'{r["uid"]}:review-{index}')
            record.artifact_hash = updated.sha256
            record.fingerprint = f'current-production-{index}'
            record.snapshot = service._json(snapshot)
            session.add(record); session.commit()
    cfg = configuration.resolve(r['uid'])
    configuration.save('project:' + r['uid'], dict(revision=cfg['revision'],
        updates={'assembly.versions': definitions}))
    with Session(get_engine()) as session:
        row = session.get(Publication, r['key'])
        snapshot = json.loads(row.snapshot); snapshot['parts'] = frozen_parts
        row.snapshot = service._json(snapshot)
        draft = json.loads(row.draft)
        draft['parts'] = [dict(artifact_id=part['artifact_id'], title=part['title']) for part in frozen_parts]
        row.draft = service._json(draft)
        session.add(row); session.commit()
    current = {f'review-{index}' for index in range(len(r['revised']))}
    monkeypatch.setattr(av, 'is_current', lambda deps, version, **kwargs: version['id'] in current)
    return {**r, 'sidecars': sidecars, 'sidecar_info': recipes, 'version_current': current, 'video': video}


def test_reassembled_reviewed_submission_reuses_same_artifact_with_new_hash(reassembled_review_submission):
    r = reassembled_review_submission
    before = stored(r['key'])
    preview = review.plan(r['key'])
    assert count_rows() == 1 and stored(r['key']) == before
    assert [(part['source_artifact_id'], part['artifact_id'], part['sha256']) for part in preview['parts']] == [
        (source.id, revised.id, revised.sha256) for source, revised in zip(r['originals'], r['revised'])]
    assert [part['title'] for part in preview['parts']] == ['修订分P0', '修订分P1']
    created = review.create(r['key'], body_for(preview))
    assert created['status'] == 'draft' and created['snapshot'] == created['receipt'] == {}
    assert created['draft']['review_source']['selection_changed'] is False
    assert all(part['matches_original_part'] for part in created['draft']['review_source']['parts'])
    assert count_rows() == 2 and stored(r['key']) == before


def test_reviewed_submission_without_output_hash_change_still_resolves_underlying_source(reassembled_review_submission):
    r = reassembled_review_submission
    with Session(get_engine()) as session:
        row = session.get(Publication, r['key'])
        snapshot = json.loads(row.snapshot)
        for part, art in zip(snapshot['parts'], r['revised']):
            part.update(sha256=art.sha256, size=art.size)
        row.snapshot = service._json(snapshot)
        session.add(row); session.commit()
    preview = review.plan(r['key'])
    assert [part['source_artifact_id'] for part in preview['parts']] == [art.id for art in r['originals']]
    assert count_rows() == 1


@pytest.mark.parametrize('issue', ['missing', 'bad-json', 'unknown-schema', 'digest', 'encoder',
    'empty-inputs', 'source-id', 'source-hash', 'source-bytes', 'output-bytes',
    'missing-source', 'current-version', 'definition', 'video-id', 'video-guard'])
def test_reassembled_review_sidecar_requires_verifiable_provenance(reassembled_review_submission, issue):
    r = reassembled_review_submission
    before = stored(r['key'])
    sidecar = r['sidecars'][0]
    info = copy.deepcopy(r['sidecar_info'][0])
    if issue == 'missing':
        sidecar.unlink()
    elif issue == 'bad-json':
        service.atomic_write_bytes(sidecar, b'{broken')
    elif issue == 'source-bytes':
        source = r['originals'][0]
        path = crud.project_dir(r['uid']) / source.rel_path
        service.atomic_write_bytes(path, b'x' * source.size)
    elif issue == 'output-bytes':
        art = r['revised'][0]
        service.atomic_write_bytes(crud.project_dir(r['uid']) / art.rel_path, b'x' * art.size)
    elif issue == 'missing-source':
        (crud.project_dir(r['uid']) / r['originals'][0].rel_path).unlink()
    elif issue == 'current-version':
        r['version_current'].remove('review-0')
    else:
        if issue == 'unknown-schema':
            info['schema'] = info['recipe']['schema'] = 'review-render-unrecognized'
        elif issue == 'digest':
            info['fingerprint'] = 'not-the-recipe-digest'
        elif issue == 'encoder':
            info['encoder'] = {'name': 'different-from-recipe'}
        elif issue == 'empty-inputs':
            info['recipe']['inputs'] = []
        elif issue == 'source-id':
            info['source_artifact_id'] = r['originals'][1].id
        elif issue == 'source-hash':
            info['recipe']['inputs'][1]['sha256'] = 'not-the-original-hash'
        elif issue == 'definition':
            info['recipe']['definition']['dubbing'] = not info['recipe']['definition']['dubbing']
        elif issue == 'video-id':
            info['recipe']['video_artifact_id'] = r['originals'][0].id
        elif issue == 'video-guard':
            info['recipe']['inputs'] = [item for item in info['recipe']['inputs']
                                        if item.get('id') != r['video'].id]
        if issue not in {'digest', 'encoder', 'source-id'}:
            # 即使重算摘要也不能掩盖未知格式、输入/原成片或版本定义不匹配。
            info['fingerprint'] = av.digest(info['recipe'])
        service.atomic_write_bytes(sidecar, service._json(info).encode())
    with pytest.raises(DubForgeError) as error:
        review.plan(r['key'])
    assert error.value.code in {'PUBLISH_REVIEW_MAPPING', 'CONTENT_REVIEW_OUTPUT_STALE'}
    assert count_rows() == 1 and stored(r['key']) == before and r['calls'] == []


def test_changed_regular_original_hash_is_not_relaxed_by_valid_review_candidate(revisions):
    r = revisions
    source = r['originals'][0]
    path = crud.project_dir(r['uid']) / source.rel_path
    service.atomic_write_bytes(path, b'changed-ordinary-source')
    updated = crud.add_artifact(crud.get_project(r['uid']).id, 'assemble', 'video',
                                source.name, path, source.rel_path, replace_existing=True)
    assert updated.id == source.id and updated.sha256 != source.sha256
    with pytest.raises(DubForgeError) as error:
        review.plan(r['key'])
    assert error.value.code == 'PUBLISH_REVIEW_MAPPING'
    assert '冻结哈希' in error.value.detail['parts'][0]['reason']
    assert count_rows() == 1 and r['calls'] == []
