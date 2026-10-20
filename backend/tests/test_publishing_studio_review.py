"""追加分P在当前规则下复核冻结文件；测试账号和网络均为隔离桩。"""
import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from dubforge import comment_studio, config, configuration, content_review_service, db
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions, content_redaction, review_render
from dubforge.pipeline.dependencies import Dependencies
from dubforge.publishing import service, studio
from dubforge.publishing.models import Publication, PublishChange
from dubforge.utils.fs import atomic_write_bytes
from test_content_redaction import _seed_text
from test_publish_studio import BV, env as studio_env


@pytest.fixture
def review_env(studio_env):
    _project_setting(studio_env.uid, 'content_review.enabled', True)
    return studio_env


def _project_setting(uid, path, value, *, external=False):
    if external:
        # 模拟绕开正常编辑门禁的外部数据库改动，发送前仍必须再次核对。
        from dubforge.configuration_models import ConfigurationScope
        with Session(db.get_engine()) as session:
            row = session.get(ConfigurationScope, 'project:' + uid)
            patch = json.loads(row.values)
            patch[path] = value
            row.values = configuration._json(patch)
            row.revision += 1
            session.add(row)
            session.commit()
        return
    current = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=current['revision'], updates={path: value}))


def _uploaded(env, *, risky=False, reviewed=True, comments=False):
    if risky:
        project, root, _ = _seed_text(env.uid)
    else:
        project, root, _ = _seed_text(env.uid,
            source=[dict(id=1, start=0.5, end=2.5, text='Welcome to this beautiful city')],
            target={1: '欢迎来到美丽的城市'})
    parts, transfers = [], {}
    for index, title in enumerate(('审核版原声', '审核版配音')):
        path = root / 'output' / f'final_version_review-{index}.mp4'
        atomic_write_bytes(path, f'isolated review video {index}'.encode())
        crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
        artifact = crud.latest_artifacts(project.id, ['assemble'])['assemble/' + path.name]
        snapshot = dict(id=f'review-{index}', include_comments=comments, comment_overlay=None)
        if reviewed:
            snapshot['content_review_receipt'] = content_redaction.receipt(Dependencies(project))
            if comments:
                snapshot['comment_overlay'] = {'items': []}
        assembly_versions.record_result(SimpleNamespace(project=project), snapshot, path.name, 'fixture')
        parts.append(dict(artifact_id=artifact.id, sha256=artifact.sha256, title=title,
                          has_dubbing=bool(index)))
        transfers[str(artifact.id)] = dict(cid=index + 3, filename=f'uploaded-review-{index}')
    record = Publication(project_uid=env.uid, status='uploaded', snapshot=service._json(dict(
        project_uid=env.uid, account_id='123', settings={**service.DEFAULTS,
        'title': '城市旅行', 'declaration_id': 1}, origins={}, parts=parts)),
        transfers=service._json(transfers))
    with Session(db.get_engine()) as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id


def _prepare(key):
    return studio.prepare('123', dict(bvid=BV, kind='append', publication_id=key))


def _writes(env):
    return [payload for op, payload in env.calls if op == 'studio_write']


def test_reviewed_append_preserves_original_then_dub_order_and_sends_once(review_env):
    key = _uploaded(review_env, risky=True)
    change = _prepare(key)
    assert [p['cid'] for p in change['preview']['after']['videos']] == [1, 2, 3, 4]
    assert not _writes(review_env)
    assert studio.commit('123', change['id'], {'confirmed': True})['status'] == 'done'
    assert studio.commit('123', change['id'], {'confirmed': True})['status'] == 'done'
    assert [p['cid'] for p in _writes(review_env)[0]['videos']] == [3, 4]
    assert [p['title'] for p in _writes(review_env)[0]['videos']] == ['审核版原声', '审核版配音']
    assert len(_writes(review_env)) == 1


@pytest.mark.parametrize('comments', [False, True])
def test_old_upload_without_required_review_proof_cannot_prepare_append(review_env, comments):
    key = _uploaded(review_env, risky=True, reviewed=False, comments=comments)
    with pytest.raises(DubForgeError) as error:
        _prepare(key)
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    assert not _writes(review_env)
    assert service.view(key)['status'] == 'uploaded'
    with Session(db.get_engine()) as session:
        assert session.get(Publication, key).status == 'uploaded'
        from sqlmodel import select
        assert session.exec(select(PublishChange)).all() == []


