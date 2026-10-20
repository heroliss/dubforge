"""真实 ASS 在深层目录仍可烧录，且字幕/评论画面与原读取路径一致。"""
import subprocess

import pytest

from dubforge import comment_studio
from dubforge.pipeline import comment_overlay, subtitle_style
from dubforge.utils.srt import SubLine


def _frames(folder, graph):
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1',
               '-f', 'lavfi', '-i', 'color=c=black:size=640x360:rate=10:duration=1.6']
    if graph:
        command += ['-vf', graph]
    command += ['-frames:v', '16', '-an', '-f', 'framemd5', '-']
    result = subprocess.run(command, cwd=folder, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stderr
    rows = [line.rsplit(',', 1)[-1].strip() for line in result.stdout.splitlines()
            if line and not line.startswith('#')]
    assert len(rows) == 16
    return rows


@pytest.mark.parametrize('kind', ['subtitle', 'comment', 'combined'])
def test_visible_overlays_keep_pixels_and_timing_in_long_directories(isolated_env, tmp_path, kind):
    short = tmp_path / 'short'
    short.mkdir()
    long = tmp_path / "项目 有空格 ' [方括号],;=引号"
    # Windows 的进程 cwd 另有长度边界；本回归精确覆盖 cwd 可用、ASS 文件超过260字符的情况。
    padding = 248 - len(str(long)) - 1
    assert 0 < padding < 255
    long /= ('nested-render-' * 20)[:padding]
    long.mkdir(parents=True)
    settings = {**subtitle_style.STYLE_DEFAULTS,
                'assemble.subtitle_font': 'Arial', 'assemble.subtitle_size': 56,
                'assemble.subtitle_source_style_enabled': True,
                'assemble.subtitle_source_font': 'Arial', 'assemble.subtitle_source_size': 42,
                'assemble.subtitle_source_color': '#00FF00'}
    cfg = lambda key, default=None: settings.get(key, default)
    line = SubLine(1, .4, 1.4, '原文与译文')
    line.parts = (('source', 'Original subtitle'), ('trans', '中文译文'))
    overlay = dict(settings=comment_studio.validate_settings(dict(
        start=.3, duration=1., interval=2., show_author=False, position='top',
        font='Arial', font_size=40, min_font_size=24, color='#FF0000',
        outline=0, background_opacity=0)), items=[dict(display_text='VISIBLE COMMENT 可见评论')])

    def prepare(folder):
        graphs, documents = [], []
        if kind in {'subtitle', 'combined'}:
            graphs.append(subtitle_style.prepare_subtitle_lines([line], cfg, (640, 360), folder, name='subtitle'))
            documents.append(folder / 'subtitle.ass')
        if kind in {'comment', 'combined'}:
            graph, document = comment_overlay.prepare_filter(overlay, None, folder, canvas=(640, 360))
            graphs.append(graph)
            documents.append(document)
        assert all(graph.startswith('ass=') for graph in graphs)
        assert all('Dialogue:' in path.read_text(encoding='utf-8-sig') for path in documents)
        return ','.join(graphs), documents

    short_graph, short_documents = prepare(short)
    long_graph, long_documents = prepare(long)
    assert [path.read_bytes() for path in short_documents] == [path.read_bytes() for path in long_documents]
    old_graph = []
    for index, path in enumerate(short_documents):
        legacy = short / f'legacy-{index}.ass'
        legacy.write_bytes(path.read_bytes())
        old_graph.append('ass=' + legacy.name)
    blank = _frames(short, '')
    reference = _frames(short, ','.join(old_graph))
    current = _frames(short, short_graph)
    deep = _frames(long, long_graph)
    assert len(str(long_documents[0])) > 260
    assert reference == current == deep
    assert reference[0] == blank[0]
    assert reference[5] != blank[5]  # 明确位于字幕和评论可见窗口内，不能让全黑帧冒充样式保真。
    assert any(frame != background for frame, background in zip(reference, blank, strict=True))
    assert not list(tmp_path.rglob('*.tmp'))


@pytest.mark.parametrize('value,expected', [
    (r'C:\video\subtitle.ass', r'\\?\C:\video\subtitle.ass'),
    (r'\\server\share\subtitle.ass', r'\\?\UNC\server\share\subtitle.ass'),
    (r'\\?\C:\video\subtitle.ass', r'\\?\C:\video\subtitle.ass'),
    (r'\\?\UNC\server\share\subtitle.ass', r'\\?\UNC\server\share\subtitle.ass'),
])
def test_windows_extended_path_is_canonical_without_accessing_unc(value, expected):
    from dubforge.utils.ffmpeg import _extended_windows_path
    assert _extended_windows_path(value) == expected


@pytest.mark.parametrize('value', ['bad;filter.ass', 'bad,filter.ass', 'not-an-ass.txt'])
def test_ass_filter_rejects_noninternal_filenames(value):
    from dubforge.errors import DubForgeError
    from dubforge.utils.ffmpeg import ass_file_filter
    with pytest.raises(DubForgeError) as invalid:
        ass_file_filter(value)
    assert invalid.value.code == 'SUBTITLE_FILE_INVALID'
