"""恢复预检的两个真实消费者复用请求内排布；新依赖快照仍重新核验。"""
import copy
import json

from dubforge import workflow_plan
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, timing_repair, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.line_settings import identity
from dubforge.workflow_plan import production_checks


def test_recovery_consumers_share_one_layout_but_new_dependencies_read_again(make_project, monkeypatch):
    project = crud.get_project(make_project())
    rows = [dict(id=index + 1, text=f'测试译文{index + 1}', source_text='fixture',
        start=float(index * 3 + 2), end=float(index * 3 + 3.5), source='dub', take=1)
        for index in range(3)]
    deps = Dependencies(project)
    for row in rows:
        path = dubbing_edit.take_path(deps.root, row['id'], 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'isolated-duration-reader-fixture')
        path.with_suffix('.take.json').write_text(json.dumps(dict(identity=identity(row),
            version=dubbing_edit.file_version(path))), encoding='utf-8')

    duration_reads, layout_builds = [], []
    def seconds(path):
        assert path.is_relative_to(deps.root)
        duration_reads.append(path)
        return 1.0
    original = voice_timing._build_plan
    def build(*args, **kwargs):
        layout_builds.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(voice_timing, 'audio_seconds', seconds)
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 20.)
    monkeypatch.setattr(voice_timing, '_build_plan', build)
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda tool: f'fixture/{tool}')

    def check(current):
        monkeypatch.setattr(current, 'production_lines', lambda: copy.deepcopy(rows))
        inspection = timing_repair.inspect(current)
        issues = production_checks(current, ['assemble'], online=False,
            dubbing_summary={'deferred': False, 'generate': 0, 'repair': 0})
        assert inspection['repair_rows'] == [] and inspection['blocked'] == []
        assert inspection['unresolved_ids'] == [] and issues == []
        assert [(row['id'], row['seconds']) for row in inspection['timing']['lines']] == [
            (1, 1.0), (2, 1.0), (3, 1.0)]
        return inspection

    first = check(deps)
    assert len(duration_reads) == 3 and len(layout_builds) == 1
    fresh = Dependencies(crud.get_project(project.uid))
    assert fresh._voice_timing_cache is not deps._voice_timing_cache
    assert check(fresh) == first
    assert len(duration_reads) == 6 and len(layout_builds) == 2
