"""旧制作链的恢复预检：补齐真实依赖，执行终点仍限定在原剩余计划。"""
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEP_MAP
from dubforge.pipeline.recovery import RecoveryDependencies as Dependencies, describe


class ChainRecoveryError(DubForgeError):
    code = 'CHAIN_PLAN_CHANGED'
    suggestion = '请重新检查恢复方案，再确认本次执行范围。'


def _comment_configuration(deps):
    """评论准备方式属于完整项目配置，不能从仅制作参数的 cfg 覆盖中推断。"""
    from dubforge import configuration
    if deps._assembly_configuration is None:
        deps._assembly_configuration = configuration.resolve(deps.project.uid, settings=deps.settings)
    return deps._assembly_configuration['values']


def _assembly_settings(deps, resolved):
    """只冻结版本实际使用的呈现值，忽略未启用或被版本覆盖的默认参数。"""
    from dubforge import comment_studio, workflow_plan
    from dubforge.pipeline import assembly_versions as versions
    values = []
    needs_comments = any(version['include_comments'] for version in resolved)
    for version in resolved:
        item = dict(id=version['id'], values=versions.active_values(version))
        if version['include_comments']:
            settings = comment_studio.validate_settings({key.removeprefix('comments.'): value
                for key, value in version['values'].items() if key.startswith('comments.')})
            item['comments'] = comment_studio.render_fingerprint_plan(dict(settings=settings, items=[]))['settings']
        values.append(item)
    comments = None
    if needs_comments:
        config = _comment_configuration(deps)
        mode = config['comments.mode']
        comments = dict(mode=mode)
        if mode == 'auto':
            comments['preparation'] = {key: config['comments.' + key]
                for key in ('limit', 'count', 'prompt', 'failure_policy')}
    source = workflow_plan.configuration_snapshot(deps, steps=[])['source']
    return versions.digest(dict(source=source, versions=values, comments=comments))


def _manual_inputs(deps, key, resolved):
    """只比较用户编辑；上游正常生成文件和音频不会改变此快照。"""
    from dubforge.pipeline import subtitle_document
    params = crud.project_params(deps.project)
    if key == 'tts':
        return dict(subtitle_edits=subtitle_document.fingerprint(deps.project, key, deps.cfg),
                    tts_lines=params.get('tts_lines', {}), dubbing_edit=params.get('dubbing_edit', {}))
    if key in {'subtitles', 'dubtask'}:
        edits = (subtitle_document.records(deps.project)
                 if key == 'subtitles' and deps.cfg('content_review.enabled', True)
                 else subtitle_document.fingerprint(deps.project, key, deps.cfg))
        return dict(subtitle_edits=edits)
    if key != 'assemble':
        return None
    from dubforge import comment_studio
    edits = subtitle_document.records(deps.project)
    result = dict(versions=[])
    for version in resolved:
        scoped = deps.for_version(version)
        keys = {'text', 'start', 'end'} if version['dubbing'] else set()
        if version['burn_subtitles']:
            keys |= {'start', 'end'}
            track = version['values']['assemble.subtitle_track']
            if track != 'trans':
                keys.add('source_text')
            if track != 'source':
                keys.add('text')
        filtered = (edits if scoped.cfg('content_review.enabled', True) else
            {ident: {part: {field: value for field, value in row.get(part, {}).items() if field in keys}
                     for part in ('base', 'values')}
             for ident, row in edits.items() if keys.intersection(row.get('values', {}))})
        result['versions'].append(dict(id=version['id'], subtitle_edits=filtered))
    if any(version['dubbing'] for version in resolved):
        result['dubbing_edit'] = params.get('dubbing_edit', {})
    if (any(version['include_comments'] for version in resolved)
            and _comment_configuration(deps)['comments.mode'] != 'auto'):
        # saved 模式冻结已确认内容；auto 的自然采集写回不能误判为人工改动。
        comment_studio.confirmed(deps.project.uid)
        result['comments'] = [dict(id=version['id'],
            overlay=comment_studio.render_fingerprint_plan(version['comment_overlay']))
            for version in resolved if version['include_comments']]
    return result


def _execution_snapshot(deps, steps, definitions, resolved):
    from dubforge import workflow_plan
    from dubforge.pipeline import assembly_versions as versions
    configuration_stamps, manual_input_stamps = {}, {}
    for key in steps:
        configuration_stamps[key] = (_assembly_settings(deps, resolved) if key == 'assemble' else
                                     workflow_plan.configuration_stamp(deps, steps=[key]))
        if (inputs := _manual_inputs(deps, key, resolved)) is not None:
            manual_input_stamps[key] = versions.digest(inputs)
    return dict(schema=2, configuration_stamps=configuration_stamps,
                manual_input_stamps=manual_input_stamps,
                version_definitions_digest=versions.digest(definitions) if resolved else None)


