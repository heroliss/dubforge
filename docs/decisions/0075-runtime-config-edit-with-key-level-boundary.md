# ADR-0075：运行中可修改制作参数与键级边界暂停

日期：2026-09-30。状态：采用。

## 背景

全流程运行期间修改制作参数（如模板的音轨分离参数）被三层门禁拦截：`WORKFLOW_BUSY`（全流程租约）、`CONFIGURATION_BUSY`（任意步骤运行中）、`PIPELINE_BUSY`（配音编辑）。用户批量运行多项目时被迫先停止全部流程才能改一个参数，再逐个恢复。

这些拦截是 UX 哲学选择而非技术必需：每个步骤启动时冻结自己的配置快照，正在执行的步骤不受中途修改影响；引擎早有 `_check_configuration` 在每步边界对比指纹并暂停，`resume` 也会按产物新鲜度修复游标——「改动 → 下一步边界暂停 → 重新确认 → 受影响步骤重做、其余跳过」的路径本来就存在，只是编辑入口被锁死走不到。真正的数据一致性保护只有配音逐句编辑（`dubbing_session`，运行中改逐句文本会与合成写回冲突），必须保留。

## 决策

### 1. 放开运行中编辑

`configuration._check_idle` 只保留 `dubbing_session.check_idle`（配音会话互斥），删除全流程租约检查与「任意步骤运行中」检查；`_update_project_step_params` 同步移除 `check_idle` 前置。运行中保存设置、改步骤参数、绑定/解绑系列模板均放行。手动链语义：正在运行的步骤用旧快照，之后启动的步骤取新值；已完成的步骤要应用新参数需显式重跑（下游自动过期）。

### 2. 键级边界暂停（剩余步骤消费的键）

- `workflow_plan` 拆出 `configuration_snapshot`（指纹前逐键快照：来源身份 + 各步骤 cfg_keys 前缀的值树），`configuration_stamp` 变为其摘要；计划新增 `configuration_snapshot` 字段。
- `_check_configuration(run, steps)`：按「剩余步骤集合」重建快照，与冻结快照做**字段级子集对比**（`stored ∩ current == current`）+ 来源身份对比。传入剩余步骤列表时，只有仍被剩余步骤消费的键才参与；发布准备阶段传 `[]`（制作已完成，只校验来源身份）。旧运行无快照字段时退回全量指纹对比。
- 版本模式（引擎运行恒为版本模式）下 `assemble`/`subtitles` 呈现参数本就随版本规格单独冻结、不在指纹内——运行中改这些参数不打断流程是**正确语义**（运行产出已确认的版本，改动作用于下次确认）。
- 触发后行为不变：`WORKFLOW_CONFIG_CHANGED` → 运行停止（消息「已暂停；请重新预检后继续」）→ `resume` 重预检，指纹按全量对比，游标按新鲜度修复（受影响步骤自动重做，未变步骤跳过）。
- 引擎运行中禁止插入手动单步（`runner._start_step` 的 `check_idle`）保持不变。

### 3. 不做的事

- 手动链（`_chain`）不引入边界暂停：没有等价的计划冻结点，改动自然作用于之后启动的步骤。
- 配音逐句编辑的 `PIPELINE_BUSY` 互斥保留。
- 不在保存时预判「这次修改会影响哪些步骤」并提示——边界暂停消息 + 总览恢复入口已足够。

## 验证

`test_boundary_check_narrows_to_remaining_steps`（preview 计划直查：剩余键触发/还原不触发/版本冻结键不触发/发布阶段只看来源）、`test_runtime_edit_of_finished_step_does_not_pause`（E2E：运行中改已完成步骤参数不打断）、`test_lease_still_blocks_manual_step_start`（租约仍禁手动单步）、`test_edit_and_binding_allowed_while_dependent_project_runs`、`test_batch_binding_applies_to_projects_with_queued_steps`（改写自旧「运行中拒绝」契约）。全量：后端 1324 项通过，前端测试与构建通过。

## 已知约束

- 引擎运行恒为版本模式：改「合成/字幕呈现」参数在当前运行永远不生效（含暂停后恢复），需重新预检生成新版本规格。
- 批量场景改一个系列模板会让多个在跑全流程各自在边界暂停，需逐个（或用「继续失败的自动流程」批量）重新确认。
