"""评论自动采集准备 (comments.mode=auto)：采集、评分、翻译并自动保存上屏方案。

工作流引擎（合成前独立步骤）与手动合成（步骤内自动准备）共用同一段逻辑；
挑选规则与人工确认完全一致（语言、文字长度、时间轴容量），零条可用即报错，
不猜测、不放空方案过关。用户通过显式选择 auto 模式授权机器挑选，
上屏内容仍可在评论精选页随时人工重审。
"""
from dubforge.domain import crud
from dubforge.errors import DubForgeError


def _report_stage(ctx, label):
    """阶段的实时计数由评论工作室提供，这里仅标记当前自动准备活动。"""
    from dubforge.execution_progress import report_stage
    report_stage(ctx, 'preparing_comments', label)


def spec_from_config(uid) -> dict:
    """从项目/全局设置推导自动采集规格（与 run_spec 同源，不含发布字段）。"""
    from dubforge import configuration
    from dubforge.comment_studio import DEFAULT_SETTINGS
    values = configuration.resolve(uid)['values']
    return dict(comments_mode='auto', comment_limit=values['comments.limit'],
                comment_count=values['comments.count'], comment_prompt=values['comments.prompt'],
                comment_failure_policy=values['comments.failure_policy'],
                comment_settings={k: values['comments.' + k] for k in DEFAULT_SETTINGS})


