"""已有评论成片按实际生成快照核对，不借用当前工作区猜历史画面。"""
import copy
import json
from types import SimpleNamespace

import pytest

from dubforge import comment_studio, config, content_review_service
from dubforge.domain import crud
from test_configuration import save
from dubforge.pipeline import assembly_versions, content_redaction, version_render
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.fs import atomic_write_bytes
from test_content_redaction import MediaContext, _media, _seed_text

pytestmark = pytest.mark.usefixtures('isolated_env')


def _safe_project(uid):
    return _seed_text(uid, source=[dict(id=1, start=0.5, end=2.5, text='Welcome to this beautiful city')],
                      target={1: '欢迎来到美丽的城市'})


def _item(**values):
    return {'id': 'c1', 'source_hash': 'old-source', 'text': 'Beautiful architecture',
            'source_text': 'Beautiful architecture', 'display_text': '建筑很漂亮', **values}


def _overlay(items):
    return dict(settings={**comment_studio.DEFAULT_SETTINGS, 'font': 'Arial', 'font_size': 18,
                          'start': 0.1, 'interval': 1, 'duration': 1, 'margin': 10,
                          'show_author': False}, items=items)


def _output(project, root, snapshot=None, name='final_version_comments.mp4'):
    path = root / 'output' / name
    atomic_write_bytes(path, b'isolated historical video')
    crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
    artifact = crud.latest_artifacts(project.id, ['assemble'])['assemble/' + path.name]
    if snapshot is not None:
        assembly_versions.record_result(SimpleNamespace(project=project), {'id': 'history', **snapshot},
                                        path.name, 'historical-fingerprint')
    return artifact


@pytest.mark.parametrize('field', ['text', 'source_text', 'display_text', 'text_zh'])
def test_historical_comment_match_blocks_without_any_speech_range(make_project, field):
    project, root, _ = _safe_project(make_project())
    item = _item()
    item[field] = 'the tragedy of Tiananmen Square'
    artifact = _output(project, root, dict(include_comments=True, comment_overlay=_overlay([item])))
    assert content_redaction.plan(Dependencies(project))['ranges'] == []
    with pytest.raises(content_redaction.ContentRedactionError, match='已烧录的评论') as error:
        content_review_service.assert_snapshot(dict(project_uid=project.uid, settings={'title': '城市旅行'},
            parts=[dict(artifact_id=artifact.id, sha256=artifact.sha256, title='旅行')]))
    assert error.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    state = content_review_service.candidate_states(project, [artifact])[artifact.id]
    assert state['review_ready'] is False and '评论' in state['review_reason']


def test_current_filtered_overlay_cannot_clear_risk_in_old_video(make_project):
    project, root, _ = _safe_project(make_project())
    risky = _item()
    risky['display_text'] = '自定义屏蔽词'
    artifact = _output(project, root, dict(include_comments=True, comment_overlay=_overlay([risky]),
        content_review_receipt=content_redaction.receipt(Dependencies(project))))
    # 旧文件生成后新增屏蔽词，并确认一个已清理的当前方案；旧文件的像素并未改变。
    save('project:' + project.uid, {'content_review.blocked_terms': '自定义屏蔽词'})
    comment_studio._write(project, {'overlay': _overlay([_item()])})
    assert not comment_studio.view(project.uid)['overlay']['items'][0].get('review_blocked')
    with pytest.raises(content_redaction.ContentRedactionError, match='修改当前评论方案不会改变旧文件'):
        content_review_service.assert_publishable(project, artifact)


def test_sensitive_location_and_terror_event_comment_blocks_old_snapshot_and_new_overlay(make_project):
    project, root, _ = _safe_project(make_project())
    risky = _item(text='The square is the most sensitive place in the country; terrorism happened there.',
                  display_text='那里是全国最敏感的地方，确实发生过恐怖袭击。')
    safe = _item(id='safe')
    artifact = _output(project, root, dict(include_comments=True, comment_overlay=_overlay([risky, safe])))
    # 当前编辑区已经移除该条，仍需拒绝尚未重生成的历史视频。
    comment_studio._write(project, {'overlay': _overlay([safe])})
    with pytest.raises(content_redaction.ContentRedactionError, match='已烧录的评论'):
        content_review_service.assert_publishable(project, artifact)
    filtered = content_redaction.safe_overlay(Dependencies(project), _overlay([risky, safe]))
    assert [item['id'] for item in filtered['items']] == ['safe']
    # 评论专用规则不把普通字幕强制扩大为声音处理区间。
    assert content_redaction.plan(Dependencies(project))['ranges'] == []


