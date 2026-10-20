from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from dubforge.pipeline import virtual_preview as vp


def test_map_source_to_media_keeps_source_and_output_coordinates():
    cuts = {"source_duration": 20, "ranges": [{"start": 5, "end": 10}]}
    assert vp._map_source_to_media(cuts, 0, 15) == [
        (0, 5, 0, 5), (10, 15, 5, 10)
    ]


def test_map_source_to_media_rejects_deleted_only_interval():
    cuts = {"source_duration": 20, "ranges": [{"start": 5, "end": 10}]}
    assert vp._map_source_to_media(cuts, 6, 9) == []


def test_intervals_ignore_disabled_and_merge_adjacent():
    rows = [
        {"start": 1, "end": 2, "enabled": True},
        {"start": 2, "end": 4, "enabled": True},
        {"start": 8, "end": 9, "enabled": False},
    ]
    assert vp._intervals(rows, 10) == [{"start": 1.0, "end": 4.0}]


def test_intervals_reject_source_overflow():
    with pytest.raises(vp.VirtualPreviewError) as error:
        vp._intervals([{"start": 1, "end": 11}], 10)
    assert error.value.code == "VIRTUAL_PREVIEW_RANGES_INVALID"


def test_public_exposes_common_progress_contract_without_internal_handles():
    result = vp.public({"task_id": "abc", "status": "running", "stage": "encoding", "progress": 12.5,
                        "message": "正在编码", "root": "secret", "cancel": object(), "plan": {}})
    assert result["phase"] == "encoding"
    assert result["detail"] == "正在编码"
    assert result["percent"] == 12.5
    assert "root" not in result and "cancel" not in result and "plan" not in result


def test_snapshot_preserves_historical_source_duration_for_validation():
    snapshot = vp._snapshot({"source_duration": 9, "ranges": [{"start": 2, "end": 3}]}, 10)
    assert snapshot["source_duration"] == 9
    assert snapshot["output_duration"] == 8


def test_input_seek_offsets_skip_long_prefix_but_keep_one_second_margin():
    rows = [
        {"path": "source.mp4", "media_start": 1890.0},
        {"path": "source.mp4", "media_start": 1895.0},
        {"path": "donor.mp4", "media_start": 4.0},
    ]
    assert vp._input_seek_offsets(rows) == {"source.mp4": 1889.0, "donor.mp4": 3.0}


def test_immutable_cut_snapshot_requires_matching_artifact_duration(monkeypatch):
    deps = SimpleNamespace(project=SimpleNamespace(uid="project"), root=Path("project"))
    saved = {"output_cuts": {"source_fingerprint": "same", "source_duration": 20.0,
                             "ranges": [{"start": 5.0, "end": 10.0}]}}
    monkeypatch.setattr(vp, "_probe", lambda _path: {"duration": 15.0, 'frame_rate': '30000/1001'})
    cuts, meta = vp._validated_artifact_cuts(deps, Path("video.mp4"), saved, 20.0, "same")
    assert cuts["ranges"] == [{"start": 5.0, "end": 10.0}]
    assert meta["cut_snapshot_source"] == "immutable"
    assert meta['frame_rate'] == pytest.approx(30000 / 1001)


@pytest.mark.parametrize("fingerprint,actual_duration", [("other", 15.0), ("same", 20.0)])
def test_immutable_cut_snapshot_rejects_unproven_mapping(monkeypatch, fingerprint, actual_duration):
    deps = SimpleNamespace(project=SimpleNamespace(uid="project"), root=Path("project"))
    saved = {"output_cuts": {"source_fingerprint": "same", "source_duration": 20.0,
                             "ranges": [{"start": 5.0, "end": 10.0}]}}
    monkeypatch.setattr(vp, "_probe", lambda _path: {"duration": actual_duration})
    with pytest.raises(vp.VirtualPreviewError) as error:
        vp._validated_artifact_cuts(deps, Path("video.mp4"), saved, 20.0, fingerprint)
    assert error.value.code in {"VIRTUAL_PREVIEW_CUTS_SNAPSHOT_MISSING", "VIRTUAL_PREVIEW_CUTS_SNAPSHOT_MISMATCH"}


