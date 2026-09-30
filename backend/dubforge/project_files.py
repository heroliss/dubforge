"""项目文件的只读清单与用途说明；不读取文件正文。"""
import fnmatch
import re
from pathlib import Path

from dubforge.pipeline.registry import STEPS
from dubforge.pipeline.assembly_outputs import final_variant, VARIANT_LABELS


def description(relative_path):
    path = Path(relative_path)
    name = path.name
    variant = final_variant(name)
    if variant:
        return '正式成品 · ' + VARIANT_LABELS.get(variant, '旧版成片')
    if name.startswith('preview_'):
        return '预览试片' if path.suffix == '.mp4' else '预览音轨'
    if name.startswith('peaks_'):
        return '音频波形预览缓存'
    if name.endswith('.tmp') or name.startswith(('.tmp_', '.pending_')):
        return '处理中临时文件'
    if name.endswith('.llm.jsonl'):
        return 'AI 请求与响应日志'
    if path.suffix == '.log':
        return '运行日志'
    if name == 'manifest.json':
        return '项目索引与配置快照（用于恢复项目）'
    if name == 'comment_studio.json':
        return '评论评分、译文与已确认的上屏方案'
    if name.endswith('.take.json'):
        return '逐句配音版本与缓存信息'
    if name == 'dub_full.wav':
        return '铺轨后的完整配音音轨'
    if path.parent.as_posix() == 'artifacts/tts' and re.fullmatch(r'\d+(?:_v\d+)?\.wav', name):
        return '逐句配音音频' + (' · 备选版本' if '_v' in name else '')
    for step in STEPS:
        if path.parent.as_posix() in {step.dir, f'artifacts/{step.key}'}:
            for pattern, desc in step.outputs:
                if fnmatch.fnmatch(name, pattern):
                    return desc
    if path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}:
        return '封面或预览图片'
    if path.parts and path.parts[0] == 'media':
        return '源视频与平台资料'
    if 'comments' in path.as_posix():
        return '观众评论、评分译文或上屏方案'
    if path.suffix == '.srt':
        return '字幕文件'
    if path.suffix == '.json':
        return '项目工作数据'
    return '项目辅助文件'


def list_files(root, limit=2000):
    root = root.resolve()
    entries = []
    if root.is_dir():
        for path in sorted(root.rglob('*')):
            try:
                # 不通过目录连接展示工程外的文件；扫描期间删除文件也不影响整张表。
                if not path.resolve().is_relative_to(root) or not path.is_file():
                    continue
                info = path.stat()
                relative = str(path.relative_to(root))
                entries.append(dict(file=relative, path=str(path), description=description(relative),
                                    size=info.st_size, mtime=info.st_mtime, ctime=info.st_ctime))
                if len(entries) >= limit:
                    break
            except OSError:
                continue
    return {'dir': str(root), 'entries': entries}
