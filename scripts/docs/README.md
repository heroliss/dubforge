# 文档截图复现

这组工具用于截取**真实 Vue 页面与真实本地 GET 接口呈现的演示数据**，再添加可复现的圈线、箭头和编号。它不生成产品界面，不执行真实模型任务，也不证明配音、合成或B站投稿成功。

准备阶段会创建专用 SQLite 数据库、三个演示项目、一个系列模板和六句自写双语校对稿。`--with-media` 仅用 FFmpeg 的 lavfi 图形与静音源生成 16 秒几何动画；没有外部素材、人声、真实账号、模型调用或正式成片记录。制作步骤保持待运行。

## 前提与目录

使用仓库已有的 Python 3.13 环境，以及安装/构建完成的 `frontend/dist/`。生成示例动画需要 PATH 中的 FFmpeg 和 FFprobe。不要为了截图重启用户实例、改变用户配置或在任务运行中重新构建前端。

所有命令从仓库根目录运行；服务端口固定为 **8611**。8611 有占用时工具拒绝启动，不复用、不接管、不杀占用进程。

| 文件 | 用途 |
|---|---|
| `prepare_demo.py` | 只有显式 `--prepare` 才创建全新隔离夹具，不启动服务 |
| `serve_demo.py` | 只有显式 `--serve` 才启动只读演示；关闭正常应用 lifespan，不运行恢复扫描和后台任务监控 |
| `annotate.mjs` | 在浏览器原图上合成 SVG 标注，输出独立 WebP 和摘要收据 |
| `package.json` / `package-lock.json` | 仅文档工具使用的 Sharp 锁定依赖，不进入主程序运行链 |
| `test_prepare_demo.py` | 标准库安全回归；不启动服务、不导入产品、不执行媒体命令 |

## 1. 显式准备新夹具

```powershell
# 仅在 data/diagnostics/readme-demo/runs/ 下使用一个新直接子目录
$demoName = 'capture-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
$demoRun = Join-Path (Get-Location).Path ('data/diagnostics/readme-demo/runs/' + $demoName)
& .\backend\.venv\Scripts\python.exe scripts/docs/prepare_demo.py --prepare --run-dir $demoRun --with-media
```

运行目录已存在、超出指定根目录或不是直接子目录都会被拒绝；不要删除旧证据来复用同名目录。不带 `--with-media` 可以只准备数据，但不会出现参考动画。

准备结果在新目录中：

- `fixture-config.yaml`、`fixture-data/`、`empty-model-cache/`：专用配置、数据库及模型缓存范围；不复制真实 `config.yaml`。
- `fixture.json`：演示标记、项目 UID、自绘媒体摘要与探测收据，以及零模型/零账号/零投稿/零正式成片声明。
- `screens.json`：五个真实路由、每页截图目的与操作边界。项目 UID 每次不同，按本次文件读取，不硬编码历史 UID。

## 2. 启动有时限的只读服务

```powershell
# 沿用上一步输出的 $demoRun，不要另建或复用其他目录
& .\backend\.venv\Scripts\python.exe scripts/docs/serve_demo.py --serve --run-dir $demoRun --max-seconds 900
```

服务只监听 `127.0.0.1:8611`，默认 600 秒，允许 30–900 秒，最长 **900 秒自动退出**。保留该前台终端。脚本校验演示目录标记、数据库、配置及自绘视频收据，拒绝链接到目录外的夹具文件。

HTTP 只允许 GET/HEAD/OPTIONS；设置保存、制作、模型请求、外部 HTTP、配音服务管理、媒体子进程与平台通信均受限制。原生文件选择器、环境扫描和任意外部预设不属于截图范围。只读服务仍会在自己的演示目录写诊断记录，不能把它描述为整个文件系统只读。

## 3. 在浏览器截取真实页面

使用当前可用的浏览器连接工具或浏览器原生截图功能打开 `http://127.0.0.1:8611`，按本次 `screens.json` 的路由访问。仓库没有隐藏的自动浏览器截屏命令；不要用生成 HTML、拼接控件或修改 DOM 文案代替真实页面。

| README 图片名 | 本次路由来源 | 展示范围 |
|---|---|---|
| `project-space` | `/` | 模板、项目勾选与批量入口 |
| `project-overview` | `/projects/<本次UID>` | 交付终点、目标成片与检查运行入口 |
| `subtitles-dubbing` | `/projects/<本次UID>/steps/tts` | 演示视频、六句校对稿、逐句设置 |
| `assembly-versions` | `/projects/<本次UID>/steps/assemble` | 原声/配音两版本及尚未制作的剪辑草稿 |
| `production-settings` | `/settings?section=production&stage=assemble` | 制作规则与继承入口，无真实账号和连接 |
| `settings-models` | `/settings?section=models` | 模型连接字段、测试入口与保存位置；未测试或保存连接 |
| `settings-runtime` | `/settings?section=runtime` | 并行度与总工作目录；未修改实际设置 |
| `manual-step` | `/projects/<本次UID>/steps/split` | 普通步骤的运行入口与尚未补齐的依赖 |
| `publishing-target` | `/projects/<本次UID>/steps/upload` | 项目交付方式与稿件目标，无发布记录 |
| `publishing-parts` | `/projects/<本次UID>/steps/upload` | 项目分 P 清单、排序与名称，无上传或投稿 |

只进行导航、滚动、折叠展开和本地勾选。不要点击保存、运行、生成、试配、试片、取帧、服务启动、测试连接、上传或投稿；被服务拒绝也不是许可去试这些操作。

