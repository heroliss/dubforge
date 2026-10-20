"""选片元数据只描述成功文件；隔离目录、伪视频，不调用真实媒体或发布服务。"""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as versions
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from dubforge.publishing import service, updates
from dubforge.publishing.chapters import freeze_parts
from test_publishing import setup_publish  # noqa: F401


def _definition(name='生成时的原声设置'):
    return dict(id='recorded', name=name, dubbing=False, burn_subtitles=True, include_comments=False,
                overrides={'assemble.subtitle_track': 'source'})


def _record(uid, artifact, *, generated_at='2026-10-17T03:04:05+00:00'):
    definition = _definition()
    versions.save(uid, dict(revision=versions.catalog(uid)['revision'], versions=[definition]))
    snapshot = versions.resolve(Dependencies(crud.get_project(uid)), definition)
    if generated_at is not None:
        snapshot['generated_at'] = generated_at
    with crud.session() as session:
        session.merge(AssemblyResult(key=f'{uid}:recorded', project_uid=uid, version_id='recorded',
            artifact_id=artifact.id, artifact_hash=artifact.sha256, fingerprint='fixture',
            snapshot=json.dumps(snapshot, ensure_ascii=False)))
        session.commit()
    return snapshot


def test_candidate_name_and_settings_come_from_success_not_current_form(setup_publish, monkeypatch):
    uid, _, artifacts, _ = setup_publish
    artifact = artifacts[0]
    _record(uid, artifact)
    current = _definition('后来重命名的设置')
    current['overrides']['assemble.subtitle_track'] = 'bilingual'
    versions.save(uid, dict(revision=versions.catalog(uid)['revision'], versions=[current]))
    monkeypatch.setattr(service, '_probe', lambda path: (_ for _ in ()).throw(AssertionError('列表不应探测视频')))

    result = next(row for row in service.candidates(uid)['files'] if row['id'] == artifact.id)
    assert result['version_generated_name'] == '生成时的原声设置'
    assert result['version_name'] == '后来重命名的设置'
    assert result['version_summary'] == '原声 · 原文字幕'
    assert result['generated_at'] == '2026-10-17T03:04:05+00:00'
    assert result['generated_at_source'] == 'assembly_success'
    assert result['registered_at'] == result['created_at']
    assert result['created_at'].endswith('+00:00')


def test_rerender_same_artifact_records_latest_success_time(setup_publish, monkeypatch):
    uid, _, artifacts, _ = setup_publish
    artifact = artifacts[0]
    version = _record(uid, artifact)
    before = copy.deepcopy(version)
    moments = iter([datetime(2026, 10, 17, 4, tzinfo=timezone.utc), datetime(2026, 10, 17, 6, tzinfo=timezone.utc)])
    monkeypatch.setattr(versions, 'datetime', SimpleNamespace(now=lambda tz: next(moments), fromisoformat=datetime.fromisoformat))
    context = SimpleNamespace(project=crud.get_project(uid))
    versions.record_result(context, version, artifact.name, 'first')
    first = versions.artifact_metadata(uid)[artifact.id]
    versions.record_result(context, version, artifact.name, 'second')
    second = versions.artifact_metadata(uid)[artifact.id]

    assert first['generated_at'] == '2026-10-17T04:00:00+00:00'
    assert second['generated_at'] == '2026-10-17T06:00:00+00:00'
    assert first['registered_at'] == second['registered_at']
    assert versions.results(uid)['recorded'].fingerprint == 'second'
    assert version == before  # 不向运行参数注入登记状态。


def test_historical_unknown_time_and_hash_mismatch_are_not_invented(setup_publish):
    uid, _, artifacts, _ = setup_publish
    artifact = artifacts[0]
    _record(uid, artifact, generated_at=None)
    result = next(row for row in service.candidates(uid)['files'] if row['id'] == artifact.id)
    assert result['generated_at'] is None
    assert result['generated_at_source'] == 'artifact_registered'
    assert result['registered_at']
    with crud.session() as session:
        record = session.get(AssemblyResult, f'{uid}:recorded')
        record.artifact_hash = 'not-this-file'
        session.add(record)
        session.commit()
    result = next(row for row in service.candidates(uid)['files'] if row['id'] == artifact.id)
    assert result['generated_at'] is None
    assert 'version_generated_name' not in result


def test_new_publication_and_replacement_freeze_success_metadata(setup_publish):
    uid, row, artifacts, _ = setup_publish
    artifact = artifacts[0]
    _record(uid, artifact)
    snapshot = service.plan(row['id'])['snapshot']
    part = next(part for part in snapshot['parts'] if part['artifact_id'] == artifact.id)
    assert part['version_generated_name'] == '生成时的原声设置'
    assert part['version_summary'] == '原声 · 原文字幕'
    assert part['generated_at'] == '2026-10-17T03:04:05+00:00'
    replacement = updates._freeze_file(dict(project_uid=uid), dict(artifact_id=artifact.id), dict(cid=1, title='原P'))
    assert replacement['generated_at'] == part['generated_at']
    assert replacement['version_generated_name'] == part['version_generated_name']
    assert replacement['version_summary'] == part['version_summary']

    original = copy.deepcopy(part)
    with crud.session() as session:
        record = session.get(AssemblyResult, f'{uid}:recorded')
        saved = json.loads(record.snapshot)
        saved.update(name='重做后新名称', generated_at='2026-10-18T00:00:00+00:00')
        record.snapshot = json.dumps(saved)
        session.add(record)
        session.commit()
    assert freeze_parts(uid, [part], dict(enabled=False, items=[]))[0] == original


def test_sparse_legacy_success_snapshot_does_not_break_candidate_list(setup_publish):
    uid, _, artifacts, _ = setup_publish
    artifact = artifacts[0]
    _record(uid, artifact)
    with crud.session() as session:
        record = session.get(AssemblyResult, f'{uid}:recorded')
        record.snapshot = json.dumps(dict(output_cuts=None))
        session.add(record)
        session.commit()
    result = next(row for row in service.candidates(uid)['files'] if row['id'] == artifact.id)
    assert result['generated_at'] is None
    assert 'version_summary' not in result
    assert result['registered_at']
