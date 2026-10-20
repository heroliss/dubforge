"""项目交付目标解析；只读取已保存设置，不启动制作或远程写操作。"""
import copy


def production_selection(values):
    """历史选择仅在证据唯一时兼容；版本存在不等于选择了制作它。"""
    versions = [row['id'] for row in values['assembly.versions']]
    selected = values.get('assembly.target_version_ids')
    source = 'explicit'
    if selected is None:
        if len(versions) == 1:
            selected, source = versions, 'single_version'
        elif values.get('publish.version_ids') is not None:
            selected, source = values['publish.version_ids'], 'publish_selection'
        else:
            selected, source = [], 'unresolved'
    valid = bool(selected) and len(set(selected)) == len(selected) and set(selected) <= set(versions)
    blockers = [] if valid else ['请到「合成成片」选择并保存项目目标成片，不能根据历史版本自动决定制作范围。']
    return dict(version_ids=list(selected) if valid else [], source=source,
                needs_confirmation=not valid, blockers=blockers)


def bind_recipe(spec, data):
    """制作目标与发布分P独立，原稿目标不提升保存的交付权限。"""
    values = data['values']
    result = copy.deepcopy(spec)
    mode = result['mode']
    target = values.get('publish.project_target')
    selection = production_selection(values)
    existing = isinstance(target, dict) and target.get('kind') == 'existing' and mode != 'produce'
    update = {key: copy.deepcopy(target[key]) for key in
              ('publication_id', 'replace_videos', 'replacements', 'sync_fields')} if existing else None
    metadata_only = existing and not update['replace_videos'] and 'add_parts' not in update['sync_fields']
    issues = []
    if not metadata_only:
        issues.extend(dict(code='PROJECT_ASSEMBLY_TARGET_REQUIRED', message=message, step='assemble')
                      for message in selection['blockers'])
    if mode in {'draft', 'upload', 'publish'} and target is None:
        target_selection = data.get('target_selection') or {}
        if target_selection.get('source') == 'history_requires_choice':
            message = '项目已有发布记录，请到「发布到B站」明确选择新建稿件或要更新的原稿。'
        elif target_selection.get('source') == 'explicit_unset':
            message = '本项目稿件目标已保留为未选择，请到「发布到B站」选择新建稿件或要更新的原稿。'
        else:
            message = '请到「发布到B站」明确选择新建稿件或要更新的原稿。'
        issues.append(dict(code='PROJECT_PUBLISH_TARGET_REQUIRED',
                           message=message, step='publish'))
    if existing and update['replace_videos'] and not update['replacements']:
        issues.append(dict(code='PROJECT_UPDATE_REPLACEMENTS_REQUIRED',
                           message='请到发布步骤选择要替换的分P与成片版本。', step='publish'))
    production = [] if metadata_only else list(selection['version_ids'])
    publish = values.get('publish.version_ids')
    if publish is None:
        publish = list(selection['version_ids'])
    else:
        publish = list(publish)
    if existing and not metadata_only:
        # 替换范围才决定额外制作目标，不因发布清单中未采用的历史版本而重做。
        needed = [item['version_id'] for item in update['replacements']]
        if 'add_parts' in update['sync_fields']:
            needed.extend(publish)
    elif mode != 'produce' and not metadata_only:
        needed = publish
    else:
        needed = []
    production = list(dict.fromkeys([*production, *needed]))
    result.update(target_driven=True, delivery_mode=mode,
                  production_version_ids=production, version_ids=[] if metadata_only else publish,
                  project_settings_fingerprint=data['fingerprint'], project_goal_issues=issues)
    result['part_titles'] = {key: title for key, title in values['publish.part_titles'].items() if key in publish}
    if existing:
        result.update(mode='update', update=update)
    elif (mode != 'produce' and isinstance(target, dict) and target.get('kind') == 'new'
            and (data.get('target_selection') or {}).get('source') == 'project'):
        # 用户保存“创建新稿”即明确选择；首次发布缺省绝不提升该授权。
        result['confirm_new_publication'] = True
    return result


def production_completion(deps):
    """按当前保存的本地制作目标核对完成；不执行预检、制作或平台请求。"""
    from dubforge import configuration
    from dubforge.domain import crud
    from dubforge.errors import DubForgeError
    from dubforge.pipeline.registry import STEP_KEYS

    def result(keys, *, known=True, complete=False, source='target'):
        keys = [key for key in STEP_KEYS if key in keys and key != 'dubtask']
        return dict(steps=keys, known=known, complete=complete, source=source,
                    failed=any(deps.status(key) == 'failed' for key in keys))

    # 旧接口明确保存的选择仍是独立制作合同，尤其不能把主动选中的字幕导出删掉。
    selected = crud.project_params(deps.project).get('auto_steps')
    if isinstance(selected, list):
        keys = [key for key in STEP_KEYS if key != 'dubtask' and key in selected]
        return result(keys, complete=bool(keys) and all(deps.display_fresh(key) for key in keys),
                      source='explicit_steps')
    try:
        if deps._assembly_configuration is None:
            deps._assembly_configuration = configuration.resolve(deps.project.uid, settings=deps.settings)
        data = deps._assembly_configuration
        selected = production_selection(data['values'])
        if selected['needs_confirmation']:
            return result([], known=False)
        spec = bind_recipe(dict(mode=data['values']['run.mode']), data)
        # 纯资料更新没有额外视频消费，但不把空制作集合冒充本地已完成。
        ids = spec['production_version_ids'] or selected['version_ids']
        from dubforge.pipeline import assembly_versions
        definitions = {version['id']: version for version in data['values']['assembly.versions']}
        if not ids or any(key not in definitions for key in ids):
            return result([], known=False)
        overlay = None
        if any(definitions[key]['include_comments'] for key in ids):
            from dubforge import comment_studio
            overlay = comment_studio.effective_overlay(deps.project.uid, comment_studio._read(deps.project).get('overlay'))
        versions = [assembly_versions.resolve(deps, definitions[key], comment_plan=overlay) for key in ids]
        actual = deps.with_versions(versions)
        from dubforge.workflow_plan import target_closure
        keys = target_closure(actual, ['assemble'])
        from dubforge.pipeline.version_render import VersionContext, uses_source_directly
        direct = [uses_source_directly(VersionContext(actual, version), version) for version in versions]
        if all(direct):
            # 当前目标完全直用源片时，旧的编码失败已不属于实际制作合同。
            keys.discard('assemble')
        scope = result(keys)
        if scope['failed']:
            return scope
        # 展示成品的 assembly_state 只核验已有版本；完成合同必须逐一核验当前目标，
        # 新增但尚未生成的目标不能借用其他旧成品的 fresh。原片直用无需伪造成功登记。
        targets_ready = all(assembly_versions.is_current(actual, version)
                            or (source_direct
                                and not actual.for_version(version).unready('assemble'))
                            for version, source_direct in zip(versions, direct))
        scope['complete'] = targets_ready and all(actual.display_fresh(key) for key in keys if key != 'assemble')
        scope['version_ids'] = list(ids)
        return scope
    except (DubForgeError, OSError, ValueError, TypeError, KeyError):
        # 设置或目标引用不可核实时保守保留待处理；不补写产物/指纹或降格为历史发布成功。
        return result([], known=False)
