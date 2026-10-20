# 固定字幕样本与离线质量观察

日期：2026-10-07。沿用 ADR-0102 的全文翻译和定稿原文逐行对应，以及 ADR-0104 的适中配音单元、宽松纠错和识别证据边界。

## 目的和边界

过去的提示词测试可以确认程序接受何种输出，不能证明真实模型能正确纠错。新增固定真实片段，保留原转录、词轴、历史模型候选和正式产物的区别。修改提示词或后处理之后，可以用相同窗口比较新旧稿，避免把“少了几个短句”误当成质量提高。

本工具只读取文本和时间，不运行 LLM、ASR、TTS，不读取配置或数据库，不改项目文件，不改变生产校验。全文请求、low 思考强度和现有检查宽松程度均保持。退出码 0 表示报告已完成，结构问题仍写入报告；退出码 2 表示输入或报告保存失败。它不是自动拒绝字幕的门禁。

## 固定样本

样本位于 `backend/tests/fixtures/text_quality/corpus.json`。从首项目 `vPoYu-uHQPg` 和另一个包含独立 `okay` 的实际项目只读截取，记录源文件 SHA-256、原编号、秒数及模型完整响应的时间和哈希。不会记录密钥、连接资料、全量日志或音视频文件。

共九个关注片段：

| 片段 | 检查目的 |
| --- | --- |
| 票价 `10 yen` | 观察 `yuan` 纠错和孤立 `I`，不把常识推测当成已核听事实 |
| `I mean` | 观察连续短语是否在同一行，允许其他合理自然分句 |
| `The line` | 回放模型完整短语被历史后处理拆成独立 `The` 的问题 |
| `we` 与 `made it` | 观察长词轴空档和孤词；不能按词性直接删词 |
| `one` 与后句 | 区分跨停顿参考上下文和字幕时间占用空档 |
| 短应答 `Yeah` | 防止因短句规则丢失有意义内容 |
| 零时长 `now` | 句内仍有意义，防止零时长直接触发删除 |
| `kwa jude` | 观察专名 `Quanjude` 规范化，原名仍需核听 |
| 独立 `okay` | 保留有意义应答，避免孤词启发误伤 |

这些样本目前全部标注为**原音待核听**。词轴空档不是经过核听的静音结论，疑词也不是确认错误；标注措辞是复核关注点，不是唯一正确译文。未匹配 `10 yuan` 可能只是用了 `ten Chinese yuan` 等合理改写。

## 用法

在 `backend/` 运行；不需要起服务。先查看来源标识：

```powershell
uv run python -m dubforge.text_quality --corpus tests/fixtures/text_quality/corpus.json --list
```

评估与样本相同的视频，使用其完整整理稿和对应译文；这里的路径是当前项目示例。报告放在项目目录以外，输入文件保持只读。

```powershell
$qualityProject = '../data/projects/20260929_1926_www.youtube.com_vPoYu-uHQPg'
uv run python -m dubforge.text_quality --corpus tests/fixtures/text_quality/corpus.json --source train --split "$qualityProject/artifacts/split/split.txt" --translate "$qualityProject/artifacts/translate/translate.txt" --report '../data/diagnostics/text-quality-current.md'
```

旧稿应另行保存供比较。增加 `--baseline` 和可选 `--baseline-translate` 比较同一视频；译文需要对应各自的整理稿，不能拿旧稿译文评价新稿的语义质量。也可省略译文，这时报告明确“未提供译文”。

```powershell
uv run python -m dubforge.text_quality --corpus tests/fixtures/text_quality/corpus.json --source train --split 'D:/evaluation/new-split.txt' --translate 'D:/evaluation/new-translate.txt' --baseline 'D:/evaluation/old-split.txt' --baseline-translate 'D:/evaluation/old-translate.txt' --format json --report 'D:/evaluation/comparison.json'
```

Markdown 方便阅读，JSON 保留每个窗口的候选行、观察匹配行号和输入哈希，方便归档比较。工具禁止报告覆盖任何输入。

## 指标怎么理解

- 全稿结构：行数、连续/重复编号、空文或无效时长、时间重叠。它们可以复现，但不代表语义正确。
- 源译编号对应：重复、缺失、多余、乱序编号，以及空译文。空译文单独列出，不直接判错；编号一致也不能证明每句译文忠实。
- 固定窗口：参考词轴空档的字幕占用秒数取区间并集，避免重叠字幕重复计数；给出原句和当前稿便于核听。
- 内容关注：只做标准化后的完整词组匹配，忽略大小写、标点和空格。标注支持短语、独立词或句尾匹配，没有模糊推断、同义替换或关键词删除。
- 短句/单词行：只给出观察计数，不设置最短句长，不把减少短句当成进步。
- 新旧变化：展示每项措辞是否出现、空档占用和重叠处数的变化；没有整体质量分。

报告不捏造未测的模型耗时、token、声学质量或翻译忠实度。评估提示词时，应另行记录相同模型、low 参数、全文输入、真实耗时和 token，并对这些片段核听原音及配音。人工确认后，可补充 `audio_review` 和说明，保留此前快照，避免把模型候选直接当成答案。

## 原音与分离人声的后续 A/B

YouDub 使用分离人声识别，我们当前使用原音并启用 VAD。本轮没有更改默认输入，也没有新增未经素材验证的识别配置。分离可能减少背景干扰，也可能损伤轻声或制造失真，无法仅凭代码确定优劣。

后续应在独立评估目录对同一视频分别识别原音和分离人声，固定模型、语言、VAD 和解码参数，保留两份原始转录及词轴。用本样本核听并比较误增词、真实短答误删、专名识别、起止误差及耗时；再决定是否开放可选输入。不能把更少字词或更高模型置信度当成改善的充分证据。

## 验证

`tests/test_text_quality_regression.py` 使用隔离 DATA_ROOT，验证真实快照、当前离线时间后处理不再拆坏 `The line`、短答和零时长有意义词不被拒绝、空档并集计数、译文重复编号保留诊断、原子报告及输入不变。测试不发模型请求。
