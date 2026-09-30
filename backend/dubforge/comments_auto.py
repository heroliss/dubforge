"""评论自动采集准备 (comments.mode=auto)：采集、评分、翻译并自动保存上屏方案。

工作流引擎（合成前独立步骤）与手动合成（步骤内自动准备）共用同一段逻辑；
挑选规则与人工确认完全一致（语言、文字长度、时间轴容量），零条可用即报错，
不猜测、不放空方案过关。用户通过显式选择 auto 模式授权机器挑选，
上屏内容仍可在评论精选页随时人工重审。
"""
from dubforge.domain import crud
from dubforge.errors import DubForgeError


def spec_from_config(uid) -> dict:
    """从项目/全局设置推导自动采集规格（与 run_spec 同源，不含发布字段）。"""
    from dubforge import configuration
    from dubforge.comment_studio import DEFAULT_SETTINGS
    values = configuration.resolve(uid)['values']
    return dict(comments_mode='auto', comment_limit=values['comments.limit'],
                comment_count=values['comments.count'], comment_prompt=values['comments.prompt'],
                comment_failure_policy=values['comments.failure_policy'],
                comment_settings={k: values['comments.' + k] for k in DEFAULT_SETTINGS})


def prepare_overlay(uid: str, spec: dict, *, cancel_check=None, extra_settings=()) -> tuple[list, int]:
    """执行自动采集并保存上屏方案；返回 (警告列表, 实际上屏条数)。

    cancel_check 在每个网络/LLM 阶段之间被调用；extra_settings 是各版本冻结的
    展示设置（如不同分P的 language_mode），候选必须同时满足才会入选。
    零条可用时抛 COMMENTS_AUTO_EMPTY。
    """
    cancel = cancel_check or (lambda: None)
    from dubforge import comment_studio, video_info
    cancel()
    video_info.update(uid, 'comments', comment_limit=spec['comment_limit'])
    cancel()
    warnings = []
    for action in ('filter', 'translate'):
        # 全部翻译仍服务于阅读评论；单条内容失败不再阻塞其他评论。
        result = comment_studio.run(uid, action, prompt=spec['comment_prompt'], limit=spec['comment_count'],
            failure_policy=spec.get('comment_failure_policy', 'skip'), cancel_check=cancel)
        warning = result.get('job', {}).get('warning') if isinstance(result, dict) else None
        if warning:
            warnings.append(('评分：' if action == 'filter' else '翻译：') + warning)
    state = comment_studio.view(uid)
    rows = {r['id']: r for r in state['rows']}
    assessments = state['filter'].get('assessments')
    candidates = sorted((item for item in assessments if item['eligible']), key=lambda item: -item['score']) \
        if assessments is not None else state['filter']['items']
    settings = spec['comment_settings']
    display_settings = [settings, *extra_settings]

    def usable(ident):
        row = rows.get(ident)
        return bool(row and all(not comment_studio.overlay_text_issues(
            {'display_text': row.get('text_zh', ''), 'source_text': row['text']}, current) for current in display_settings))

    usable_ids = [item['id'] for item in candidates if usable(item['id'])]
    excluded = len(candidates) - len(usable_ids)
    if excluded:
        warnings.append(f'已自动排除{excluded}条不满足评论方案或本次分P语言要求的候选（缺字或超长），按评分递补；完整评论与译文仍保留。')
    ids = usable_ids[:spec['comment_count']]
    from dubforge.pipeline.video_source import resolve_video
    from dubforge.publishing.service import _probe
    project = crud.get_project(uid)
    duration = _probe(resolve_video(project, crud.project_dir(uid)))
    fitting = max(0, int((duration - settings['start'] - settings['duration']) // settings['interval']) + 1)
    ids = ids[:fitting]
    if not ids:
        raise DubForgeError('没有可上屏评论：需满足当前语言、文字长度及时间设置；请检查逐条失败原因或手动确认评论方案',
                            code='COMMENTS_AUTO_EMPTY')
    if len(ids) < spec['comment_count']:
        warnings.append(f'本片实际使用{len(ids)}条评论：受有效评分、可用译文、文字长度或视频时长限制。')
    items = [dict(id=k, source_hash=rows[k]['source_hash'], display_text=rows[k]['text_zh'], source_text=rows[k]['text'])
             for k in ids]
    cancel()
    comment_studio.save(uid, dict(revision=state['overlay_revision'], settings=settings, items=items))
    return warnings, len(items)


def current_overlay(uid):
    """当前已保存的上屏方案（无已保存方案时为 None）。"""
    from dubforge import comment_studio
    project = crud.get_project(uid)
    if project is None:
        return None
    overlay = comment_studio.effective_overlay(uid, comment_studio._read(project).get('overlay'))
    return overlay or None


def ensure_render_specs(ctx, deps, specs) -> list:
    """comments_auto 合成任务的前置：仍无已确认方案时自动准备一份；随后带方案
    重新解析全部版本规格，让运行内指纹与实际渲染输入一致。"""
    uid = ctx.project.uid
    overlay = current_overlay(uid)
    if overlay and overlay.get('items'):
        ctx.log('复用已确认的评论上屏方案')
    else:
        spec = spec_from_config(uid)
        base = spec['comment_settings']
        extra = [{**base, 'language_mode': s['values'].get('comments.language_mode', base['language_mode'])}
                 for s in specs if s['include_comments']]
        ctx.log('评论准备为自动采集：正在采集、评分并翻译上屏评论…')
        warnings, count = prepare_overlay(uid, spec, cancel_check=ctx.check_cancel, extra_settings=extra)
        for warning in warnings:
            ctx.log(f'⚠ {warning}')
        ctx.log(f'已自动确认 {count} 条上屏评论')
        overlay = current_overlay(uid)
    from dubforge.pipeline import assembly_versions
    raw = {v['id']: v for v in assembly_versions.catalog(uid)['versions']}
    return [assembly_versions.resolve(deps, raw[s['id']], comment_plan=overlay) for s in specs]
