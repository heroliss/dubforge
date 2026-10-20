"""阶段仪表集成：真实步骤分支报告事实，不调用模型或外部服务。"""
from types import SimpleNamespace

from dubforge.pipeline import step_download, step_preprocess, step_subtitles


class Context:
    def __init__(self, pdir):
        self.pdir = pdir
        self.project = SimpleNamespace(uid='test', source_kind='youtube')
        self.stages, self.phases, self.files = [], [], []
        self.rebuild = False
    def set_progress_phase(self, label, **kwargs):
        self.phases.append((label, kwargs))
    def set_progress_detail(self, detail):
        self.stages.append(detail)
    def report_progress(self, *args):
        pass
    def log(self, *args):
        pass
    def cfg(self, key, default=None):
        return False if key == 'separate.enabled' else default
    def upstream(self, *args):
        return self.pdir / 'video.mp4'
    def register_file(self, *args, **kwargs):
        self.files.append(args[0])
    def write_json(self, *args):
        self.files.append(args[0])
    def check_cancel(self):
        pass


def test_preprocess_cached_audio_and_disabled_separation_have_distinct_stages(tmp_path):
    ctx = Context(tmp_path)
    audio = tmp_path / 'audio'
    audio.mkdir()
    (audio / 'raw.wav').write_bytes(b'existing validated fixture')
    step_preprocess.run(ctx)
    assert [row['phase'] for row in ctx.stages] == ['checking_audio', 'reuse_audio', 'separation_disabled']
    assert ctx.stages[1]['reused'] is True
    assert ctx.stages[2]['state'] == 'skipped'
    assert ctx.files == ['raw.wav', 'separate.json']


def test_download_summary_and_save_are_separate_from_download_completion(tmp_path, monkeypatch):
    ctx = Context(tmp_path)
    ctx.artifact_dir = lambda: tmp_path
    monkeypatch.setattr(step_download, '_build_video_info', lambda *a, **kw: {'summary_status': 'generated'})
    monkeypatch.setattr('dubforge.project_titles.sync', lambda *a: False)
    result = step_download._ensure_video_info(ctx, 'youtube', 'title', 'https://example.test/video', refresh=True)
    assert result['summary_status'] == 'generated'
    assert [row['phase'] for row in ctx.stages] == ['metadata', 'saving_metadata']
    assert all(row['completed'] is None and row['total'] is None for row in ctx.stages)


def test_subtitle_export_reports_real_file_count_and_preserves_outputs(tmp_path, monkeypatch):
    ctx = Context(tmp_path)
    document = [{'edited': False}]
    monkeypatch.setattr(step_subtitles, 'build_tracks', lambda _: ({'source.srt': [], 'trans.srt': []}, document))
    writes = []
    monkeypatch.setattr(step_subtitles, 'write_srt', lambda rows, path: writes.append(path.name))
    step_subtitles.run(ctx)
    saving = [row for row in ctx.stages if row['phase'] == 'saving_subtitles']
    assert [(row['completed'], row['total'], row['unit']) for row in saving] == [(0, 2, '文件'), (1, 2, '文件'), (2, 2, '文件')]
    assert writes == ['source.srt', 'trans.srt'] and ctx.files[-1] == 'subtitles.json'


def test_asr_load_and_wait_are_unknown_until_real_segments_arrive(tmp_path, monkeypatch):
    import sys
    from dubforge.pipeline import step_transcribe
    from dubforge import config
    ctx = Context(tmp_path)
    ctx.check_pause = lambda: None
    monkeypatch.setattr(config, 'ensure_hf_env', lambda: None)
    monkeypatch.setattr('dubforge.environment.require_component', lambda *a: None)
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    segment = SimpleNamespace(end=3., words=[SimpleNamespace(start=1., end=3., word='Hello.')])
    model = SimpleNamespace(transcribe=lambda *a, **kw: (iter([segment]), SimpleNamespace(duration=10., language='en')))
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=lambda *a, **kw: model))
    monkeypatch.setattr(step_transcribe, 'write_srt', lambda *a: None)
    step_transcribe.run(ctx)
    states = {row['phase']: row for row in ctx.stages}
    assert states['loading_asr_model']['completed'] is None
    assert states['waiting_first_segment']['completed'] is None
    assert (states['transcribing']['completed'], states['transcribing']['total']) == (3., 10.)
    assert states['saving']['completed'] == 1