@pytest.mark.parametrize('after_preview', [False, True])
def test_terms_changed_after_upload_or_preview_block_append(review_env, after_preview):
    key = _uploaded(review_env)
    change = _prepare(key) if after_preview else None
    _project_setting(review_env.uid, 'content_review.blocked_terms', 'beautiful')
    with pytest.raises(DubForgeError) as error:
        if change:
            studio.commit('123', change['id'], {'confirmed': True})
        else:
            _prepare(key)
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    assert not _writes(review_env)
    assert service.view(key)['status'] == 'uploaded'


def test_changed_audio_policy_invalidates_uploaded_review_before_commit(review_env):
    key = _uploaded(review_env, risky=True)
    change = _prepare(key)
    _project_setting(review_env.uid, 'content_review.mute_audio', False)
    with pytest.raises(DubForgeError) as error:
        studio.commit('123', change['id'], {'confirmed': True})
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    assert not _writes(review_env)


def test_historical_baked_comment_blocks_append_without_speech_findings(review_env):
    key = _uploaded(review_env, comments=True)
    project = crud.get_project(review_env.uid)
    assert content_redaction.plan(Dependencies(project))['ranges'] == []
    with Session(db.get_engine()) as session:
        record = assembly_versions.results(review_env.uid)['review-0']
        snapshot = json.loads(record.snapshot)
        snapshot['comment_overlay'] = {'items': [dict(id='old-comment',
            text='the tragedy of Tiananmen Square', display_text='现在的评论稿已经删掉了原话')]}
        record.snapshot = service._json(snapshot)
        session.merge(record)
        session.commit()
    with pytest.raises(DubForgeError, match='已烧录的评论') as error:
        _prepare(key)
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE' and not _writes(review_env)


def test_last_moment_rule_change_is_definite_failure_without_remote_write(review_env, monkeypatch):
    key = _uploaded(review_env)
    change = _prepare(key)
    real_check, checks = content_review_service.assert_snapshot, []

    def check(snapshot):
        checks.append(snapshot)
        real_check(snapshot)
        if len(checks) == 1:
            # 首次检查后规则变化，发送前的第二次检查仍必须阻止网络写入。
            _project_setting(review_env.uid, 'content_review.blocked_terms', 'beautiful', external=True)

    monkeypatch.setattr(content_review_service, 'assert_snapshot', check)
    with pytest.raises(DubForgeError) as error:
        studio.commit('123', change['id'], {'confirmed': True})
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    assert len(checks) == 2 and not _writes(review_env)
    with Session(db.get_engine()) as session:
        row = session.get(PublishChange, change['id'])
        assert row.status == 'failed' and '审查' in row.error
    assert service.view(key)['status'] == 'uploaded'
    assert change['id'] not in studio._inflight


@pytest.mark.parametrize('kind,values', [('edit', {'description': '新的旅行简介'}),
    ('collection', {'season_id': 4, 'section_id': 5})])
def test_metadata_or_collection_changes_do_not_require_video_review(review_env, monkeypatch, kind, values):
    _uploaded(review_env, risky=True, reviewed=False)

    def unexpected(snapshot):
        pytest.fail('只修改文案或合集不应检查未选用的本地视频')

    monkeypatch.setattr(content_review_service, 'assert_snapshot', unexpected)
    from dubforge.publishing import review_draft
    monkeypatch.setattr(review_draft, 'assert_snapshot_current', unexpected)
    change = studio.prepare('123', dict(bvid=BV, kind=kind, values=values))
    assert studio.commit('123', change['id'], {'confirmed': True})['status'] == 'done'
    assert len(_writes(review_env)) == 1 and _writes(review_env)[0]['videos'] == []


def test_frozen_artifact_hash_change_blocks_append(review_env):
    key = _uploaded(review_env)
    change = _prepare(key)
    from dubforge.domain.models import Artifact
    with Session(db.get_engine()) as session:
        publication = session.get(Publication, key)
        artifact_id = json.loads(publication.snapshot)['parts'][0]['artifact_id']
        artifact = session.get(Artifact, artifact_id)
        artifact.sha256 = 'changed-file-hash'
        session.add(artifact)
        session.commit()
    with pytest.raises(DubForgeError) as error:
        studio.commit('123', change['id'], {'confirmed': True})
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    assert not _writes(review_env)