def test_safe_historical_comment_does_not_require_unrelated_new_receipt(make_project):
    project, root, _ = _safe_project(make_project())
    item = _item()
    item.update(text='A lovely visit to Tiananmen Square', display_text='天安门广场的旅游风景')
    artifact = _output(project, root, dict(include_comments=True, comment_overlay=_overlay([item])))
    content_review_service.assert_publishable(project, artifact)
    state = content_review_service.candidate_states(project, [artifact])[artifact.id]
    assert state['review_ready'] is None and state['review_status'] == '未命中本地规则'


@pytest.mark.parametrize('record', [False, True])
def test_outputs_without_comments_are_not_blocked_by_current_comment_workspace(make_project, record):
    project, root, _ = _safe_project(make_project())
    item = _item()
    item['display_text'] = '天安门广场的悲剧'
    comment_studio._write(project, {'overlay': _overlay([item])})
    artifact = _output(project, root, dict(include_comments=False, comment_overlay=None) if record else None,
                       name='final_dub_20261005_100000_000001.mp4')
    content_review_service.assert_publishable(project, artifact)
    assert content_review_service.candidate_states(project, [artifact])[artifact.id]['review_ready'] is None


@pytest.mark.parametrize('kind', ['missing', 'wrong_hash', 'missing_overlay', 'malformed'])
def test_legacy_comment_output_requires_exact_historical_snapshot(make_project, kind):
    project, root, _ = _safe_project(make_project())
    snapshot = None if kind == 'missing' else dict(include_comments=True, comment_overlay=_overlay([_item()]))
    if kind == 'missing_overlay':
        snapshot.pop('comment_overlay')
    if kind == 'malformed':
        snapshot['comment_overlay'] = {'items': '无效快照'}
    artifact = _output(project, root, snapshot, name='final_src_comments_20261005_100000_000001.mp4')
    if kind == 'wrong_hash':
        with crud.session() as session:
            row = session.get(type(assembly_versions.results(project.uid)['history']), project.uid + ':history')
            row.artifact_hash = 'different-file'
            session.add(row)
            session.commit()
    with pytest.raises(content_redaction.ContentRedactionError, match='生成快照'):
        content_review_service.assert_publishable(project, artifact)
    assert content_review_service.candidate_states(project, [artifact])[artifact.id]['review_ready'] is False


def test_version_style_legacy_comment_filename_requires_historical_snapshot(make_project):
    project, root, _ = _safe_project(make_project())
    artifact = _output(project, root, name='final_version_legacy-src_hardsub_comments.mp4')
    with pytest.raises(content_redaction.ContentRedactionError, match='生成快照'):
        content_review_service.assert_publishable(project, artifact)
    assert content_review_service.candidate_states(project, [artifact])[artifact.id]['review_ready'] is False


def test_known_comment_record_with_mismatched_hash_blocks_arbitrary_version_filename(make_project):
    project, root, _ = _safe_project(make_project())
    artifact = _output(project, root, dict(include_comments=True, comment_overlay=_overlay([_item()])),
                       name='final_version_travel.mp4')
    with crud.session() as session:
        record = assembly_versions.results(project.uid)['history']
        record.artifact_hash = 'old-output-hash'
        session.merge(record)
        session.commit()
    with pytest.raises(content_redaction.ContentRedactionError, match='生成快照'):
        content_review_service.assert_publishable(project, artifact)
    assert content_review_service.candidate_states(project, [artifact])[artifact.id]['review_ready'] is False