def _uncut_snapshot():
    return {"output_cuts": None, "sentence_inputs": {"duration": 20.0,
        "global_inputs": {"source": {"sha256": "source-hash", "size": 100}, "output_cuts": None}}}


def test_null_snapshot_means_uncut_artifact_even_after_draft_deletions(monkeypatch):
    deps = SimpleNamespace(project=SimpleNamespace(uid="project"), root=Path("project"))
    monkeypatch.setattr(vp, "resolve_video", lambda *_args: Path("source.mp4"))
    monkeypatch.setattr(vp, "_path_hash", lambda _path: (100, "source-hash"))
    monkeypatch.setattr(vp, "_probe", lambda _path: {"duration": 19.976})
    # 该接口不得读取草稿；曾把新删除清单误认为旧文件剪辑方案。
    monkeypatch.setattr(vp.output_cuts, "view", lambda *_args, **_kw: pytest.fail("不应读取当前删除草稿"))
    cuts, metadata = vp._validated_artifact_cuts(deps, Path("video.mp4"), _uncut_snapshot(), 20.0, "new-path-fp")
    assert cuts["ranges"] == []
    assert cuts["output_duration"] == 20.0
    assert metadata["cut_snapshot_source"] == "immutable_uncut"
    assert metadata["source_identity"] == "immutable_source_hash"


def test_same_duration_different_original_source_is_rejected(monkeypatch):
    deps = SimpleNamespace(project=SimpleNamespace(uid="project"), root=Path("project"))
    monkeypatch.setattr(vp, "resolve_video", lambda *_args: Path("source.mp4"))
    monkeypatch.setattr(vp, "_path_hash", lambda _path: (100, "different-source"))
    with pytest.raises(vp.VirtualPreviewError) as error:
        vp._validated_artifact_cuts(deps, Path("video.mp4"), _uncut_snapshot(), 20.0, "fp")
    assert error.value.code == "VIRTUAL_PREVIEW_BASE_CHANGED"


def test_missing_immutable_cut_evidence_cannot_use_current_plan(monkeypatch):
    deps = SimpleNamespace(project=SimpleNamespace(uid="project"), root=Path("project"))
    saved = _uncut_snapshot()
    saved.pop("output_cuts")
    saved["sentence_inputs"]["global_inputs"].pop("output_cuts")
    with pytest.raises(vp.VirtualPreviewError) as error:
        vp._validated_artifact_cuts(deps, Path("video.mp4"), saved, 20.0, "fp")
    assert error.value.code == "VIRTUAL_PREVIEW_CUTS_SNAPSHOT_MISSING"


def test_changed_path_fingerprint_is_allowed_if_immutable_source_bytes_match(monkeypatch):
    deps = SimpleNamespace(project=SimpleNamespace(uid="project"), root=Path("project"))
    saved = _uncut_snapshot()
    saved["output_cuts"] = {"source_duration": 20.0, "source_fingerprint": "old-path-fp",
                            "ranges": [{"start": 5.0, "end": 10.0}]}
    monkeypatch.setattr(vp, "resolve_video", lambda *_args: Path("source.mp4"))
    monkeypatch.setattr(vp, "_path_hash", lambda _path: (100, "source-hash"))
    monkeypatch.setattr(vp, "_probe", lambda _path: {"duration": 15.0})
    cuts, _meta = vp._validated_artifact_cuts(deps, Path("video.mp4"), saved, 20.0, "new-path-fp")
    assert cuts["source_fingerprint"] == "new-path-fp"
    assert saved["output_cuts"]["source_fingerprint"] == "old-path-fp"