def _revision_proof(env, monkeypatch):
    """媒体制作不参与追加用例，修订有效性桩保留实际领域快照及变更输入。"""
    project = crud.get_project(env.uid)
    definitions = []
    with Session(db.get_engine()) as session:
        for index, record in enumerate(assembly_versions.results(env.uid).values()):
            spec = dict(id=record.version_id, name=f'审查修订{index}', dubbing=bool(index),
                        burn_subtitles=True, include_comments=True, overrides={})
            definitions.append(spec)
            previous = json.loads(record.snapshot)
            snapshot = assembly_versions.resolve(Dependencies(project), spec,
                comment_plan={'settings': comment_studio.DEFAULT_SETTINGS, 'items': []})
            snapshot['content_review_receipt'] = previous['content_review_receipt']
            snapshot['review_render'] = dict(schema=review_render.RENDER_VERSION,
                source_artifact_id=100 + index, fingerprint='isolated-recipe',
                encoder={'name': 'libx264'})
            record.snapshot = service._json(snapshot)
            session.merge(record)
        session.commit()
    current_settings = configuration.resolve(env.uid)
    configuration.save('project:' + env.uid, dict(revision=current_settings['revision'],
        updates={'assembly.versions': definitions}))

    def signature():
        return service._hash([comment_studio._read(project).get('overlay'),
                              configuration.resolve(env.uid)['values']['assemble.crf']])

    frozen, checks = signature(), []

    def current(deps, version, record=None, verify_hash=False):
        checks.append((record.artifact_id, verify_hash))
        valid = signature() == frozen
        return dict(current=valid, reasons=[] if valid else [dict(code='REVIEW_RECIPE_CHANGED', message='修订制作设置已变化')])

    monkeypatch.setattr(review_render, 'revision_state', current)
    return checks


def test_current_revision_recipe_can_be_appended(review_env, monkeypatch):
    key = _uploaded(review_env)
    checks = _revision_proof(review_env, monkeypatch)
    change = _prepare(key)
    assert studio.commit('123', change['id'], {'confirmed': True})['status'] == 'done'
    assert len(_writes(review_env)) == 1
    assert len(checks) == 6 and all(verify_hash for _, verify_hash in checks)


@pytest.mark.parametrize('after_preview', [False, True])
@pytest.mark.parametrize('changed_input', ['comments', 'render_settings'])
def test_changed_revision_recipe_blocks_append_even_without_text_risk(review_env, monkeypatch,
                                                                      after_preview, changed_input):
    key = _uploaded(review_env)
    checks = _revision_proof(review_env, monkeypatch)
    change = _prepare(key) if after_preview else None
    if changed_input == 'comments':
        comment_studio._write(crud.get_project(review_env.uid), {'overlay': dict(
            settings=comment_studio.DEFAULT_SETTINGS, items=[dict(id='c1', source_hash='source',
            text='A lovely city', source_text='A lovely city', display_text='城市很漂亮')])})
    else:
        _project_setting(review_env.uid, 'assemble.crf', configuration.resolve(review_env.uid)['values']['assemble.crf'] + 1)
    assert content_redaction.plan(Dependencies(crud.get_project(review_env.uid)))['ranges'] == []
    with pytest.raises(DubForgeError) as error:
        if change:
            studio.commit('123', change['id'], {'confirmed': True})
        else:
            _prepare(key)
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE' and not _writes(review_env)
    assert checks and all(verify_hash for _, verify_hash in checks)


def test_revision_recipe_rechecked_at_actual_send_boundary(review_env, monkeypatch):
    key = _uploaded(review_env)
    _revision_proof(review_env, monkeypatch)
    change = _prepare(key)
    from dubforge.publishing import review_draft
    real_check, checks = review_draft.assert_snapshot_current, []

    def check(snapshot):
        checks.append(snapshot)
        real_check(snapshot)
        if len(checks) == 1:
            _project_setting(review_env.uid, 'assemble.crf', configuration.resolve(review_env.uid)['values']['assemble.crf'] + 1,
                             external=True)

    monkeypatch.setattr(review_draft, 'assert_snapshot_current', check)
    with pytest.raises(DubForgeError) as error:
        studio.commit('123', change['id'], {'confirmed': True})
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    assert len(checks) == 2 and not _writes(review_env)
    with Session(db.get_engine()) as session:
        assert session.get(PublishChange, change['id']).status == 'failed'