保持真实浏览器尺寸和页面布局。首组原图宽 2560 像素，教学补图还包含宽 3160 像素的 JPEG，以每张图片的实际元数据为准；内容区域自身限制宽度，不为构图修改 viewport、CSS 或业务状态。`screens.json` 中的视口值只是准备脚本的参考值，不能当成本次真实采集尺寸。

先保存完整浏览器截图的原始字节；本次使用默认完整截图，不使用非零原点的浏览器截图裁剪。当前连接工具返回 JPEG，所以原图放在 `docs/images/raw/<图片名>.jpg`。其他工具若确实返回 PNG，可以保存 `.png`；**不要只改扩展名冒充格式**，也不要用后续裁剪结果覆盖原图。

记录原图尺寸、实际视口、待展示区域的 DOM 边界、页面缩放/设备像素比及本次夹具目录。按这些事实换算到原图像素后，在下一步的 `spec.crop` 中指定局部裁剪；这只是对已保存原图的离线处理，不改变浏览器视口或页面布局。

截图工具可能为画布补白，不能直接用“原图宽度 ÷ 视口宽度”推断缩放。用可辨识的控件边界核对实际映射，再逐图检查裁切与标记。

## 4. 添加圈线与编号

首次安装文档专用依赖：

```powershell
Push-Location scripts/docs
npm ci --ignore-scripts
Pop-Location
```

为每张图保存 `docs/images/annotations/<图片名>.json`。`crop` 以**实际完整原图像素**为准；`marks` 的目标盒与编号位置以**裁剪后画布像素**为准。不直接照抄 CSS 像素或缩小后的 WebP 坐标。下面只展示格式，位置必须根据本次原图重新测量：

```json
{
  "maxWidth": 1180,
  "crop": { "left": 500, "top": 100, "width": 1500, "height": 1000 },
  "marks": [
    {
      "number": 1,
      "shape": "rect",
      "target": { "x": 100, "y": 120, "width": 240, "height": 70 },
      "label": { "x": 70, "y": 90 },
      "arrow": true
    }
  ]
}
```

`crop` 可省略，省略时使用完整原图。提供时只能包含 `left`、`top`、`width`、`height` 四个整数：左/上边界非负、宽/高大于零，且整个区域在原图内。不能传入 `null`。例如原图目标点是 `(650, 220)`、裁剪原点是 `(500, 100)`，其标注坐标就是 `(150, 120)`。

`shape` 使用 `rect` 或 `ellipse`，`arrow: false` 可关闭箭头。目标盒必须在裁剪画布内，编号位置不能越界。`maxWidth` 允许 640–1200，默认 1180；不会放大较小画布，所以最终宽度始终不超过 1200。

```powershell
node scripts/docs/annotate.mjs docs/images/raw/project-space.jpg docs/images/annotations/project-space.json docs/images/project-space.webp
```

工具保留完整原图和标注清单，依次裁剪、合成标注、缩放；为输出独立生成 `project-space.webp.json`，记录原图/输出摘要、`original_size`、实际 `crop` 与原始 `spec`。省略 `crop` 时，收据仍记录原点为零的完整画布。输入、清单、输出、收据及临时路径不得互相覆盖或通过路径别名指向同一文件。

逐张目视检查：编号对应文字、圈线没有挡住按钮、裁剪包含必要上下文、缩小后中文仍可读。README 图注明确“隔离演示项目”，不把待运行状态改成成功，也不从演示库杜撰账号、审核或模型质量。[首组采集清单](../../docs/images/capture.json)与[教学补图清单](../../docs/images/tutorial-capture.json)记录实际原图尺寸、裁剪、输出大小和摘要。

## 5. 退出与保留证据

在另一个终端中使用本次目录，提前请求服务正常退出：

```powershell
# 新终端需要重新指定最初 prepare 输出的目录，替换下面的本次目录名
$demoRun = (Resolve-Path 'data/diagnostics/readme-demo/runs/<本次目录名>').Path
New-Item -ItemType File -Path (Join-Path $demoRun 'runtime.stop') | Out-Null
```

也可等到最大运行时间。确认服务进程正常退出、`runtime-events.json` 有 shutdown 且 watcher 已结束、8611 不再监听；不要按端口强杀其他进程。含 `runtime.stop` 的目录不会被重新启动，应为下一次截图准备新目录。

保留本次 `fixture.json`、`screens.json`、`runtime.json`、`runtime-events.json` 和必要的浏览器只读请求/截图记录在忽略的运行目录中；提交文档用的原图 JPG/PNG、标注 JSON、WebP 与摘要收据。不要提交演示数据库、配置、运行目录或私有浏览器资料。整组工具与图片只证明界面复现和标注过程，不代替正式媒体、模型端到端或平台发布验收。

## 首页的说明性示意图

README 另有两张直接维护的 SVG，和上述真实截图区分：

| 文件 | 解释什么 |
|---|---|
| [workflow-overview.svg](../../docs/images/workflow-overview.svg) | 导入、校对、成片与交付的四段体验，不代表所有项目必须执行全部步骤 |
| [local-architecture.svg](../../docs/images/local-architecture.svg) | 浏览器、本地程序与文件、媒体工具、按需模型服务及 B 站连接的边界 |

SVG 使用与工作台截图协调的暗色背景、浅色文字和少量蓝／青／橙色强调，自带文字、图形及可访问性标题，不加载远程资源或字体，也不模拟产品界面。修改后应在 GitHub 的实际内容宽度下检查中文可读性、连线、文字边界与明暗对比；截图收据 `capture.json` 只描述真实采集的五张图，不把示意图加入该采集记录。