@pytest.fixture()
def tiny_media(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("真实媒体回归需要 ffmpeg 和 ffprobe")
    monkeypatch.setenv("DUBFORGE_DATA_ROOT", str(tmp_path / "data"))
    paths = {}
    for name, color, duration in (("source", "red", 6), ("base", "blue", 4)):
        path = tmp_path / f"{name}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s=160x90:r=10:d={duration}", "-f", "lavfi", "-i",
                        f"sine=frequency=440:sample_rate=48000:duration={duration}",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
                       capture_output=True, check=True, timeout=15)
        paths[name] = path
    return tmp_path, paths


def test_real_preview_restores_missing_middle_from_source_and_finishes(tiny_media, monkeypatch):
    root, paths = tiny_media
    project = SimpleNamespace(uid="preview-fixture")
    monkeypatch.setattr(vp.crud, "get_project", lambda _uid: project)
    monkeypatch.setattr(vp.crud, "project_dir_by_project", lambda _project: root)
    monkeypatch.setattr(vp, "Dependencies", lambda _project: SimpleNamespace(root=root, project=project))
    monkeypatch.setattr(vp.output_cuts, "source_context", lambda _deps: {"duration": 6.0, "source_fingerprint": "same"})
    monkeypatch.setattr(vp, "resolve_video", lambda *_args: paths["source"])
    monkeypatch.setattr(vp, "_version_media", lambda *_args, **_kw: (paths["base"],
        {"source_duration": 6.0, "source_fingerprint": "same", "ranges": [{"start": 2.0, "end": 4.0}]},
        {"version_id": "base", "artifact_hash": "blue"}))
    task = vp.start(project.uid, {"version_id": "base", "draft": {"enabled": True, "ranges": []},
                                 "donor": {"kind": "source"}})
    task_id = task["task_id"]
    try:
        vp._tasks[task_id]["future"].result(timeout=20)
        result = vp.status(project.uid, task_id)
        assert result["status"] == "done", result
        assert result["stage"] == "ready"
        assert [row["media_kind"] for row in result["segments"]] == ["target", "source", "target"]
        assert [(row["media_start"], row["media_end"]) for row in result["segments"]] == [(0, 2), (2, 4), (2, 4)]
        output = vp._output_path(root, task_id)
        assert abs(vp._probe(output)["duration"] - 6.0) < .15
        for second, expected_channel in ((.5, 2), (2.5, 0), (4.5, 2)):
            pixel = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(second), "-i", str(output),
                                    "-vf", "scale=1:1", "-frames:v", "1", "-pix_fmt", "rgb24",
                                    "-f", "rawvideo", "pipe:1"], capture_output=True, check=True, timeout=10).stdout
            assert pixel[expected_channel] > 180
    finally:
        vp._tasks.pop(task_id, None)


def test_cancel_works_when_encoder_emits_no_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("DUBFORGE_DATA_ROOT", str(tmp_path / "data"))
    project = SimpleNamespace(uid="cancel-fixture")
    (tmp_path / 'source.mp4').write_bytes(b'fake-source-for-cancel-test')
    plan = {"root": tmp_path, "project": project, "source_fingerprint": "fp", "source_duration": 6,
            "duration": 6, "base": None, "donor": None, "source_validation": {},
            "preview_range": {"start": 0, "end": 6},
            "segments": [{"path": str(tmp_path / "source.mp4"), "media_start": 0, "media_end": 6}]}
    monkeypatch.setattr(vp, "_build_plan", lambda *_args: plan)
    monkeypatch.setattr(vp, "_probe", lambda _path: {"width": 160, "height": 90, "has_audio": False})
    started = threading.Event()
    real_popen = subprocess.Popen

    def silent_encoder(*_args, **kwargs):
        proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        started.set()
        return proc

    monkeypatch.setattr(vp, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": silent_encoder}))
    task = vp.start(project.uid, {})
    task_id = task["task_id"]
    try:
        assert started.wait(5)
        before = time.monotonic()
        vp.cancel(project.uid, task_id)
        vp._tasks[task_id]["future"].result(timeout=8)
        assert time.monotonic() - before < 6
        assert vp.status(project.uid, task_id)["status"] == "cancelled"
        assert not vp._output_path(tmp_path, task_id).exists()
    finally:
        vp._tasks.pop(task_id, None)