def prepare_overlay(uid: str, spec: dict, *, cancel_check=None, extra_settings=(), log=None, phase=None) -> tuple[list, int]:
    """执行自动采集并保存上屏方案；返回 (警告列表, 实际上屏条数)。

    cancel_check 在每个网络/LLM 阶段之间被调用；extra_settings 是各版本冻结的
    展示设置（如不同分P的 language_mode），候选必须同时满足才会入选。
    零条可用时抛 COMMENTS_AUTO_EMPTY。
    """
    cancel = cancel_check or (lambda: None)
    write_log = log or (lambda _message: None)
    report_phase = phase or (lambda _message: None)
    from dubforge import comment_studio, video_info, configuration
    cancel()
    report_phase('评论采集：检查可复用资料，缺少或范围扩大时获取')
    write_log('评论准备：检查已采集评论，缺少有效快照或采集范围扩大时再获取…')
    snapshot = video_info.ensure_comments(uid, comment_limit=spec['comment_limit'],
                                          **({'cancel_check': cancel_check} if cancel_check else {}))
    source_ids = snapshot['ids']
    if snapshot['reused']:
        write_log(f'复用 {snapshot["collected_at"]} 已采集的 {snapshot["available"]} 条评论，本次处理前 {snapshot["count"]} 条；没有重新联网获取。')
    else:
        write_log(f'评论采集完成：本次获取 {snapshot["count"]} 条，采集上限 {snapshot["requested_limit"]} 条。')
    cancel()
    if not source_ids:
        raise DubForgeError('没有可上屏评论：已获取评论，但没有可用正文；请手工选择其他来源或调整评论版本', code='COMMENTS_AUTO_EMPTY')
    settings = spec['comment_settings']
    display_settings = list(extra_settings) or [settings]
    from dubforge.pipeline.video_source import resolve_video
    from dubforge.publishing.service import _probe
    from dubforge.pipeline import comment_overlay
    from dubforge import comment_preparation
    project = crud.get_project(uid)
    video = resolve_video(project, crud.project_dir(uid))
    duration = _probe(video)
    # 自动流程与评论页面共用同一准备器；这里只提供成片的真实容量与画幅。
    # 缓存、失败递补和人工选择保护不能因入口不同而出现两套规则。
    if video.is_file():
        try:
            canvas = comment_overlay.video_canvas(video)
        except DubForgeError:
            # 正式渲染入口仍会核对真实画幅，探测暂不可用时不丢有效 AI 缓存。
            canvas = (1920, 1080)
    else:
        canvas = (1920, 1080)
    fitting = max(0, int((duration - settings['start'] - settings['duration']) // settings['interval']) + 1)
    if not fitting:
        raise DubForgeError('评论开始时间和显示时长超出视频，无法上屏；请调整评论时间设置', code='COMMENTS_AUTO_EMPTY')
    state = comment_preparation.prepare(uid, {
        'target': spec['comment_count'], 'prompt': spec['comment_prompt'], 'ids': source_ids,
        'failure_policy': spec.get('comment_failure_policy', 'skip'), 'settings': settings,
    }, cancel_check=cancel, extra_settings=display_settings, canvas=canvas, capacity=fitting,
       log=write_log, phase=report_phase)
    prepared = state['preparation']
    invalid_manual = prepared.get('invalid_manual') or []
    if invalid_manual:
        # 自动补齐不能撤销人工决定；超长人工文字只提示，不会进入此阻断列表。
        labels = '、'.join(str(item['id']) for item in invalid_manual[:8])
        raise DubForgeError(f'保留的评论仍需调整（{labels}），原上屏方案未修改；请在评论精选中处理后继续',
            code='COMMENTS_MANUAL_INVALID', detail={'invalid_manual': invalid_manual})
    draft = prepared['draft']
    items = draft['items']
    if len(items) > fitting:
        raise DubForgeError(f'保留的评论超过当前视频可容纳的 {fitting} 条，原上屏方案未修改；请调整显示间隔或解除部分保留',
            code='COMMENTS_MANUAL_INVALID', detail={'capacity': fitting, 'selected': len(items)})
    if not items:
        raise DubForgeError('没有可上屏评论：需通过内容检查并满足当前语言、文字长度及时间设置；请检查逐条原因或重新选择评论',
                            code='COMMENTS_AUTO_EMPTY')
    warnings = list(prepared.get('warnings') or [])
    if len(items) < spec['comment_count']:
        # 页面返回待确认草稿，自动模式则即将确认；使用实际成片数量表述。
        warnings = [message for message in warnings if not message.startswith('本次已准备 ')]
        warnings.append(f'本片实际使用{len(items)}条评论：受有效评分、可用译文、文字长度或视频时长限制。')
    cancel()
    write_log(f'评论确认：已选 {len(items)} 条，正在保存本次上屏方案…')
    report_phase(f'评论确认：保存 {len(items)} 条上屏方案')
    cfg = state.get('configuration') or configuration.resolve(uid)
    comment_studio.save(uid, {**draft, 'revision': state['overlay_revision'],
        'configuration_fingerprint': cfg['fingerprint']}, display_settings=display_settings)
    return warnings, len(items)


def confirmed_or_auto(uid, project=None):
    """合成规划/启动共用的评论门禁: 返回 (上屏方案, 是否将自动准备)。

    已有人工确认方案 → (方案, False); 无方案且评论准备=auto 且来源为
    YouTube → (None, True), 由合成步骤自动采集 (ADR-0073); 其余情形
    维持「请先确认」拦截。"""
    from dubforge import comment_studio, configuration, video_info
    try:
        return comment_studio.confirmed(uid), False
    except DubForgeError as exc:
        if exc.code != 'COMMENTS_NOT_CONFIRMED':
            raise
        project = project or crud.get_project(uid)
        if (project is not None and configuration.resolve(uid)['values'].get('comments.mode') == 'auto'
                and video_info._youtube(project.source_url)):
            return None, True
        raise


def current_overlay(uid):
    """当前已保存的上屏方案（无已保存方案时为 None）。"""
    from dubforge import comment_studio
    project = crud.get_project(uid)
    if project is None:
        return None
    try:
        return comment_studio.confirmed(uid)
    except DubForgeError as exc:
        if exc.code == 'COMMENTS_NOT_CONFIRMED':
            return None
        raise


def ensure_render_specs(ctx, deps, specs) -> list:
    """comments_auto 合成任务的前置：仍无已确认方案时自动准备一份；随后带方案
    重新解析全部版本规格，让运行内指纹与实际渲染输入一致。"""
    uid = ctx.project.uid
    from dubforge import comment_studio, configuration
    from dubforge.pipeline import comment_overlay
    from dubforge.pipeline.video_source import resolve_video
    overlay = current_overlay(uid)
    # 评论方案可能是在旧样式或旧画幅下确认的；仅靠字符数校验无法保证
    # 正式 ASS 能容纳双语、作者名和换行。所有合成入口都做一次同路径预检，
    # 自动模式可重新筛选并递补，手动模式则保留明确的排版错误供用户修正。
    layout_error = None
    if overlay and overlay.get('items'):
        video = resolve_video(ctx.project, deps.root)
        canvas = comment_overlay.video_canvas(video) if video.is_file() else (1920, 1080)
        for version in specs:
            plan = version.get('comment_overlay')
            if not plan or not version.get('include_comments'):
                continue
            try:
                comment_overlay.ass_document(plan, canvas)
            except comment_studio.CommentError as exc:
                if exc.code == 'COMMENTS_LAYOUT_OVERFLOW':
                    layout_error = exc
                    break
                raise
    if layout_error:
        mode = configuration.resolve(uid)['values'].get('comments.mode')
        if mode != 'auto':
            raise layout_error
        # 自动模式的确认方案不可直接放进成片；复用评分/翻译缓存，
        # 让 prepare_overlay 按实际画幅筛掉问题候选并从评分池递补。
        spec = spec_from_config(uid)
        base = spec['comment_settings']
        extra = [{**base, 'language_mode': s['values'].get('comments.language_mode', base['language_mode'])}
                 for s in specs if s['include_comments']]
        ctx.log('评论方案与当前画幅/样式不匹配：自动重新筛选可排版评论并递补…')
        from dubforge import runtime_edit
        set_phase = getattr(ctx, 'set_progress_phase', lambda *_args, **_kwargs: None)
        with runtime_edit.pipeline_writes(ctx):
            try:
                warnings, count = prepare_overlay(uid, spec, cancel_check=ctx.check_cancel,
                    extra_settings=extra, log=ctx.log,
                    phase=lambda label: _report_stage(ctx, label))
            finally:
                set_phase('', indeterminate=False)
        for warning in warnings:
            ctx.log(f'⚠ {warning}')
        ctx.log(f'已重新确认 {count} 条可排版评论')
        overlay = current_overlay(uid)
    if overlay and overlay.get('items'):
        ctx.log('复用已确认的评论上屏方案')
    else:
        spec = spec_from_config(uid)
        base = spec['comment_settings']
        extra = [{**base, 'language_mode': s['values'].get('comments.language_mode', base['language_mode'])}
                 for s in specs if s['include_comments']]
        ctx.log('评论准备为自动采集：正在采集、评分并翻译上屏评论…')
        from dubforge import runtime_edit
        set_phase = getattr(ctx, 'set_progress_phase', lambda *_args, **_kwargs: None)
        with runtime_edit.pipeline_writes(ctx):
            try:
                warnings, count = prepare_overlay(uid, spec, cancel_check=ctx.check_cancel, extra_settings=extra,
                    log=ctx.log, phase=lambda label: _report_stage(ctx, label))
            finally:
                set_phase('', indeterminate=False)
        for warning in warnings:
            ctx.log(f'⚠ {warning}')
        ctx.log(f'已自动确认 {count} 条上屏评论')
        overlay = current_overlay(uid)
    from dubforge.pipeline import assembly_versions
    raw = {v['id']: v for v in assembly_versions.catalog(uid)['versions']}
    return [assembly_versions.resolve(deps, raw[s['id']], comment_plan=overlay) for s in specs]
