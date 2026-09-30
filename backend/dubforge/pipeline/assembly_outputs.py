"""成品方案的名称、目标文件与选择校验，由计划和执行共用。"""
import re
import hashlib
import json
from pathlib import Path

from dubforge.errors import DubForgeError

VARIANT_LABELS = {
    "dub": "配音 · 不烧录字幕",
    "dub_hardsub": "配音 · 烧录字幕",
    "src_hardsub": "原声 · 烧录字幕",
    "src_comments": "原声 · 仅叠加评论",
    "dub_comments": "配音 · 不烧录字幕 · 叠加评论",
    "dub_hardsub_comments": "配音 · 烧录字幕 · 叠加评论",
    "src_hardsub_comments": "原声 · 烧录字幕 · 叠加评论",
}
OUTPUT_VARIANTS = tuple(VARIANT_LABELS)
DIMENSIONS = ('dubbing', 'burn_subtitles', 'include_comments')


def final_variant(name):
    for variant in OUTPUT_VARIANTS:
        if re.fullmatch(rf'final_{re.escape(variant)}_\d{{8}}_\d{{6}}_\d+\.mp4', name):
            return variant
    return 'legacy' if re.fullmatch(r'final_\d{8}_\d{6}_\d+\.mp4', name) else None


def current_output(output_dir: Path, variant: str) -> Path | None:
    """同组合复用最新成品的位置；兼容已有文件名，不删除升级前的历史文件。"""
    if variant not in OUTPUT_VARIANTS:
        raise DubForgeError('成品方案无效', code='ASSEMBLE_VARIANTS')
    pattern = re.compile(rf'final_{re.escape(variant)}_\d{{8}}_\d{{6}}_\d+\.mp4')
    files = (p for p in output_dir.glob(f'final_{variant}_*.mp4')
             if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink())
    return max(files, key=lambda p: p.name, default=None)


def dimensions(variant):
    """任务、依赖和渲染共用三个独立维度；原片不创建重复产物。"""
    return dict(zip(DIMENSIONS, (variant.startswith('dub'), 'hardsub' in variant, variant.endswith('_comments'))))


def resolve_outputs(variants=None, *, outputs=None, include_comments=False):
    """新接口逐方案声明三个布尔值；旧 variants＋整批评论开关只在入口转换。"""
    if type(include_comments) is not bool:
        raise DubForgeError('是否叠加评论必须是明确的勾选值', code='ASSEMBLE_VARIANTS')
    if outputs is not None:
        if variants is not None or include_comments or not isinstance(outputs, list):
            raise DubForgeError('请只提交逐方案的合成选项，不能混用旧版整批开关', code='ASSEMBLE_VARIANTS')
        keys = []
        for output in outputs:
            if (not isinstance(output, dict) or set(output) != set(DIMENSIONS)
                    or any(type(output[k]) is not bool for k in DIMENSIONS)):
                raise DubForgeError('每种方案必须明确选择配音、烧录字幕和叠加评论', code='ASSEMBLE_VARIANTS')
            key = ('dub' if output['dubbing'] else 'src') + ('_hardsub' if output['burn_subtitles'] else '') + ('_comments' if output['include_comments'] else '')
            if key == 'src':
                raise DubForgeError('三项都关闭时就是原片，无需合成，请直接使用源视频', code='ASSEMBLE_VARIANTS')
            keys.append(key)
        return validate_variants(keys)
    ordered = validate_variants(variants)
    if not include_comments and any(dimensions(v)['include_comments'] for v in ordered):
        raise DubForgeError('评论版需要明确启用已确认评论', code='COMMENTS_NOT_ENABLED')
    return list(dict.fromkeys(v if v.endswith('_comments') or not include_comments else v + '_comments' for v in ordered))


def validate_variants(raw) -> list[str]:
    if not isinstance(raw, list):
        raise DubForgeError("成片组合必须是列表", code="ASSEMBLE_VARIANTS")
    ordered = list(dict.fromkeys(str(item) for item in raw))
    if not ordered or any(item not in OUTPUT_VARIANTS for item in ordered):
        raise DubForgeError("请勾选至少一个有效的成品方案", code="ASSEMBLE_VARIANTS",
                            suggestion="配音、烧录字幕和叠加评论可独立组合；三项都关闭时直接使用原片。")
    return ordered