def test_exact_no_comment_snapshot_overrides_legacy_name_and_unrelated_old_record(make_project):
    from dubforge.pipeline.assembly_models import AssemblyResult
    project, root, _ = _safe_project(make_project())
    artifact = _output(project, root, dict(include_comments=False, comment_overlay=None),
                       name='final_version_legacy-src_hardsub_comments.mp4')
    with crud.session() as session:
        session.add(AssemblyResult(key=project.uid + ':old-comment', version_id='old-comment',
            project_uid=project.uid, artifact_id=artifact.id, artifact_hash='old-comment-file', fingerprint='old',
            snapshot=json.dumps(dict(include_comments=True, comment_overlay=_overlay([_item()])))))
        session.commit()
    assert len(assembly_versions.results(project.uid)) == 2
    content_review_service.assert_publishable(project, artifact)
    assert content_review_service.candidate_states(project, [artifact])[artifact.id]['review_ready'] is None


def test_explicit_review_disable_keeps_publication_choice_available(make_project):
    project, root, _ = _safe_project(make_project())
    artifact = _output(project, root, name='final_src_comments_20261005_100000_000001.mp4')
    save('project:' + project.uid, {'content_review.enabled': False})
    content_review_service.assert_publishable(project, artifact)
    assert content_review_service.candidate_states(project, [artifact])[artifact.id]['review_status'] == '审查关闭'


@pytest.mark.parametrize('field', ['source_text', 'display_text'])
def test_report_counts_edited_comment_fields_and_deduplicates_overlay(make_project, field):
    project, root, _ = _safe_project(make_project())
    atomic_write_bytes(root / 'artifacts/download/video_info.json', json.dumps({
        'comments': {'items': [dict(id='c1', text='Beautiful architecture', author='观众')]}}).encode())
    item = {**comment_studio._rows(project)[0], 'display_text': '建筑很漂亮', 'source_text': 'Beautiful architecture'}
    item[field] = '天安门广场的悲剧'
    comment_studio._write(project, {'overlay': _overlay([item])})
    report = content_review_service.report(project.uid)
    assert report['summary']['blocked_comments'] == 1
    assert [finding['id'] for finding in report['comment_findings']] == ['c1']
    assert report['comment_findings'][0]['source_text' if field == 'source_text' else 'text'] == item[field]


def test_report_keeps_unsafe_saved_comment_when_original_collection_no_longer_contains_it(make_project):
    project, _, _ = _safe_project(make_project())
    item = _item()
    item['display_text'] = '天安门广场的悲剧'
    comment_studio._write(project, {'overlay': _overlay([item])})
    report = content_review_service.report(project.uid)
    assert report['summary']['blocked_comments'] == 1
    assert report['comment_findings'][0]['text'] == item['display_text']


def test_new_render_snapshot_contains_only_actual_filtered_comment_plan(make_project, monkeypatch):
    project, root, paths = _safe_project(make_project())
    ctx = MediaContext(project, root, paths)
    video, _, _ = _media(ctx)
    crud.add_artifact(project.id, 'download', 'video', video.name, video, 'media/video.mp4')
    risky, safe = _item(), _item()
    risky.update(id='risky', text='the tragedy of Tiananmen Square', display_text='普通译文')
    safe['id'] = 'safe'
    overlay = _overlay([risky, safe])
    original = copy.deepcopy(overlay)
    version = assembly_versions.resolve(Dependencies(project), dict(id='comment-review', name='评论处理版',
        dubbing=False, burn_subtitles=False, include_comments=True, overrides={}), comment_plan=overlay)
    resolved_original = copy.deepcopy(version['comment_overlay'])
    ctx.task_data = {'version_specs': [version]}
    from dubforge.pipeline import step_assemble
    mux, used = step_assemble._mux_original_sub, []

    def capture(*args, **kwargs):
        used.append(copy.deepcopy(kwargs['comment_plan']))
        return mux(*args, **kwargs)

    monkeypatch.setattr(step_assemble, '_mux_original_sub', capture)
    version_render.run(ctx)
    stored = json.loads(assembly_versions.results(project.uid)['comment-review'].snapshot)
    assert [item['id'] for item in stored['comment_overlay']['items']] == ['safe']
    assert stored['comment_overlay'] == used[0]
    assert overlay == original and version['comment_overlay'] == resolved_original
    artifact = crud.latest_artifacts(project.id, ['assemble'])['assemble/final_version_comment-review.mp4']
    assert (root / artifact.rel_path).is_file()
    content_review_service.assert_publishable(project, artifact)
