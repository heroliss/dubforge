"""原稿更新进度投影：阶段细节和总体百分比必须连续、兼容旧结构。"""
from types import SimpleNamespace

from dubforge.publishing import updates


def _row(execution, *, progress=0):
    return SimpleNamespace(
        id='update-1', source_publication_id='publication-1', project_uid='project-1',
        bvid='BV1', account_id='account-1', status='uploading', phase='上传修订视频',
        fingerprint='fingerprint', plan='{}', preview='{}', execution=updates.json.dumps(execution),
        error='', error_code='', progress=progress, created_at=updates.now(),
        updated_at=updates.now(),
    )


def test_stage_progress_includes_partial_upload_and_preserves_stage_map():
    execution = {
        'stages': {
            'upload': {'status': 'running', 'progress': 37.5, 'label': '上传分P'},
            'edit': {'status': 'pending'},
            'review': {'status': 'pending'},
            'chapters': {'status': 'pending'},
            'collection': {'status': 'pending'},
            'verify': {'status': 'pending'},
        },
        'phase': '上传修订视频：P1',
    }
    detail = updates._stage_progress(execution)
    assert detail['progress'] == 6.2
    assert detail['current_stage'] == 'upload'
    public = updates._public(_row(execution))
    assert isinstance(public['execution']['stages'], dict)
    assert public['execution']['stages']['upload']['progress'] == 37.5
    assert public['progress'] == 6.2
    assert public['execution']['progress_detail']['total_stages'] == 6


def test_completed_and_skipped_stages_are_full_progress_even_without_saved_value():
    execution = {'stages': {
        'upload': {'status': 'skipped'}, 'edit': {'status': 'completed'},
        'review': {'status': 'waiting'}, 'chapters': {'status': 'pending'},
        'collection': {'status': 'pending'}, 'verify': {'status': 'pending'},
    }}
    detail = updates._stage_progress(execution)
    assert detail['stages'][0]['progress'] == 100
    assert detail['stages'][1]['progress'] == 100
    assert detail['progress'] == 20.0


def test_progress_summary_keeps_project_polling_alive_for_legacy_bad_snapshot():
    row = _row({}, progress=12)
    row.execution = '{broken-json'
    summary = updates.progress_summary(row)
    assert summary['progress'] == 12
    assert summary['execution']['total_stages'] == 6