def default_variants(cfg) -> list[str]:
    """流程运行及旧版试片接口的已保存方案，不代表当前成品清单。"""
    return ["dub_hardsub" if cfg("assemble.burn_subtitles", False) else "dub"]


def default_description(cfg) -> str:
    return VARIANT_LABELS[default_variants(cfg)[0]]


def completed_outputs_state(deps):
    """展示已生成的方案，不把自选成品与默认方案混比；执行跳过仍用原来的严格判断。"""
    from sqlmodel import select
    from dubforge.domain import crud
    from dubforge.domain.models import Job
    from dubforge.runner import compute_fingerprint

    from dubforge.pipeline.assembly_versions import assembly_state
    if state := assembly_state(deps):
        return state

    step = deps.steps.get('assemble')
    fallback = {'fresh': deps.fresh('assemble'), 'label': '', 'reason': ''}
    if not step or step.status != 'done':
        return fallback
    task = None
    with crud.session() as session:
        # 只需最近的任务找到最后一次非预览运行; task_data 很大, 限制扫描量。
        jobs = session.exec(select(Job).where(Job.project_id == deps.project.id, Job.step_key == 'assemble')
                            .order_by(Job.id.desc()).limit(50))
        for job in jobs:
            try:
                candidate = json.loads(job.task_data or '{}')
            except (ValueError, TypeError):
                return fallback
            if not isinstance(candidate, dict):
                return fallback
            if candidate.get('scope') == 'preview':
                continue
            if job.status != 'done':
                return {'fresh': False, 'label': '正式合成未完成', 'reason': '最近一次正式合成尚未成功；试片成功不会替代正式成品状态。'}
            task = candidate
            break
    if task is None or task.get('scope', 'default') not in {'outputs', 'default'}:
        return fallback
    explicit = task.get('scope') == 'outputs'
    try:
        variants = validate_variants(task.get('variants')) if explicit else default_variants(deps.cfg)
    except DubForgeError:
        return fallback
    actual = deps.with_assembly_variants(variants)
    fingerprint = compute_fingerprint(deps.project, 'assemble', dependencies=actual)
    if task.get('production_fingerprint'):
        matches = fingerprint == task['production_fingerprint']
    else:
        # 兼容已存在的输出/评论专用指纹：只和当次任务的方案及评论快照比较。
        expected = compute_fingerprint(deps.project, 'assemble', dependencies=deps)
        if default_variants(deps.cfg)[0] not in variants:
            expected = 'outputs:' + fingerprint
            if task.get('comment_overlay'):
                expected = 'comments:' + hashlib.sha256((expected + json.dumps(task['comment_overlay'], sort_keys=True)).encode()).hexdigest()
        matches = expected == step.input_fingerprint
    if not matches or actual.unready('assemble'):
        return {'fresh': False, 'label': '需更新', 'reason': '已生成方案的输入或制作设置发生变化；可在成品列表选择需要更新的组合。'}
    latest = {}
    for artifact in actual.arts.values():
        variant = final_variant(artifact.name) if artifact.step_key == 'assemble' else None
        if variant in variants and (variant not in latest or (artifact.created_at, artifact.id) > (latest[variant].created_at, latest[variant].id)):
            latest[variant] = artifact
    missing = []
    for variant in variants:
        artifact = latest.get(variant)
        path = actual.path('assemble', artifact.name) if artifact else None
        try:
            available = path and path.is_file() and path.stat().st_size == artifact.size
        except OSError:
            available = False
        if not available:
            missing.append(VARIANT_LABELS[variant])
    if missing:
        return {'fresh': False, 'label': '成品文件缺失', 'reason': '上次生成方案有文件缺失或大小变化：' + '；'.join(missing)}
    return {'fresh': True, 'label': '已生成方案有效' if explicit else '已是最新',
            'reason': '上次生成的组合文件完整，输入与制作设置未变化。与流程默认组合不同不代表过期；旧版或其他组合的缺失单独标注。'}