def plan(uid):
    from dubforge.pipeline import assembly_versions as versions
    from dubforge import comments_auto, workflows, workflow_plan
    from dubforge.runner import compute_fingerprint
    project = crud.get_project(uid)
    if project is None:
        raise ChainRecoveryError('项目不存在', code='PROJECT_NOT_FOUND')
    params = crud.project_params(project)
    remaining = params.get('_chain')
    if (params.get('_chain_state') != 'held' or not isinstance(remaining, list)
            or not remaining or any(k not in STEP_MAP for k in remaining)):
        raise ChainRecoveryError('没有已挂起的制作计划，请检查并运行完整流程', code='CHAIN_NOT_HELD')
    deps = Dependencies(project)
    definitions, resolved, auto_comments = [], [], False
    if 'assemble' in remaining:
        definitions = versions.catalog(uid)['versions']
        overlay, auto_comments = comments_auto.confirmed_or_auto(uid, project) if any(
            v['include_comments'] for v in definitions) else (None, False)
        resolved = [versions.resolve(deps, v, comment_plan=overlay) for v in definitions]
        deps = deps.with_versions(resolved)
    candidate = deps.plan(remaining, True)
    recovery = describe(deps, candidate['steps'], previous_steps=remaining,
                        changed=candidate['steps'] != remaining)
    # 原链只记得剩余步骤；不伪造历史版本或历史参数。
    previous = (params.get('_chain_hold') or {}).get('step', remaining[0])
    recovery.update(previous_step=previous, previous_label=STEP_MAP[previous].label if previous in STEP_MAP else None)
    if recovery['start_step'] is None:
        recovery['reason'] = '原制作计划所需产物均有效，确认后结束此计划'
    blockers = list(dict.fromkeys(item['reason'] for item in candidate['blockers']))
    if any(row.status in {'queued', 'running'} for row in deps.steps.values()):
        blockers.append('本项目正在制作，请等当前任务完成后再恢复')
    try:
        workflows.check_idle(uid)
    except DubForgeError as exc:
        blockers.append(exc.message)
    if not deps.root.is_dir():
        blockers.append('项目目录缺失，请先还原项目目录')
    effects = ['仅继续原制作计划；使用当前已保存设置，不上传、不投稿。']
    if resolved:
        effects.append('生成已保存版本：' + '、'.join(v['name'] for v in resolved))
    if auto_comments:
        effects.append('评论准备为自动采集：合成时自动抓取、评分并翻译上屏评论。')
    # 确认令牌绑定真实起点、原链、当前输入与版本，不向客户端泄露配置原始值。
    binding = dict(remaining=remaining, hold=params.get('_chain_hold'), recovery=recovery,
        configuration=workflow_plan.configuration_stamp(deps, version_mode=bool(resolved), steps=candidate['steps']),
        inputs={row['key']: compute_fingerprint(project, row['key'], dependencies=deps)
                for row in recovery['rows']},
        versions=[dict(id=v['id'], name=v['name'], fingerprint=versions.fingerprint(deps, v)) for v in resolved])
    execution = _execution_snapshot(deps, candidate['steps'], definitions, resolved)
    return {**candidate, 'blockers': blockers, 'labels': {r['key']: r['label'] for r in recovery['rows']},
            'recovery': recovery, 'fingerprint': versions.digest(binding), 'version_specs': resolved,
            'effects': effects, 'mode': 'produce', 'execution': execution}


def check_execution(uid, snapshot, remaining):
    """确认后改变剩余步骤的设置或版本时挂起，不静默扩大已确认范围。"""
    from dubforge import workflow_plan
    from dubforge.pipeline import assembly_versions as versions
    deps = Dependencies(crud.get_project(uid))
    definitions, resolved = [], []
    if 'assemble' in remaining:
        definitions = versions.catalog(uid)['versions']
        if (snapshot.get('version_definitions_digest') is not None
                and snapshot['version_definitions_digest'] != versions.digest(definitions)):
            raise ChainRecoveryError('成片版本已变化，请重新检查恢复方案')
    if snapshot.get('schema') == 2:
        from dubforge import comment_studio
        overlay = comment_studio.effective_overlay(uid, comment_studio._read(deps.project).get('overlay')) if any(
            version['include_comments'] for version in definitions) else None
        resolved = [versions.resolve(deps, version, comment_plan=overlay) for version in definitions]
        deps = deps.with_versions(resolved) if resolved else deps
        try:
            current = _execution_snapshot(deps, remaining, definitions, resolved)
        except DubForgeError as exc:
            raise ChainRecoveryError('恢复所需的人工编辑或评论已变化，请重新检查：' + exc.message) from exc
        if any(snapshot.get('configuration_stamps', {}).get(key) != value
               for key, value in current['configuration_stamps'].items()):
            raise ChainRecoveryError('剩余步骤的设置已变化，请重新检查恢复方案')
        if any(snapshot.get('manual_input_stamps', {}).get(key) != value
               for key, value in current['manual_input_stamps'].items()):
            raise ChainRecoveryError('字幕校对、声源选择、句级参数或已确认评论已变化，请重新检查恢复方案')
        return
    expected = snapshot.get('configuration_stamps', {})
    if any(expected.get(key) != workflow_plan.configuration_stamp(deps, steps=[key])
           for key in remaining if key in expected):
        raise ChainRecoveryError('剩余步骤的设置已变化，请重新检查恢复方案')
