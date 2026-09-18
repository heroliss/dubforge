<script setup lang="ts">
// 帮助页: 目录结构 / 产物速查 / UI 上看不出来的隐藏知识 (纯静态, 内容与后端实现同步维护)
</script>

<template>
  <div class="help">
    <n-h1 style="font-size: 22px">📖 帮助</n-h1>

    <n-card title="数据都存在哪？" size="small" style="margin-bottom: 14px">
      <p class="p">
        所有数据集中在一个<strong>总工作目录</strong>里（默认是程序目录下的
        <code>data</code>，可在「⚙️ 设置 → 存储」修改，应用后重启生效）。
        一个视频 = 一个项目目录，删除项目就是删除这个目录，不会碰你磁盘上的其他文件。
      </p>
      <pre class="tree">总工作目录 (data)\
├─ dubforge.db              项目索引 (步骤状态/产物登记表; 目录里的事实为准, 删了可重建)
├─ uploads\                 拖入上传的收件箱 —— 文件随引用它的项目一起删除
├─ download_archive.txt     已下载视频去重档案 (同一链接永不重复下载)
└─ projects\
   ├─ 20260918_1533_我的视频\   ← 一个项目目录 (创建日期_时间_标题, 天然按时间排序)
   │  ├─ manifest.json          项目自述文件: 标题/来源/每步状态/全部产物清单 (记事本可读)
   │  ├─ media\                 下载的视频 (本地导入的项目此目录为空, 只记引用不复制)
   │  ├─ audio\                 中间音频: raw.wav 原始音轨 · vocal.wav 分离人声 · background.wav 背景音
   │  ├─ srt\                   字幕: src.srt 源语言 · trans.srt 译文 · bilingual.srt 双语
   │  ├─ artifacts\             各步骤产物 (按步骤分目录)
   │  │  ├─ download\           meta.json —— 视频元数据 (标题/时长/大小)
   │  │  ├─ transcribe\         transcribe.json —— 逐句转录结果 (词级时间戳)
   │  │  ├─ split\              split.json —— 分句结果
   │  │  ├─ translate\          translate.json —— 逐句译文 (+ 术语表/风格记录)
   │  │  ├─ dubtask\            dubtask.json —— 配音任务表 (每句: 文本/时间/时长/语速)
   │  │  ├─ tts\                12.wav —— 每句配音, 文件名就是句号
   │  │  └─ assemble\           final_xxx.mp4 —— 合成后的完整音轨
   │  ├─ logs\                  每步骤的运行日志 (排查问题先看这里)
   │  └─ output\                最终成片 final_日期_时间.mp4 (可带伴生字幕 .srt)
   └─ …</pre>
      <p class="p">
        <strong>搬走/备份一个项目 = 复制那个项目文件夹</strong>。
        每个项目的 <code>manifest.json</code> 记录了它的全部信息，打开目录就能看懂。
      </p>
    </n-card>

    <n-card title="你可能看不出来的机制" size="small" style="margin-bottom: 14px">
      <n-collapse>
        <n-collapse-item title="本地导入是「引用」不是「复制」" name="ref">
          <p class="p">
            导入本地视频时，程序<strong>不复制文件</strong>，只在项目里登记一条指向原文件的引用——
            几个 GB 的视频导入也是瞬间完成、不占双倍硬盘。代价：项目存续期间<strong>别移动或重命名原文件</strong>，
            否则重跑「视频获取」会提示原文件不存在。
          </p>
        </n-collapse-item>
        <n-collapse-item title="删除项目会删什么、不会删什么" name="delete">
          <p class="p">
            删除项目 = 删除<strong>项目目录</strong>（含全部中间产物）+ 数据库里的登记记录 +
            收件箱里它上传的文件（若无其他项目共用）。<strong>你磁盘上的原视频是外部引用，永远不会被程序删除或修改。</strong>
          </p>
        </n-collapse-item>
        <n-collapse-item title="为什么点另一个任务时，这个任务在「排队中」？" name="queue">
          <p class="p">
            单张显卡同时只能跑一个模型任务。DubForge 全局<strong>一次只执行一个步骤任务</strong>，
            其余自动排队（显示 ⏳ 排队中，排队时也能取消）。这是保护显存的刻意设计，不是卡死。
          </p>
        </n-collapse-item>
        <n-collapse-item title="重跑某一步会怎样？" name="rerun">
          <p class="p">
            重跑 = 清空该步产物 + 所有下游步骤标记「已过期」（stale）。
            反过来，<strong>输入没变的已完成步骤会被自动跳过</strong>（配置与上游产物一致时无需重算），
            所以改了翻译配置后只有翻译及之后的步骤会真正重跑。
          </p>
        </n-collapse-item>
        <n-collapse-item title="程序中途退出 / 断电会怎样？" name="recover">
          <p class="p">
            产物全部先写临时名、完成才原子改名，<strong>不会留半成品</strong>。
            重启后上次正在跑的步骤会被标记为「已中断」，直接重跑该步即可继续。
          </p>
        </n-collapse-item>
        <n-collapse-item title="设置页：「测试」和「应用」的区别" name="apply">
          <p class="p">
            「测试」只探测你当前表单里填的内容，<strong>不写入任何配置</strong>；
            「💾 应用设置」才是唯一写入口，写入 config.yaml 后才对翻译任务生效。
            有未应用的修改时页面会显示橙色「未应用」标记。
          </p>
        </n-collapse-item>
        <n-collapse-item title="LLM 参数怎么生效？temperature 在哪设？" name="llmparams">
          <p class="p">
            没有全局模型参数——每个配置组的「专属参数」按模型填写（JSON，如
            <code>{"temperature": 0.3}</code>）。多条规则：<strong>留空的字段不会发送</strong>，
            由服务商用自己的默认值；专属参数在请求的最后合并，同名覆盖一切；
            测试和真实翻译的取值逻辑完全一致。
          </p>
        </n-collapse-item>
        <n-collapse-item title="错误信息怎么看？" name="error">
          <p class="p">
            服务器有响应时，程序只展示<strong>状态码 + 码义 + 服务端返回的原文</strong>（那才是权威信息）；
            连不上服务器或本地校验失败时，才给出一条中文修复建议。
            例如 401 = 身份验证未通过（可能是 api_key 不对），404 可能是模型名不对或接口地址填错。
          </p>
        </n-collapse-item>
        <n-collapse-item title="总工作目录怎么搬家？" name="move">
          <p class="p">
            「⚙️ 设置 → 存储」填写新路径 → 💾 应用设置 → <strong>手动把整个 data 目录复制/移动过去</strong> →
            重启程序。已有数据不会自动搬家。
          </p>
        </n-collapse-item>
      </n-collapse>
    </n-card>

    <n-card title="外部服务" size="small">
      <p class="p">
        DubForge 不捆绑模型，通过 URL 使用本机/局域网上的外部服务：
        IndexTTS2.5（默认 <code>http://127.0.0.1:7860</code>）、GPT-SoVITS（备用，9880）、
        LLM 走 OpenAI 兼容接口。对应服务没开时，相关步骤会明确报错告诉你先把它启动。
      </p>
    </n-card>
  </div>
</template>

<style scoped>
.help { max-width: 860px; }
.p { margin: 6px 0; line-height: 1.75; font-size: 13px; }
.tree {
  background: #181c20; color: #d8e0e8; border-radius: 6px;
  padding: 12px 16px; font-size: 12.5px; line-height: 1.65;
  overflow-x: auto; margin: 8px 0;
}
code {
  background: rgba(128,128,128,.15); border-radius: 4px;
  padding: 1px 5px; font-size: 12px;
}
</style>
