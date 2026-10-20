"""发布章节只绑定成功成片；测试不读取用户数据或调用远端。"""
import copy
import json
from types import SimpleNamespace

import pytest

from dubforge.domain import crud
from dubforge.pipeline import review_render
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.publishing.chapters import freeze_parts, map_items
from dubforge.publishing.service import PublishError


def _cuts(ranges=((0, 10), (20, 30), (50, 60)), duration=100):
    return dict(schema=1, source_fingerprint='source-fixture', source_duration=duration,
                output_duration=duration - sum(end - start for start, end in ranges),
                ranges=[dict(start=start, end=end) for start, end in ranges])


def _artifact(uid, identifier, cuts):
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / 'output' / f'final_version_{identifier}.mp4'
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(identifier.encode())
    artifact = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
    with crud.session() as session:
        session.add(AssemblyResult(key=f'{uid}:{identifier}', project_uid=uid, version_id=identifier,
                    artifact_id=artifact.id, artifact_hash=artifact.sha256, fingerprint=identifier,
                    snapshot=json.dumps({'output_cuts': cuts})))
        session.commit()
    return dict(artifact_id=artifact.id, sha256=artifact.sha256, title=identifier)


def test_deleted_chapter_does_not_borrow_next_chapter_start():
    rows = [{'title': title, 'start_time': start} for title, start in
            [('片头广告', 0), ('开场', 10), ('中段广告', 20), ('正文', 30), ('推广', 50), ('结尾', 60)]]
    mapped, timeline = map_items(_cuts(), rows)
    assert mapped == [dict(title='开场', start_time=0, end_time=10),
                      dict(title='正文', start_time=10, end_time=30),
                      dict(title='结尾', start_time=30, end_time=70)]
    assert timeline['removed_chapters'] == 3
    assert timeline['removed_duration'] == 30
    assert rows[0]['title'] == '片头广告'


def test_first_chapter_is_not_forced_to_zero_if_source_has_no_opening_chapter():
    mapped, _ = map_items(_cuts(((0, 10),)), [dict(title='稍后开始', start_time=20, end_time=40)])
    assert mapped[0]['start_time'] == 10


def test_parts_use_their_own_success_snapshot_not_current_project_plan(make_project):
    uid = make_project()
    cut = _artifact(uid, 'cut', _cuts())
    source = _artifact(uid, 'source', None)
    rows = [dict(title='原片十秒', start_time=10, end_time=20)]
    options = dict(enabled=True, items=rows)
    before = copy.deepcopy(options)
    result = freeze_parts(uid, [cut, source], options)
    assert result[0]['chapter_items'][0]['start_time'] == 0
    assert result[1]['chapter_items'][0]['start_time'] == 10
    assert result[0]['chapter_timeline']['kind'] == 'cut'
    assert result[1]['chapter_timeline']['kind'] == 'source'
    assert options == before
    assert 'output_cuts' not in cut


def test_confirmed_part_keeps_frozen_cuts_when_success_record_is_replaced(make_project):
    uid = make_project()
    part = _artifact(uid, 'cut', _cuts())
    options = dict(enabled=True, items=[dict(title='开场', start_time=10, end_time=20)])
    frozen = freeze_parts(uid, [part], options)
    with crud.session() as session:
        record = session.get(AssemblyResult, f'{uid}:cut')
        record.snapshot = json.dumps({'output_cuts': None})
        session.add(record)
        session.commit()
    assert freeze_parts(uid, frozen, options) == frozen
    changed = freeze_parts(uid, frozen, dict(enabled=True, items=[dict(title='新标题', start_time=10, end_time=20)]), refresh=True)
    assert changed[0]['chapter_items'] == [dict(title='新标题', start_time=0, end_time=10)]


def test_old_record_hash_mismatch_cannot_get_another_files_timeline(make_project):
    uid = make_project()
    part = _artifact(uid, 'cut', _cuts())
    part['sha256'] = 'different-file'
    options = dict(enabled=True, items=[dict(title='开场', start_time=10, end_time=20)])
    with pytest.raises(PublishError) as exc:
        freeze_parts(uid, [part], options)
    assert exc.value.code == 'PUBLISH_CHAPTER_TIMELINE_INVALID'
    result = freeze_parts(uid, [part], options, strict=False)
    assert result[0]['chapter_items'] == []
    assert result[0]['chapter_timeline']['kind'] == 'unavailable'


def test_old_publication_detail_backfills_chapters_and_safe_uploaded_cid_without_writing(make_project):
    from dubforge.publishing.models import Publication
    from dubforge.publishing.service import view
    uid = make_project()
    part = _artifact(uid, 'cut', _cuts())
    original = dict(parts=[part], settings={}, origins={}, chapter_options=dict(enabled=True,
                    items=[dict(title='正文', start_time=30, end_time=50)]))
    with crud.session() as session:
        session.add(Publication(id='old-published', project_uid=uid, status='published',
            snapshot=json.dumps(original), transfers=json.dumps({str(part['artifact_id']):
                dict(cid=12345, filename='不可暴露上传令牌')})))
        session.commit()
    result = view('old-published')
    assert result['snapshot'] == original
    detail = result['chapter_parts'][0]
    assert detail['chapter_cid'] == '12345'
    assert detail['chapter_items'] == [dict(title='正文', start_time=10, end_time=30)]
    assert 'filename' not in detail
    with crud.session() as session:
        assert json.loads(session.get(Publication, 'old-published').snapshot) == original


def test_version_output_without_success_snapshot_is_not_assumed_uncut(make_project):
    uid = make_project()
    part = _artifact(uid, 'orphan', _cuts())
    with crud.session() as session:
        session.delete(session.get(AssemblyResult, f'{uid}:orphan'))
        session.commit()
    result = freeze_parts(uid, [part], dict(enabled=True, items=[]), strict=False)
    assert result[0]['chapter_timeline']['kind'] == 'unavailable'


@pytest.mark.parametrize('changes', [dict(output_duration=99), dict(source_duration=float('nan')),
                                   dict(ranges=[dict(start=20, end=30), dict(start=10, end=15)])])
def test_invalid_cut_snapshot_is_not_silently_mapped(changes):
    with pytest.raises(ValueError):
        map_items({**_cuts(), **changes}, [])


def test_old_review_render_refuses_cut_success_snapshot():
    record = SimpleNamespace(snapshot=json.dumps({'output_cuts': _cuts()}))
    with pytest.raises(review_render.ReviewRenderError) as exc:
        review_render._snapshot(record)
    assert exc.value.code == 'REVIEW_RENDER_CUTS_UNSUPPORTED'
    assert '合成成片' in exc.value.message


def test_old_review_render_refuses_current_cuts_before_using_old_mix(monkeypatch):
    from dubforge.pipeline import output_cuts
    monkeypatch.setattr(review_render, '_snapshot', lambda record: {})
    monkeypatch.setattr(output_cuts, 'snapshot', lambda deps: _cuts())
    with pytest.raises(review_render.ReviewRenderError) as exc:
        review_render._prepare(SimpleNamespace(), None, None, verify_hash=False)
    assert exc.value.code == 'REVIEW_RENDER_CUTS_UNSUPPORTED'
