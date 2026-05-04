# Aequora Project Blueprint

这个文件是 Aequora 的项目总地图、任务板和交接日志。它面向 vibe coding 工作流：用户可以用直白自然语言在这里记录想法、痛点和下一步，Codex 每次开始任务前先读它，每次结束前检查并更新它。

## 使用规则

给用户：

- 可以直接在“任务池”里写需求，不需要写技术方案。
- 建议写成“我现在遇到什么问题 / 我想要什么效果 / 什么算解决”。
- 如果不确定怎么表达，先写主观体验，例如“启动太慢”“界面看不懂”“下载完不知道文件在哪里”。

给 Codex：

- 每次开始 Aequora 任务前，先读 `AGENTS.md`、`PROJECT_BLUEPRINT.md`，再看相关代码。
- 每次结束前，检查本次需求是否解决，并更新“任务池”和“工作日志”。
- 不要把这个文件当成正式规格冻结；它是活文档，允许随着 vibe coding 变化。
- 如果用户提出的新需求很模糊，先把它整理成清楚的任务条目，再实现。
- 更新任务时保留用户原意，补充工程判断和验证结果。

## 项目一句话

Aequora 是一个 Windows-first 的本地学习资料整理工具：用户丢进本地媒体、文件夹或网页链接，程序帮他整理出音频、字幕、文本、结果目录和后台记录。

## 当前产品方向

- 优先保证普通用户可以双击打开、看懂界面、完成任务、找到结果。
- GUI 是主体验，CLI 是开发、调试和批处理工具。
- 任务处理过程要透明：候选选择、进度、失败原因、结果位置都要清楚。
- 出错不能静默；日志、调试入口、恢复方式要保留。
- 项目文档要服务“新 Codex 窗口快速接续”和“用户自己能看懂当前进度”。

## 当前技术地图

- GUI: `media2text/gui_fluent.py`，PySide6 + qfluentwidgets。
- GUI 启动: `start.bat` -> `run_gui.pyw` -> `run_gui.py`。
- CLI: `media_tool.py` -> `media2text/cli.py`。
- 核心执行: `media2text/orchestrator.py`。
- 下载/元数据: `media2text/ytdlp_pipeline.py`。
- 网页候选发现: `media2text/scraper_engine.py`。
- 转写: `media2text/transcriber.py`。
- FFmpeg: `media2text/ffmpeg_utils.py`。
- 回退/清理 ledger: `media2text/run_ledger.py`。
- 配置: `config.json` + `media2text/config.py`。

## 当前仓库状态

- Git 历史已经重开，当前 `main` 是 Aequora 的新初始历史。
- 远端只保留 `origin/main`。
- 旧 `Unified Media2Text` 分支和 tag 已清理。
- `README.md` 是用户说明。
- `AGENTS.md` 是 Codex 交接说明。
- 本文件是项目路线图和任务日志。
- `CODEX_MEMORY.md` 是可上传/复用的长期记忆草稿。

## 任务池

状态说明：

- `Idea`: 只是想法，还没确定要做。
- `Ready`: 可以开始做。
- `Doing`: 正在做。
- `Done`: 已完成并验证。
- `Blocked`: 被外部条件卡住。

| ID | 状态 | 用户原话/自然语言需求 | 期望结果 | 最近处理 |
| --- | --- | --- | --- | --- |
| AQR-001 | Done | 需要 README 和 git 重新整理，把当前项目当第一版 | README 反映 Aequora 当前状态；git 历史从当前版本开始 | 已完成，`main` 新历史已推送 |
| AQR-002 | Done | 新 Codex 窗口要能快速读懂项目 | 仓库里有 Codex 交接文件 | 已完成，新增 `AGENTS.md` |
| AQR-003 | Done | 需要项目总地图和规则，每次任务前后都检查更新 | 有一个活文档记录蓝图、任务和日志 | 本次新增 `PROJECT_BLUEPRINT.md` |
| AQR-004 | Ready | 需要继续优化产品体验 | 先从启动、界面可懂性、结果可找到、失败可恢复这些方向挑下一个具体任务 | 等用户指定下一步或 Codex 根据现状建议 |
| AQR-005 | Done | 新任务选择本地视频后，候选页显示上一次网页任务里的旧候选 | 每次解析前清空旧候选；本地文件生成本地候选；迟到的旧网页扫描结果不能污染当前任务 | 已修复 `gui_fluent.py` 候选状态清理、扫描 token、本地候选创建，并验证通过 |

## 体验优化候选

这些不是承诺，只是后续可选方向：

- 启动体验：双击无反应时自动留下更明显的错误提示。
- 首页体验：让用户更快理解“粘贴链接/选择文件夹/开始解析”的流程。
- 结果体验：处理完成后更明显地打开结果目录、定位主结果文件。
- 失败体验：失败原因用普通语言解释，并给下一步按钮。
- 配置体验：把 Whisper 模型、语言、输出格式整理成更直观的预设。
- 打包体验：生成可分发的 EXE 或 zip，并保留 debug 启动方式。
- 文档体验：README 保持给普通用户，AGENTS/Blueprint 保持给 Codex 和维护者。

## 工作日志

### 2026-04-30

- 重写 `README.md`，从旧 `Unified Media2Text` 说明切换为 Aequora 当前说明。
- 更新 `.gitignore`，排除本地运行产物、日志、快捷方式、验证输出和旧脚本备份。
- 重开 git 历史，把当前项目作为新的 `main` 初始提交。
- 强制更新远端 `origin/main`，删除旧远端分支和旧 tag。
- 新增 `AGENTS.md`，作为未来 Codex 会话的项目交接文件。
- 新增 `PROJECT_BLUEPRINT.md`，作为项目总地图、任务池和日志。
- 新增 `CODEX_MEMORY.md`，作为可上传/复用的 Codex 项目记忆草稿。

### 2026-05-04

- 修复 Fluent GUI 候选缓存残留 BUG：开始新解析和进入候选准备时统一清空 `_candidate_items`、`_candidate_order`、`_candidate_rows`、模型、预览和运行上下文。
- 为候选扫描增加 `_candidate_scan_token`，忽略上一次网页扫描线程迟到返回的旧 `video.wixstatic.com` / `youtube.com` 候选。
- 本地文件现在在候选页生成 `source_kind="local"` 的本地候选，标题使用文件 stem，来源显示“本地文件”，状态显示“未处理”，`source_url` 为空。
- 顺手把 CLI help 描述从 `Unified Media2Text tool` 改为 `Aequora media organizer`。
- 验证：`compileall`、`media_tool.py --help`、offscreen GUI 状态测试均通过；临时验证目录已清理。

## 每次任务结束检查表

- 是否更新了任务池状态？
- 是否在工作日志写了本次关键变化？
- 是否说明了验证命令和结果？
- 是否避免提交运行输出、本机快捷方式、日志和虚拟环境？
- 如果改了启动、GUI 或打包路径，是否保留了 debug 路径？
